"""Step 17 — Security Policy Enforcement integration tests.

Validates end-to-end security contracts against PostgreSQL:
  1. Quarantined worker excluded by WorkloadScheduler
  2. Quarantined worker rejected by WorkerExecutionKernel
  3. Pre-execution policy ALLOW emits policy.allowed and completes execution
  4. Pre-execution policy DENY fails task immediately and emits policy.denied
  5. Pre-execution policy REQUIRE_APPROVAL routes task to review with approval.requested
  6. Operator approval resumes gated task back to queued
  7. Operator rejection terminally fails gated task
  8. Secret references resolved at execution time and redacted from result/errors
  9. Tool policy allow/deny enforcement
  10. Generation-fenced approval prevents stale writes
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from personaos.db.models import Task as TaskModel
from personaos.db.models import Worker as WorkerModel
from personaos.domain.errors import (
    TaskStateConflictError,
    WorkerNotEligibleError,
)
from personaos.domain.task import Task
from personaos.execution.adapters.base import Result
from personaos.execution.context import WorkerExecutionContext
from personaos.execution.kernel import WorkerExecutionKernel
from personaos.security.approval import approve_approval, reject_approval
from personaos.security.policy import Effect, Policy, PolicyEngine, Rule
from personaos.security.quarantine import unquarantine_worker
from personaos.security.secrets import SecretsManager
from personaos.security.tool_policy import ToolPolicy
from personaos.workload.scheduler import WorkloadScheduler

REPO_ROOT = Path(__file__).parents[2]
ALEMBIC_INI = REPO_ROOT / "src" / "personaos" / "db" / "migrations" / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "src" / "personaos" / "db" / "migrations"

pytestmark = pytest.mark.usefixtures("clean_tables")


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


class MockAdapter:
    """Mock execution adapter recording received context."""

    def __init__(self, result: Result | None = None) -> None:
        self.result = result or Result(status="completed", output={"msg": "success"})
        self.last_context: WorkerExecutionContext | None = None
        self.executed_count: int = 0

    async def execute(
        self,
        context: WorkerExecutionContext,
        checkpoint: Any | None,
    ) -> Result:
        self.last_context = context
        self.executed_count += 1
        return self.result


# ---------------------------------------------------------------------------
# Test 1 & 2: Worker Quarantine
# ---------------------------------------------------------------------------


async def test_quarantined_worker_excluded_by_scheduler(database_url, session_factory):
    """Scheduler must not match tasks to quarantined workers."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        # Create worker in quarantined status
        worker = WorkerModel(
            id="worker-quarantined",
            name="Quarantined Worker",
            role="devops",
            status="quarantined",
        )
        session.add(worker)
        await session.flush()

        # Enqueue runnable task assigned to this worker
        task = TaskModel(
            id="task-q-1",
            title="Quarantine Test Task",
            objective="Verify exclusion",
            status="queued",
            priority="high",
            assigned_worker="worker-quarantined",
            input={},
        )
        session.add(task)
        await session.commit()

        scheduler = WorkloadScheduler(session)
        candidate = await scheduler.find_candidate("worker-quarantined")
        assert candidate is None

        # Unquarantine worker
        await unquarantine_worker(session, worker_id="worker-quarantined", reason="cleared")
        await session.commit()

        # Candidate now found
        candidate_after = await scheduler.find_candidate("worker-quarantined")
        assert candidate_after is not None
        assert candidate_after.id == "task-q-1"


async def test_quarantined_worker_rejected_by_kernel(database_url, session_factory):
    """WorkerExecutionKernel must reject execution when worker is quarantined."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        worker = WorkerModel(
            id="worker-bad",
            name="Bad Worker",
            role="devops",
            status="quarantined",
        )
        session.add(worker)
        await session.flush()
        task = TaskModel(
            id="task-q-2",
            title="Kernel Quarantine Task",
            objective="Verify rejection",
            status="queued",
            priority="medium",
            assigned_worker="worker-bad",
            input={},
        )
        session.add(task)
        await session.commit()

        adapter = MockAdapter()
        kernel = WorkerExecutionKernel(
            session=session,
            adapter=adapter,
            worker_id="worker-bad",
        )

        domain_task = Task.from_orm(task)
        with pytest.raises(WorkerNotEligibleError, match="quarantined"):
            await kernel.execute(domain_task)

        assert adapter.executed_count == 0

        # Verify task remains in 'queued' state
        res = await session.execute(select(TaskModel.status).where(TaskModel.id == "task-q-2"))
        assert res.scalar_one() == "queued"


# ---------------------------------------------------------------------------
# Test 3 & 4: Policy Allow & Deny
# ---------------------------------------------------------------------------


async def test_policy_allowed_execution_emits_event(database_url, session_factory):
    """Permitted task executes cleanly and emits policy.allowed event."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    policy = Policy(
        id="pol-allow",
        name="Allow Policy",
        priority=10,
        rules=[Rule(id="rule-allow", action="task:test", resource="*", effect=Effect.ALLOW)],
        default_effect=Effect.DENY,
    )
    engine = PolicyEngine([policy])

    async with session_factory() as session:
        worker = WorkerModel(
            id="worker-allowed",
            name="Allowed Worker",
            role="devops",
            status="idle",
        )
        session.add(worker)
        await session.flush()
        task = TaskModel(
            id="task-allow-1",
            title="Allowed Task",
            objective="Passes policy",
            status="queued",
            priority="medium",
            type="task:test",
            assigned_worker="worker-allowed",
            input={},
        )
        session.add(task)
        await session.commit()

        adapter = MockAdapter()
        kernel = WorkerExecutionKernel(
            session=session,
            adapter=adapter,
            worker_id="worker-allowed",
            policy_engine=engine,
        )

        res = await kernel.execute(Task.from_orm(task))
        assert res.status == "completed"
        assert adapter.executed_count == 1

        # Check policy.allowed event in task_events
        events = (
            await session.execute(
                text(
                    "SELECT type, actor_id, payload FROM task_events "
                    "WHERE task_id = 'task-allow-1' ORDER BY created_at ASC"
                )
            )
        ).fetchall()
        event_types = [e[0] for e in events]
        assert "task.claimed" in event_types
        assert "policy.allowed" in event_types
        assert "task.completed" in event_types


