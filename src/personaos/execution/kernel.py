"""WorkerExecutionKernel — orchestrates task execution lifecycle.

Per ADR 0010:
  - Kernel owns lifecycle: validate → in_progress → completed|failed
  - Uses an injected resolve_context callable (typically
    ProfileRegistry.resolve_execution_context)
  - Calls adapter.execute(context, checkpoint)
  - Persists result/error + emits task events
  - Independent of OpenClaw — uses the Adapter abstraction

Per ADR 0009:
  - Context is resolved from task.profile_version, never worker.current
  - Adapter receives the frozen context

Per ADR 0016 (v0.2):
  - In-progress recovery with leases (lock_expires_at) and fencing (generation)
  - Fenced complete(), fail(), heartbeat(), and checkpoint_write()
  - Atomic reclaim() gated by side_effect_classification and retry limits
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.sql import func

from personaos.db.models import Task as TaskModel
from personaos.db.models import Worker as WorkerModel
from personaos.domain.errors import DependencyArtifactResolutionError, WorkerNotEligibleError
from personaos.domain.task import Task
from personaos.domain.worker import Worker, WorkerProfile
from personaos.events.store import emit_task_event
from personaos.execution.adapters.base import Adapter
from personaos.execution.assembler import ExecutionContextAssembler
from personaos.execution.checkpoint import load as checkpoint_load
from personaos.execution.checkpoint import write as checkpoint_write
from personaos.execution.context import WorkerExecutionContext
from personaos.execution.result import Result
from personaos.registry.profile import ProfileRegistry
from personaos.security.approval import request_approval
from personaos.security.policy import PolicyEngine, get_default_policy_engine
from personaos.security.secrets import SecretsManager, get_default_secrets_manager
from personaos.security.tool_policy import ToolPolicy


class TaskStatusError(Exception):
    """Raised when the task is not in an eligible state for the requested operation."""


class StaleWorkerError(Exception):
    """Raised when an operation (complete, fail, heartbeat, checkpoint_write) fails
    because ownership of the task has been transferred (fencing check failed).
    """


ResolveContext = Callable[[Task], Awaitable[WorkerExecutionContext]]


class WorkerExecutionKernel:
    """Orchestrates task execution lifecycle with v0.2 lease and fencing support."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        adapter: Adapter,
        resolve_context: ResolveContext | None = None,
        worker_id: str | None = None,
        node_id: str | None = None,
        profiles_dir: Path | None = None,
        lease_ttl: timedelta = timedelta(minutes=15),
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        heartbeat_interval: float | None = None,
        policy_engine: PolicyEngine | None = None,
        tool_policy: ToolPolicy | None = None,
        secrets_manager: SecretsManager | None = None,
        enforce_policy: bool = True,
    ) -> None:
        self._session = session
        self._session_factory = session_factory
        if self._session_factory is None and getattr(self._session, "bind", None) is not None:
            bind = self._session.bind
            if isinstance(bind, AsyncEngine):
                self._session_factory = async_sessionmaker(bind, expire_on_commit=False)
        self._adapter = adapter
        self._policy_engine = policy_engine or get_default_policy_engine()
        self._tool_policy = tool_policy or ToolPolicy(engine=self._policy_engine)
        self._secrets_manager = secrets_manager or get_default_secrets_manager()
        self._enforce_policy = enforce_policy
        if resolve_context is not None:
            self._resolve_context = resolve_context
        elif profiles_dir is not None:
            reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
            self._resolve_context = reg.resolve_execution_context
        else:

            async def _default_resolve(t: Task) -> WorkerExecutionContext:
                w = Worker(
                    id=self._worker_id or "worker",
                    name="Worker",
                    role="devops",
                    status="active",
                    runtime="openclaw",
                    max_concurrent_tasks=1,
                    current_profile_version=1,
                )
                p = WorkerProfile(
                    version=1,
                    role="devops",
                    runtime="openclaw",
                )
                return WorkerExecutionContext(worker=w, profile=p, task=t)

            self._resolve_context = _default_resolve

        self._worker_id = worker_id
        self._node_id = node_id
        self._lease_ttl = lease_ttl
        self._heartbeat_interval = heartbeat_interval
        self._generation: int = 0

    async def execute(self, task: Task) -> Result:
        """Execute a task end-to-end. Returns the Result."""
        if self._worker_id:
            worker_status_row = await self._session.get(WorkerModel, self._worker_id)
            if worker_status_row and worker_status_row.status in ("quarantined", "retired"):
                msg = (
                    f"Worker {self._worker_id} is in status {worker_status_row.status!r}; "
                    "execution rejected"
                )
                raise WorkerNotEligibleError(msg)

        effective_task = task
        if not effective_task.assigned_worker and self._worker_id:
            # Unassigned task claimed by worker: snapshot current profile version
            worker_row = await self._session.get(WorkerModel, self._worker_id)
            current_ver = (
                worker_row.current_profile_version if worker_row else effective_task.profile_version
            )
            effective_task = dataclasses.replace(
                effective_task,
                assigned_worker=self._worker_id,
                profile_version=current_ver,
            )

        context = await self._resolve_context(effective_task)

        # Atomic claim: queued -> in_progress
        ttl_seconds = int(self._lease_ttl.total_seconds())
        update_vals: dict[str, Any] = {
            "status": "in_progress",
            "started_at": func.now(),
            "generation": TaskModel.generation + 1,
            "lock_expires_at": func.now() + text(f"interval '{ttl_seconds} seconds'"),
        }
        if self._worker_id:
            update_vals["assigned_worker"] = self._worker_id
        if self._node_id:
            update_vals["assigned_node"] = self._node_id
        if effective_task.profile_version != task.profile_version:
            update_vals["profile_version"] = effective_task.profile_version

        where_conditions = [TaskModel.id == task.id, TaskModel.status == "queued"]
        if self._worker_id:
            where_conditions.append(
                (TaskModel.assigned_worker.is_(None))
                | (TaskModel.assigned_worker == self._worker_id)
            )
        if self._node_id:
            where_conditions.append(
                (TaskModel.assigned_node.is_(None)) | (TaskModel.assigned_node == self._node_id)
            )

        try:
            update_result = await self._session.execute(
                update(TaskModel)
                .where(*where_conditions)
                .values(**update_vals)
                .returning(TaskModel.generation, TaskModel.assigned_worker)
            )
            claimed = update_result.first()
            if claimed is None:
                raise TaskStatusError(
                    f"task {task.id} is not claimable (status != 'queued' or affinity mismatch)"
                )
            self._generation = claimed[0]
            if not self._worker_id and claimed[1]:
                self._worker_id = claimed[1]

            await emit_task_event(
                self._session,
                task_id=task.id,
                event_type="task.claimed",
                actor_id=self._worker_id or "kernel",
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise

        # Enrich execution context with upstream results, resolved $dep references, and experience
        assembler = ExecutionContextAssembler(self._session)
        try:
            context = await assembler.enrich(context)
        except DependencyArtifactResolutionError as e:
            result = Result(
                status="failed",
                error=f"Dependency artifact resolution failed: {e}",
            )
            await self.fail(task, result)
            raise

        # Pre-execution security policy evaluation
        if self._enforce_policy:
            action = task.type or "task:execute"
            resource = str(context.task.input.get("resource") or task.id)
            eval_context = {
                "task": context.task,
                "worker": context.worker,
                "profile": context.profile,
                "generation": self._generation,
            }
            decision = self._policy_engine.evaluate(
                actor=context.worker,
                action=action,
                resource=resource,
                context=eval_context,
            )

            if decision.is_denied:
                await emit_task_event(
                    self._session,
                    task_id=task.id,
                    event_type="policy.denied",
                    actor_id=self._worker_id or "security:policy",
                    payload={
                        "policy_id": decision.policy_id,
                        "rule_id": decision.rule_id,
                        "reason": decision.reason,
                        "action": action,
                        "resource": resource,
                    },
                )
                result = Result(
                    status="failed",
                    error=f"Security policy denied execution: {decision.reason}",
                )
                await self.fail(task, result)
                return result

            if decision.requires_approval:
                await request_approval(
                    self._session,
                    task_id=task.id,
                    reason=decision.reason,
                    policy_id=decision.policy_id,
                    rule_id=decision.rule_id,
                    operator_id=self._worker_id or "security:policy",
                    expected_generation=self._generation,
                )
                await self._session.commit()
                return Result(
                    status="failed",
                    error=f"Approval required: {decision.reason}",
                )

            if decision.policy_id:
                await emit_task_event(
                    self._session,
                    task_id=task.id,
                    event_type="policy.allowed",
                    actor_id=self._worker_id or "security:policy",
                    payload={
                        "policy_id": decision.policy_id,
                        "rule_id": decision.rule_id,
                        "action": action,
                        "resource": resource,
                    },
                )
                await self._session.commit()

        # Resolve secret references in task input
        resolved_input = self._secrets_manager.resolve_references(context.task.input)
        adapter_task_input = dataclasses.replace(context.task, input=resolved_input)
        adapter_context = dataclasses.replace(context, task=adapter_task_input)

        checkpoint_state = await checkpoint_load(self._session, task.checkpoint_ref)

        stop_heartbeat = asyncio.Event()
        interval = (
            self._heartbeat_interval
            if self._heartbeat_interval is not None
            else min(60.0, max(0.05, self._lease_ttl.total_seconds() / 3.0))
        )

        async def _heartbeat_loop() -> None:
            while not stop_heartbeat.is_set():
                try:
                    await asyncio.wait_for(stop_heartbeat.wait(), timeout=interval)
                    break
                except TimeoutError:
                    pass
                try:
                    await self.heartbeat(task)
                except StaleWorkerError:
                    raise
                except Exception:
                    pass

        heartbeat_task = asyncio.create_task(_heartbeat_loop())
        try:
            adapter_task = asyncio.create_task(
                self._adapter.execute(context=adapter_context, checkpoint=checkpoint_state)
            )
            done, pending = await asyncio.wait(
                [adapter_task, heartbeat_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                exc = heartbeat_task.exception()
                if exc is not None:
                    adapter_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await adapter_task
                    raise exc
            # Otherwise adapter_task finished
            result = await adapter_task
            if result.output:
                result = dataclasses.replace(
                    result, output=self._secrets_manager.redact(result.output)
                )
            if result.error:
                result = dataclasses.replace(
                    result, error=self._secrets_manager.redact(result.error)
                )
        except StaleWorkerError:
            raise
        except Exception as e:
            result = Result(
                status="failed",
                error=f"{type(e).__name__}: {e}",
            )
        finally:
            stop_heartbeat.set()
            if not heartbeat_task.done():
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task

        if result.status == "completed":
            await self.complete(task, result)
        elif result.status == "failed":
            await self.fail(task, result)
        else:
            raise ValueError(
                f"adapter returned unknown status {result.status!r}; "
                f"expected 'completed' or 'failed'"
            )

        return result

    async def complete(self, task: Task, result: Result) -> None:
        """Fenced completion: persists result + transitions to completed + clears lease."""
        expected_gen = self._generation if self._generation > 0 else getattr(task, "generation", 1)
        expected_worker = self._worker_id or task.assigned_worker

        try:
            checkpoint_ref = task.checkpoint_ref
            if result.new_checkpoint is not None:
                checkpoint_ref = await checkpoint_write(
                    self._session, task_id=task.id, state=result.new_checkpoint
                )
                await emit_task_event(
                    self._session,
                    task_id=task.id,
                    event_type="task.checkpointed",
                    actor_id=expected_worker or "kernel",
                )

            stmt = update(TaskModel).where(
                TaskModel.id == task.id,
                TaskModel.generation == expected_gen,
            )
            if expected_worker:
                stmt = stmt.where(TaskModel.assigned_worker == expected_worker)

            res = await self._session.execute(
                stmt.values(
                    status="completed",
                    completed_at=func.now(),
                    result=result.output,
                    checkpoint_ref=checkpoint_ref,
                    lock_expires_at=None,
                )
            )
            rowcount = (
                res.rowcount if isinstance(res, CursorResult) else getattr(res, "rowcount", 0)
            )
            if rowcount == 0:
                raise StaleWorkerError(f"task {task.id} was reclaimed; write rejected")

            await emit_task_event(
                self._session,
                task_id=task.id,
                event_type="task.completed",
                actor_id=expected_worker or "kernel",
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise

    async def fail(self, task: Task, result: Result) -> None:
        """Fenced failure: persists error + transitions to failed or review + clears lease.

        IMPORTANT for event consumers:
        Consumers must NOT interpret every `task.failed` event as proof that the
        task's final status is 'failed'. When a non-idempotent task ('UNKNOWN' or
        'HAS_SIDE_EFFECTS') encounters an ambiguous in-flight failure, the task is
        quarantined in 'review' status (completed_at is NULL). Due to the PostgreSQL
        CHECK constraint on task_events.type ('task_events_type_check' only permits
        the five v0.1 event types), the kernel emits 'task.failed' with:
            payload={"action": "routed_to_review",
                     "exhaustion_reason": "ambiguous_in_flight_failure"}
        Consumers must check payload.action to distinguish terminal failures from
        tasks quarantined for operator review.
        """
        expected_gen = self._generation if self._generation > 0 else getattr(task, "generation", 1)
        expected_worker = self._worker_id or task.assigned_worker

        is_ambiguous = bool(result.output and result.output.get("ambiguous") is True)
        is_non_idempotent = getattr(task, "side_effect_classification", "UNKNOWN") in (
            "UNKNOWN",
            "HAS_SIDE_EFFECTS",
        )

        if is_ambiguous and is_non_idempotent:
            target_status = "review"
            exhaustion_reason = "ambiguous_in_flight_failure"
            completed_at_val = None
        else:
            target_status = "failed"
            exhaustion_reason = None
            completed_at_val = func.now()

        try:
            stmt = update(TaskModel).where(
                TaskModel.id == task.id,
                TaskModel.generation == expected_gen,
            )
            if expected_worker:
                stmt = stmt.where(TaskModel.assigned_worker == expected_worker)

            result_payload = (
                result.output
                if result.output
                else ({"error": result.error} if result.error else None)
            )
            res = await self._session.execute(
                stmt.values(
                    status=target_status,
                    completed_at=completed_at_val,
                    result=result_payload,
                    last_error=result.error,
                    exhaustion_reason=exhaustion_reason,
                    lock_expires_at=None,
                )
            )
            rowcount = (
                res.rowcount if isinstance(res, CursorResult) else getattr(res, "rowcount", 0)
            )
            if rowcount == 0:
                raise StaleWorkerError(f"task {task.id} was reclaimed; write rejected")

            await emit_task_event(
                self._session,
                task_id=task.id,
                event_type="task.failed",
                actor_id=expected_worker or "kernel",
                payload={"action": "routed_to_review", "exhaustion_reason": exhaustion_reason}
                if exhaustion_reason
                else None,
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise

    async def heartbeat(self, task: Task) -> None:
        """Fenced heartbeat: refreshes lock_expires_at."""
        expected_gen = self._generation if self._generation > 0 else getattr(task, "generation", 1)
        expected_worker = self._worker_id or task.assigned_worker
        ttl_seconds = int(self._lease_ttl.total_seconds())

        async def _do_heartbeat(session: AsyncSession) -> None:
            stmt = update(TaskModel).where(
                TaskModel.id == task.id,
                TaskModel.status == "in_progress",
                TaskModel.generation == expected_gen,
            )
            if expected_worker:
                stmt = stmt.where(TaskModel.assigned_worker == expected_worker)

            res = await session.execute(
                stmt.values(lock_expires_at=func.now() + text(f"interval '{ttl_seconds} seconds'"))
            )
            rowcount = (
                res.rowcount if isinstance(res, CursorResult) else getattr(res, "rowcount", 0)
            )
            if rowcount == 0:
                raise StaleWorkerError(f"task {task.id} was reclaimed; heartbeat rejected")
            await session.commit()

        if self._session_factory is not None:
            async with self._session_factory() as session:
                await _do_heartbeat(session)
        else:
            try:
                await _do_heartbeat(self._session)
            except Exception:
                await self._session.rollback()
                raise

    async def checkpoint_write(self, task: Task, ref: str, state: dict[str, Any]) -> None:
        """Fenced checkpoint write: writes checkpoint state and updates checkpoint_ref."""
        expected_gen = self._generation if self._generation > 0 else getattr(task, "generation", 1)
        expected_worker = self._worker_id or task.assigned_worker

        try:
            await self._session.execute(
                text(
                    "INSERT INTO task_checkpoints (ref, task_id, state) "
                    "VALUES (:ref, :tid, CAST(:state AS jsonb))"
                ),
                {"ref": ref, "tid": task.id, "state": json.dumps(state)},
            )

            stmt = update(TaskModel).where(
                TaskModel.id == task.id,
                TaskModel.status == "in_progress",
                TaskModel.generation == expected_gen,
            )
            if expected_worker:
                stmt = stmt.where(TaskModel.assigned_worker == expected_worker)

            res = await self._session.execute(stmt.values(checkpoint_ref=ref))
            rowcount = (
                res.rowcount if isinstance(res, CursorResult) else getattr(res, "rowcount", 0)
            )
            if rowcount == 0:
                raise StaleWorkerError(f"task {task.id} was reclaimed; checkpoint write rejected")
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise

    async def reclaim(self, task: Task, *, resume_from_checkpoint: bool = False) -> None:
        """Atomic recovery reclaim per ADR 0016 / ADR 0021.

        If resume_from_checkpoint=True, preserves existing checkpoint_ref and loads
        durable checkpoint state to resume adapter execution.
        If resume_from_checkpoint=False (ADR 0016 default), resets checkpoint_ref to NULL
        and starts recovery execution with checkpoint=None.
        """
        row = (
            await self._session.execute(select(TaskModel).where(TaskModel.id == task.id))
        ).scalar_one_or_none()
        if row is None:
            raise TaskStatusError(f"Task {task.id} not found")

        # Side-effect classification safety gate
        if row.side_effect_classification != "IDEMPOTENT":
            raise TaskStatusError(
                f"Task {task.id} classification '{row.side_effect_classification}' "
                "is not IDEMPOTENT; auto-reclaim blocked"
            )

        # Retry exhaustion check
        if row.retry_count >= row.max_retries:
            exhaust_sql = text(
                """
                UPDATE tasks
                SET status = CASE
                      WHEN side_effect_classification = 'IDEMPOTENT' THEN 'failed'
                      WHEN side_effect_classification IN (
                          'UNKNOWN', 'HAS_SIDE_EFFECTS'
                      ) THEN 'review'
                    END,
                    last_error = COALESCE(last_error, 'max retries exceeded after crash recovery'),
                    exhaustion_reason = 'retry_limit_exhausted',
                    completed_at = CASE
                      WHEN side_effect_classification = 'IDEMPOTENT' THEN NOW()
                      ELSE NULL
                    END,
                    lock_expires_at = NULL
                WHERE id = :task_id
                  AND status = 'in_progress'
                  AND generation = :observed_gen
                  AND lock_expires_at < NOW()
                  AND retry_count >= max_retries
                  AND side_effect_classification IN ('IDEMPOTENT', 'UNKNOWN', 'HAS_SIDE_EFFECTS')
                RETURNING id
                """
            )
            res = await self._session.execute(
                exhaust_sql,
                {"task_id": task.id, "observed_gen": row.generation},
            )
            await self._session.commit()
            return

        # Atomic reclaim
        ttl_seconds = int(self._lease_ttl.total_seconds())
        worker_id_val = self._worker_id or "recovery-worker"

        if worker_id_val != "recovery-worker":
            w_row = await self._session.get(WorkerModel, worker_id_val)
            if w_row and w_row.status in ("quarantined", "retired"):
                msg = (
                    f"Worker {worker_id_val} is in status {w_row.status!r}; "
                    "recovery reclaim rejected"
                )
                raise TaskStatusError(msg)

        checkpoint_clause = "" if resume_from_checkpoint else "checkpoint_ref = NULL,"
        node_clause = ", assigned_node = :node_id" if self._node_id else ""
        reclaim_sql = text(
            f"""
            UPDATE tasks
            SET status = 'in_progress',
                assigned_worker = :worker_id
                {node_clause},
                lock_expires_at = NOW() + interval '{ttl_seconds} seconds',
                generation = generation + 1,
                {checkpoint_clause}
                started_at = NOW(),
                retry_count = retry_count + 1
            WHERE id = :task_id
              AND status = 'in_progress'
              AND generation = :observed_gen
              AND lock_expires_at < NOW()
              AND side_effect_classification = 'IDEMPOTENT'
              AND retry_count < max_retries
            RETURNING id, generation, checkpoint_ref
            """
        )
        params: dict[str, Any] = {
            "task_id": task.id,
            "worker_id": worker_id_val,
            "observed_gen": row.generation,
        }
        if self._node_id:
            params["node_id"] = self._node_id

        res = await self._session.execute(reclaim_sql, params)
        claimed_row = res.first()
        if claimed_row is None:
            raise TaskStatusError(f"Task {task.id} could not be reclaimed (raced or not eligible)")

        self._generation = claimed_row[1]
        self._worker_id = worker_id_val
        await self._session.commit()

        # Recovery execution
        if self._adapter is not None:
            context = None
            if self._resolve_context is not None:
                context = await self._resolve_context(task)
                assembler = ExecutionContextAssembler(self._session)
                context = await assembler.enrich(context)
                resolved_input = self._secrets_manager.resolve_references(context.task.input)
                adapter_task = dataclasses.replace(context.task, input=resolved_input)
                context = dataclasses.replace(context, task=adapter_task)

            checkpoint_state = None
            saved_ref = claimed_row[2] if resume_from_checkpoint else None
            if saved_ref:
                checkpoint_state = await checkpoint_load(self._session, saved_ref)

            adapter_res = await self._adapter.execute(context=context, checkpoint=checkpoint_state)
            if adapter_res and adapter_res.output:
                new_ref = saved_ref
                if adapter_res.new_checkpoint is not None:
                    new_ref = await checkpoint_write(
                        self._session, task_id=task.id, state=adapter_res.new_checkpoint
                    )
                    await emit_task_event(
                        self._session,
                        task_id=task.id,
                        event_type="task.checkpointed",
                        actor_id=self._worker_id or "recovery-worker",
                        payload={"checkpoint_ref": new_ref},
                    )

                redacted_output = (
                    self._secrets_manager.redact(adapter_res.output)
                    if adapter_res.output and self._secrets_manager
                    else adapter_res.output
                )
                vals: dict[str, Any] = {"result": redacted_output}
                if new_ref != saved_ref:
                    vals["checkpoint_ref"] = new_ref

                await self._session.execute(
                    update(TaskModel)
                    .where(TaskModel.id == task.id, TaskModel.generation == self._generation)
                    .values(**vals)
                )
                await self._session.commit()
