"""Orca Execution Adapter.

Bridges PersonaOS worker task execution requests to Orca-managed worktrees,
agent CLIs, and terminals.

Invariants:
- PersonaOS kernel retains sole ownership of task state, leasing, and completion.
- Adapter does NOT update PostgreSQL directly or modify task lifecycle state.
- Orca native session IDs and terminal handles are stored as opaque references.
- Security policies and permission boundaries from PersonaOS are respected.
- Duplicate executions of the same request_id are prevented.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from personaos.integrations.common.contracts import (
    ExecutionEvent,
    ExecutionHandle,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
    WorktreeDescriptor,
)
from personaos.integrations.orca.client import OrcaCLIClient
from personaos.integrations.orca.exceptions import (
    OrcaCLIError,
    OrcaError,
    OrcaExecutionFailedError,
    OrcaTimeoutError,
    OrcaUnavailableError,
    OrcaWorktreeError,
)

logger = logging.getLogger(__name__)


class OrcaExecutionAdapter:
    """Thin integration adapter executing PersonaOS requests inside Orca."""

    def __init__(
        self,
        client: OrcaCLIClient | None = None,
        default_worktree: str | None = None,
    ) -> None:
        self._client = client or OrcaCLIClient()
        self._default_worktree = default_worktree
        self._active_executions: dict[str, ExecutionHandle] = {}
        self._lock = asyncio.Lock()

    @property
    def client(self) -> OrcaCLIClient:
        """Configured Orca CLI client."""
        return self._client

    async def resolve_worktree(
        self,
        target_worktree: str | None = None,
        cwd: Path | None = None,
    ) -> WorktreeDescriptor:
        """Resolve target worktree from explicit selector, default, or active directory."""
        selector = target_worktree or self._default_worktree

        if selector and selector != "active" and selector != "current":
            # If selector looks like a path
            p = Path(selector)
            if p.is_dir():
                return await self._client.get_current_worktree(cwd=p)
            return await self._client.show_worktree(selector)

        # Fallback to active/current worktree
        return await self._client.get_current_worktree(cwd=cwd)

    async def execute(
        self,
        request: ExecutionRequest,
        event_callback: Callable[[ExecutionEvent], None] | None = None,
    ) -> ExecutionResult:
        """Execute a task request in Orca and return normalized ExecutionResult.

        Execution sequence:
        1. Duplicate execution guard.
        2. Resolve target worktree.
        3. Emit PREPARING event.
        4. Launch agent or command in managed terminal.
        5. Capture native execution references (ExecutionHandle).
        6. Monitor execution until completion or timeout.
        7. Collect output evidence and emit COMPLETED / FAILED event.
        8. Return normalized ExecutionResult.
        """
        async with self._lock:
            if request.request_id in self._active_executions:
                raise OrcaError(
                    f"Duplicate execution rejected: request {request.request_id} is already in-flight"
                )

        started_at = datetime.now(UTC)
        handle_id = f"exec-{uuid.uuid4().hex[:12]}"

        def emit(status: ExecutionStatus, msg: str, details: dict[str, Any] | None = None) -> None:
            if event_callback:
                ev = ExecutionEvent(
                    event_id=f"ev-{uuid.uuid4().hex[:12]}",
                    handle_id=handle_id,
                    task_id=request.task_id,
                    worker_id=request.worker_id,
                    status=status,
                    timestamp=datetime.now(UTC),
                    message=msg,
                    details=details or {},
                )
                try:
                    event_callback(ev)
                except Exception as ex:
                    logger.debug("Error in execution event callback: %s", ex)

        # Step 1: Resolve Worktree
        try:
            worktree = await self.resolve_worktree(request.target_worktree)
        except Exception as exc:
            logger.error("Worktree resolution failed for task %s: %s", request.task_id, exc)
            completed_at = datetime.now(UTC)
            handle = ExecutionHandle(
                handle_id=handle_id,
                request_id=request.request_id,
                worktree_id="unresolved",
                started_at=started_at,
                agent=request.agent,
            )
            return ExecutionResult(
                request_id=request.request_id,
                handle=handle,
                status=ExecutionStatus.FAILED,
                exit_code=1,
                started_at=started_at,
                completed_at=completed_at,
                duration_seconds=(completed_at - started_at).total_seconds(),
                output_summary="",
                error_message=f"Worktree resolution error: {exc}",
            )

        emit(ExecutionStatus.PREPARING, f"Resolved worktree {worktree.display_name} ({worktree.worktree_id})")

        # Step 2: Provision execution handle
        handle = ExecutionHandle(
            handle_id=handle_id,
            request_id=request.request_id,
            worktree_id=worktree.worktree_id,
            started_at=started_at,
            agent=request.agent,
        )

        async with self._lock:
            self._active_executions[request.request_id] = handle

        terminal_handle: str | None = None
        native_session_id: str | None = None

        try:
            emit(ExecutionStatus.RUNNING, f"Starting execution with agent {request.agent.kind}")

            # Step 3: Launch agent or command
            # If a custom or mock command is provided, create a managed terminal
            if request.command or request.agent.kind in ("mock", "custom"):
                cmd = request.command or "echo 'Mock agent completed successfully'"
                terminal_cmd = f"({cmd})\nexit $?"
                term_data = await self._client.create_terminal(
                    worktree_selector=worktree.worktree_id,
                    command=terminal_cmd,
                    title=f"personaos-{request.worker_id}-{request.task_id[:8]}",
                )
                terminal_handle = term_data.get("handle")
            else:
                # Standard Orca agent launch (codex, claude, agy, openclaw)
                launch_data = await self._client.launch_agent(
                    agent_kind=request.agent.kind,
                    worktree_selector=worktree.worktree_id,
                    prompt=request.prompt,
                    model=request.agent.model,
                    effort=request.agent.effort,
                )
                terminal_handle = launch_data.get("terminal", {}).get("handle")
                native_session_id = launch_data.get("session", {}).get("id")

            # Update handle with native identifiers
            handle = ExecutionHandle(
                handle_id=handle_id,
                request_id=request.request_id,
                worktree_id=worktree.worktree_id,
                started_at=started_at,
                agent=request.agent,
                native_session_id=native_session_id,
                native_terminal_id=terminal_handle,
            )

            # Step 4: Monitor terminal execution
            exit_code = 0
            raw_output = ""
            status = ExecutionStatus.COMPLETED
            err_msg: str | None = None

            if terminal_handle:
                timeout_ms = int(request.timeout_seconds * 1000)
                try:
                    # Wait for terminal process completion
                    wait_data = await self._client.wait_terminal(
                        terminal_handle=terminal_handle,
                        condition="exit",
                        timeout_ms=timeout_ms,
                    )
                    wait_info = wait_data.get("result", {}).get("wait", {}) if isinstance(wait_data, dict) else {}
                    reported_exit = wait_info.get("exitCode")
                    if reported_exit is not None:
                        exit_code = int(reported_exit)
                        if exit_code != 0:
                            status = ExecutionStatus.FAILED
                            err_msg = f"Terminal process exited with code {exit_code}"
                except OrcaTimeoutError as exc:
                    status = ExecutionStatus.TIMEOUT
                    err_msg = str(exc)
                    exit_code = 124

                # Read final terminal output
                try:
                    lines = await self._client.read_terminal(terminal_handle)
                    if lines:
                        raw_output = "\n".join(lines)
                except Exception:
                    pass

                # If read_terminal yielded no lines (e.g. process exited and stream closed),
                # inspect terminal preview buffer from show_terminal
                if not raw_output:
                    try:
                        show_data = await self._client.show_terminal(terminal_handle)
                        raw_output = show_data.get("preview", "")
                    except Exception:
                        pass

                # Inspect output for error indications if not timeout
                if status != ExecutionStatus.TIMEOUT and status != ExecutionStatus.FAILED:
                    if "Traceback (most recent call last):" in raw_output or "Command failed with exit code" in raw_output:
                        status = ExecutionStatus.FAILED
                        exit_code = 1
                        err_msg = "Agent process reported failure in terminal output"

            completed_at = datetime.now(UTC)
            duration = (completed_at - started_at).total_seconds()
            summary = raw_output[-500:] if raw_output else "Execution completed."

            emit(status, f"Execution {status.value} (exit_code={exit_code})")

            return ExecutionResult(
                request_id=request.request_id,
                handle=handle,
                status=status,
                exit_code=exit_code,
                started_at=started_at,
                completed_at=completed_at,
                duration_seconds=duration,
                output_summary=summary,
                raw_terminal_output=raw_output,
                error_message=err_msg,
                metadata={"worktree": worktree.to_dict()},
            )

        except OrcaUnavailableError as exc:
            completed_at = datetime.now(UTC)
            emit(ExecutionStatus.FAILED, f"Orca unavailable: {exc}")
            return ExecutionResult(
                request_id=request.request_id,
                handle=handle,
                status=ExecutionStatus.FAILED,
                exit_code=127,
                started_at=started_at,
                completed_at=completed_at,
                duration_seconds=(completed_at - started_at).total_seconds(),
                output_summary="",
                error_message=f"Orca unavailable: {exc}",
            )
        except Exception as exc:
            completed_at = datetime.now(UTC)
            emit(ExecutionStatus.FAILED, f"Execution failed: {exc}")
            return ExecutionResult(
                request_id=request.request_id,
                handle=handle,
                status=ExecutionStatus.FAILED,
                exit_code=1,
                started_at=started_at,
                completed_at=completed_at,
                duration_seconds=(completed_at - started_at).total_seconds(),
                output_summary="",
                error_message=str(exc),
            )
        finally:
            # Clean up terminal if created
            if terminal_handle:
                try:
                    await self._client.close_terminal(terminal_handle)
                except Exception:
                    pass

            async with self._lock:
                self._active_executions.pop(request.request_id, None)