async def test_policy_denied_execution_fails_task_and_emits_event(database_url, session_factory):
    """Denied task immediately fails, skips adapter, and emits policy.denied event."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    policy = Policy(
        id="pol-deny",
        name="Deny Policy",
        priority=100,
        rules=[Rule(id="rule-deny", action="task:destroy", resource="*", effect=Effect.DENY)],
        default_effect=Effect.ALLOW,
    )
    engine = PolicyEngine([policy])

    async with session_factory() as session:
        worker = WorkerModel(
            id="worker-denied",
            name="Denied Worker",
            role="devops",
            status="idle",
        )
        session.add(worker)
        await session.flush()
        task = TaskModel(
            id="task-deny-1",
            title="Destructive Task",
            objective="Must be blocked",
            status="queued",
            priority="critical",
            type="task:destroy",
            assigned_worker="worker-denied",
            input={},
        )
        session.add(task)
        await session.commit()

        adapter = MockAdapter()
        kernel = WorkerExecutionKernel(
            session=session,
            adapter=adapter,
            worker_id="worker-denied",
            policy_engine=engine,
        )

        res = await kernel.execute(Task.from_orm(task))
        assert res.status == "failed"
        assert "Security policy denied" in (res.error or "")
        assert adapter.executed_count == 0

        # Check task state in database
        row = (
            await session.execute(
                select(TaskModel.status, TaskModel.last_error).where(TaskModel.id == "task-deny-1")
            )
        ).first()
        assert row[0] == "failed"
        assert "Security policy denied" in row[1]

        # Check policy.denied event
        event_row = (
            await session.execute(
                text(
                    "SELECT type, payload FROM task_events "
                    "WHERE task_id = 'task-deny-1' AND type = 'policy.denied'"
                )
            )
        ).first()
        assert event_row is not None
        assert event_row[1]["rule_id"] == "rule-deny"


# ---------------------------------------------------------------------------
# Test 5, 6, 7: Approval Gate Lifecycle
# ---------------------------------------------------------------------------


async def test_policy_require_approval_and_operator_resolution(database_url, session_factory):
    """Require approval pauses task in review; operator approval re-enqueues."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    policy = Policy(
        id="pol-approval",
        name="Approval Policy",
        priority=50,
        rules=[
            Rule(
                id="gate-prod",
                action="task:deploy:prod",
                resource="*",
                effect=Effect.REQUIRE_APPROVAL,
            )
        ],
        default_effect=Effect.DENY,
    )
    engine = PolicyEngine([policy])

    async with session_factory() as session:
        worker = WorkerModel(
            id="worker-operator",
            name="Worker",
            role="devops",
            status="idle",
        )
        session.add(worker)
        await session.flush()
        task = TaskModel(
            id="task-gate-1",
            title="Deploy Prod",
            objective="Production deploy",
            status="queued",
            priority="high",
            type="task:deploy:prod",
            assigned_worker="worker-operator",
            input={},
        )
        session.add(task)
        await session.commit()

        adapter = MockAdapter()
        kernel = WorkerExecutionKernel(
            session=session,
            adapter=adapter,
            worker_id="worker-operator",
            policy_engine=engine,
        )

        res = await kernel.execute(Task.from_orm(task))
        assert res.status == "failed"
        assert "Approval required" in (res.error or "")
        assert adapter.executed_count == 0

        # Task must be in 'review' status with exhaustion_reason = 'approval_required'
        task_db = (
            await session.execute(select(TaskModel).where(TaskModel.id == "task-gate-1"))
        ).scalar_one()
        assert task_db.status == "review"
        assert task_db.exhaustion_reason == "approval_required"
        assert task_db.lock_expires_at is None
        gen_after_gate = task_db.generation

        # Verify approval.requested event emitted
        event_req = (
            await session.execute(
                text(
                    "SELECT type, payload FROM task_events "
                    "WHERE task_id = 'task-gate-1' AND type = 'approval.requested'"
                )
            )
        ).first()
        assert event_req is not None
        assert event_req[1]["rule_id"] == "gate-prod"

        # Stale generation check
        with pytest.raises(TaskStateConflictError, match="generation mismatch"):
            await approve_approval(
                session,
                task_id="task-gate-1",
                operator_id="operator:alice",
                expected_generation=gen_after_gate - 1,
            )

        # Valid operator approval returns task to 'queued'
        approved_task = await approve_approval(
            session,
            task_id="task-gate-1",
            operator_id="operator:alice",
            expected_generation=gen_after_gate,
            reason="Approved after security review",
        )
        await session.commit()

        assert approved_task.status == "queued"
        assert approved_task.generation == gen_after_gate + 1
        assert approved_task.exhaustion_reason is None


async def test_operator_rejection_fails_gated_task(database_url, session_factory):
    """Operator rejection transitions approval-gated task to 'failed'."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        worker = WorkerModel(id="w-rej", name="W", role="devops", status="idle")
        session.add(worker)
        await session.flush()
        task = TaskModel(
            id="task-rej-1",
            title="Gated Task",
            objective="Will be rejected",
            status="review",
            priority="medium",
            exhaustion_reason="approval_required",
            assigned_worker="w-rej",
            generation=3,
            input={},
        )
        session.add(task)
        await session.commit()

        rejected = await reject_approval(
            session,
            task_id="task-rej-1",
            operator_id="operator:bob",
            expected_generation=3,
            reason="Security review rejected this production push",
        )
        await session.commit()

        assert rejected.status == "failed"
        assert rejected.exhaustion_reason == "operator_rejected"
        assert rejected.last_error == "Security review rejected this production push"


# ---------------------------------------------------------------------------
# Test 8: Secrets Resolution and Redaction
# ---------------------------------------------------------------------------


async def test_secrets_resolution_and_output_redaction(database_url, session_factory, monkeypatch):
    """Secret references in input are resolved for adapter and redacted from output/errors."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    secrets_mgr = SecretsManager({"DEPLOY_TOKEN": "my-ultra-secret-token-12345"})
    policy = Policy(id="allow-all", name="Allow", default_effect=Effect.ALLOW)
    engine = PolicyEngine([policy])

    async with session_factory() as session:
        worker = WorkerModel(id="w-sec", name="Sec Worker", role="devops", status="idle")
        session.add(worker)
        await session.flush()
        task = TaskModel(
            id="task-sec-1",
            title="Secrets Task",
            objective="Verify secrets handling",
            status="queued",
            priority="medium",
            assigned_worker="w-sec",
            input={"api_token": "secret:DEPLOY_TOKEN", "cluster": "prod-1"},
        )
        session.add(task)
        await session.commit()

        # Adapter returns output containing the raw secret string
        adapter = MockAdapter(
            result=Result(
                status="completed",
                output={"log": "Connected with token my-ultra-secret-token-12345 successfully"},
            )
        )

        kernel = WorkerExecutionKernel(
            session=session,
            adapter=adapter,
            worker_id="w-sec",
            policy_engine=engine,
            secrets_manager=secrets_mgr,
        )

        res = await kernel.execute(Task.from_orm(task))
        assert res.status == "completed"

        # 1. Adapter received resolved token
        assert adapter.last_context is not None
        assert adapter.last_context.task.input["api_token"] == "my-ultra-secret-token-12345"

        # 2. Persisted result has secret redacted
        row = (
            await session.execute(select(TaskModel.result).where(TaskModel.id == "task-sec-1"))
        ).scalar_one()
        assert "my-ultra-secret-token-12345" not in row["log"]
        assert "[REDACTED_SECRET]" in row["log"]


# ---------------------------------------------------------------------------
# Test 9: Tool Policy Enforcement
# ---------------------------------------------------------------------------


def test_tool_policy_capabilities_and_policies():
    """ToolPolicy checks worker capability allowlist and policy rules."""
    policy = Policy(
        id="tool-pol",
        name="Tool Rules",
        rules=[
            Rule(id="allow-k8s", action="tool:kubectl", resource="staging*", effect=Effect.ALLOW),
            Rule(id="deny-k8s-prod", action="tool:kubectl", resource="prod*", effect=Effect.DENY),
        ],
        default_effect=Effect.DENY,
    )
    engine = PolicyEngine([policy])
    tp = ToolPolicy(engine=engine)

    worker = {"id": "w1", "capabilities": ["kubectl"]}

    # Allowed on staging
    d_staging = tp.check(worker, "kubectl", resource="staging-cluster")
    assert d_staging.is_allowed

    # Denied on prod
    d_prod = tp.check(worker, "kubectl", resource="prod-cluster")
    assert d_prod.is_denied

    # Denied due to missing capability (bash not in worker capabilities)
    d_bash = tp.check(worker, "bash", resource="staging-cluster")
    assert d_bash.is_denied
    assert "not granted in worker capabilities" in d_bash.reason
