"""Orca CLI Client.

Provides structured asynchronous subprocess interaction with the Orca CLI
(/root/.config/orca/cli/bin/orca) using documented JSON commands.

Invariants:
- Uses subprocess argument arrays only, never `shell=True`.
- Does not modify or access Orca's internal database directly.
- Communicates purely through Orca's public CLI JSON protocol.
- Normalizes errors into structured PersonaOS exceptions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from personaos.integrations.common.contracts import (
    OrcaGateDescriptor,
    OrcaMessageDescriptor,
    OrcaRunDescriptor,
    OrcaTaskDescriptor,
    WorktreeDescriptor,
)
from personaos.integrations.orca.exceptions import (
    OrcaCLIError,
    OrcaTimeoutError,
    OrcaUnavailableError,
    OrcaWorktreeError,
)

logger = logging.getLogger(__name__)


class OrcaCLIClient:
    """Asynchronous client wrapping the Orca command-line interface."""

    def __init__(
        self,
        orca_binary: str | None = None,
        default_timeout_seconds: float = 30.0,
    ) -> None:
        resolved_bin = orca_binary or os.environ.get("ORCA_BIN")
        if not resolved_bin:
            resolved_bin = shutil.which("orca") or "/root/.config/orca/cli/bin/orca"
        self._orca_binary = resolved_bin
        self._default_timeout = default_timeout_seconds

    @property
    def orca_binary(self) -> str:
        """Path to the configured Orca binary."""
        return self._orca_binary

    def is_available(self) -> bool:
        """Verify whether Orca CLI binary exists and is executable."""
        p = Path(self._orca_binary)
        return p.is_file() and os.access(p, os.X_OK)

    def _get_sanitized_env(self) -> dict[str, str]:
        """Construct a safe environment dictionary without leaking sensitive secrets."""
        allowed_keys = {
            "PATH",
            "HOME",
            "USER",
            "LOGNAME",
            "LANG",
            "LC_ALL",
            "TERM",
            "SHELL",
            "ORCA_ENVIRONMENT",
            "ORCA_PAIRING_CODE",
        }
        env = {k: v for k, v in os.environ.items() if k in allowed_keys}
        # Explicitly remove credentials
        for k in list(env.keys()):
            if any(sec in k.upper() for sec in ("KEY", "SECRET", "TOKEN", "PASS", "AUTH")):
                env.pop(k, None)
        return env

    async def _run_orca_cmd(
        self,
        args: list[str],
        timeout: float | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Execute an Orca CLI command with JSON output and argument arrays."""
        if not self.is_available():
            raise OrcaUnavailableError(
                f"Orca CLI binary not found or not executable at '{self._orca_binary}'"
            )

        cmd = [self._orca_binary, *args]
        if "--json" not in cmd:
            cmd.append("--json")

        timeout_sec = timeout or self._default_timeout
        env = self._get_sanitized_env()

        logger.debug("Executing Orca CLI command: %s (cwd=%s)", " ".join(cmd), cwd)
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(cwd) if cwd else None,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_sec,
            )
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            exit_code = proc.returncode if proc.returncode is not None else 1
        except asyncio.TimeoutError:
            if proc is not None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            raise OrcaTimeoutError(
                f"Orca command timed out after {timeout_sec}s: {' '.join(cmd)}"
            )
        except FileNotFoundError as exc:
            raise OrcaUnavailableError(f"Orca binary '{self._orca_binary}' missing: {exc}") from exc

        # Parse JSON output
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise OrcaCLIError(
                f"Orca CLI returned invalid JSON (exit code {exit_code}): {stdout[:200]} (stderr: {stderr[:200]})",
                exit_code=exit_code,
            ) from exc

        if not isinstance(data, dict):
            raise OrcaCLIError(f"Orca CLI returned non-dict payload: {data}", exit_code=exit_code)

        if not data.get("ok", False):
            err_msg = data.get("error", {}).get("message") or data.get("error") or "Unknown error"
            raise OrcaCLIError(
                f"Orca command failed: {err_msg}",
                exit_code=exit_code,
                error_payload=data,
            )

        return data

    async def get_status(self, timeout: float = 10.0) -> dict[str, Any]:
        """Query `orca status --json` to verify runtime health."""
        data = await self._run_orca_cmd(["status"], timeout=timeout)
        result = data.get("result", {})
        runtime = result.get("runtime", {})
        if runtime.get("state") != "ready" or not runtime.get("reachable", False):
            raise OrcaUnavailableError(
                f"Orca runtime is not ready (state={runtime.get('state')}, reachable={runtime.get('reachable')})"
            )
        return result

    async def list_repos(self) -> list[dict[str, Any]]:
        """Query `orca repo list --json`."""
        data = await self._run_orca_cmd(["repo", "list"])
        return data.get("result", {}).get("repos", [])

    async def get_current_worktree(self, cwd: Path | None = None) -> WorktreeDescriptor:
        """Resolve current Orca worktree via `orca worktree current --json`."""
        try:
            data = await self._run_orca_cmd(["worktree", "current"], cwd=cwd)
        except OrcaCLIError as exc:
            raise OrcaWorktreeError(f"Failed to resolve current worktree: {exc}") from exc

        wt_data = data.get("result", {}).get("worktree")
        if not wt_data:
            raise OrcaWorktreeError(f"No current worktree found for directory {cwd or Path.cwd()}")

        return self._normalize_worktree(wt_data)

    async def list_worktrees(self, repo_id: str | None = None) -> list[WorktreeDescriptor]:
        """List worktrees, optionally filtered by repo."""
        args = ["worktree", "list"]
        if repo_id:
            args.extend(["--repo", f"id:{repo_id}"])
        data = await self._run_orca_cmd(args)
        raw_list = data.get("result", {}).get("worktrees", [])
        return [self._normalize_worktree(wt) for wt in raw_list]

    async def show_worktree(self, selector: str) -> WorktreeDescriptor:
        """Show metadata for a specific worktree selector."""
        data = await self._run_orca_cmd(["worktree", "show", "--worktree", selector])
        wt_data = data.get("result", {}).get("worktree")
        if not wt_data:
            raise OrcaWorktreeError(f"Worktree selector '{selector}' not found")
        return self._normalize_worktree(wt_data)

    def _normalize_worktree(self, raw: dict[str, Any]) -> WorktreeDescriptor:
        """Convert Orca worktree dictionary into a normalized WorktreeDescriptor."""
        return WorktreeDescriptor(
            worktree_id=raw.get("id", ""),
            instance_id=raw.get("instanceId", ""),
            repo_id=raw.get("repoId", ""),
            path=Path(raw.get("path", "")),
            branch=raw.get("branch", ""),
            head=raw.get("head", ""),
            display_name=raw.get("displayName", raw.get("name", "")),
            is_main=raw.get("isMainWorktree", False),
        )

    async def create_terminal(
        self,
        worktree_selector: str,
        command: str,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Create a managed terminal session in a worktree running a specific command."""
        args = [
            "terminal",
            "create",
            "--worktree",
            worktree_selector,
            "--command",
            command,
        ]
        if title:
            args.extend(["--title", title])
        data = await self._run_orca_cmd(args)
        return data.get("result", {}).get("terminal", {})

    async def wait_terminal(
        self,
        terminal_handle: str,
        condition: str = "exit",
        timeout_ms: int = 120_000,
    ) -> dict[str, Any]:
        """Wait for a terminal condition (e.g. 'exit' or 'tui-idle')."""
        args = [
            "terminal",
            "wait",
            "--terminal",
            terminal_handle,
            "--for",
            condition,
            "--timeout-ms",
            str(timeout_ms),
        ]
        # Allow command timeout slightly longer than terminal wait timeout
        cmd_timeout = (timeout_ms / 1000.0) + 10.0
        try:
            return await self._run_orca_cmd(args, timeout=cmd_timeout)
        except OrcaCLIError as exc:
            # Check if it was a wait timeout
            err_code = exc.error_payload.get("error", {}).get("code")
            if err_code == "timeout":
                raise OrcaTimeoutError(f"Terminal wait timed out for {terminal_handle}") from exc
            raise

    async def read_terminal(self, terminal_handle: str) -> list[str]:
        """Read bounded terminal output lines via `orca terminal read`."""
        data = await self._run_orca_cmd(["terminal", "read", "--terminal", terminal_handle])
        tail = data.get("result", {}).get("terminal", {}).get("tail", [])
        return tail if isinstance(tail, list) else []

    async def show_terminal(self, terminal_handle: str) -> dict[str, Any]:
        """Inspect terminal metadata and current preview via `orca terminal show`."""
        data = await self._run_orca_cmd(["terminal", "show", "--terminal", terminal_handle])
        return data.get("result", {}).get("terminal", {})

    async def close_terminal(self, terminal_handle: str) -> bool:
        """Close an Orca terminal session cleanly."""
        try:
            await self._run_orca_cmd(["terminal", "close", "--terminal", terminal_handle])
            return True
        except Exception:
            return False

    async def launch_agent(
        self,
        agent_kind: str,
        worktree_selector: str = "active",
        prompt: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        surface: str = "terminal",
    ) -> dict[str, Any]:
        """Start an agent in a worktree via `orca agent launch`."""
        args = [
            "agent",
            "launch",
            "--agent",
            agent_kind,
            "--worktree",
            worktree_selector,
            "--surface",
            surface,
        ]
        if prompt:
            args.extend(["--prompt", prompt])
        if model:
            args.extend(["--model", model])
        if effort:
            args.extend(["--effort", effort])

        data = await self._run_orca_cmd(args)
        return data.get("result", {})

    # =========================================================================
    # Orca CLI Orchestration Methods (ADR 0040)
    # =========================================================================

    async def has_orchestration_capability(self, timeout: float = 5.0) -> bool:
        """Verify if the resolved Orca binary supports the orchestration CLI suite."""
        try:
            data = await self._run_orca_cmd(["orchestration", "run-list"], timeout=timeout)
            return bool(data.get("ok", False))
        except (OrcaCLIError, OrcaUnavailableError, OrcaTimeoutError):
            return False

    async def orchestration_run_create(
        self,
        objective: str,
        from_handle: str | None = None,
    ) -> OrcaRunDescriptor:
        """Create and bind a lightweight Orca Orchestration Run namespace."""
        args = ["orchestration", "run-create", "--objective", objective]
        if from_handle:
            args.extend(["--from", from_handle])
        data = await self._run_orca_cmd(args)
        result = data.get("result", {})
        run_data = result.get("run", result)
        return OrcaRunDescriptor(
            run_id=run_data.get("id", ""),
            objective=run_data.get("objective", objective),
            coordinator_handle=run_data.get("coordinator_handle"),
            consumer_generation=int(run_data.get("consumer_generation", 0)),
            legacy=bool(run_data.get("legacy", 0)),
            created_at=run_data.get("created_at"),
            updated_at=run_data.get("updated_at"),
        )

    async def orchestration_run_list(self) -> list[OrcaRunDescriptor]:
        """List lightweight orchestration Runs known to Orca."""
        data = await self._run_orca_cmd(["orchestration", "run-list"])
        raw_runs = data.get("result", {}).get("runs", [])
        return [
            OrcaRunDescriptor(
                run_id=r.get("id", ""),
                objective=r.get("objective", ""),
                coordinator_handle=r.get("coordinator_handle"),
                consumer_generation=int(r.get("consumer_generation", 0)),
                legacy=bool(r.get("legacy", 0)),
                created_at=r.get("created_at"),
                updated_at=r.get("updated_at"),
            )
            for r in raw_runs
        ]

    async def orchestration_run_show(self, run_id: str) -> OrcaRunDescriptor:
        """Inspect a specific Orca Orchestration Run."""
        data = await self._run_orca_cmd(["orchestration", "run-show", "--id", run_id])
        result = data.get("result", {})
        run_data = result.get("run", result)
        return OrcaRunDescriptor(
            run_id=run_data.get("id", run_id),
            objective=run_data.get("objective", ""),
            coordinator_handle=run_data.get("coordinator_handle"),
            consumer_generation=int(run_data.get("consumer_generation", 0)),
            legacy=bool(run_data.get("legacy", 0)),
            created_at=run_data.get("created_at"),
            updated_at=run_data.get("updated_at"),
        )

    async def orchestration_task_create(
        self,
        spec: str,
        deps: list[str] | None = None,
        task_title: str | None = None,
        display_name: str | None = None,
        parent_id: str | None = None,
        run_id: str | None = None,
        from_handle: str | None = None,
    ) -> OrcaTaskDescriptor:
        """Create a task within an Orca Orchestration Run."""
        args = ["orchestration", "task-create", "--spec", spec]
        if deps:
            args.extend(["--deps", json.dumps(deps)])
        if task_title:
            args.extend(["--task-title", task_title])
        if display_name:
            args.extend(["--display-name", display_name])
        if parent_id:
            args.extend(["--parent", parent_id])
        if run_id:
            args.extend(["--run", run_id])
        if from_handle:
            args.extend(["--from", from_handle])

        data = await self._run_orca_cmd(args)
        result = data.get("result", {})
        task_data = result.get("task", result)
        return OrcaTaskDescriptor(
            task_id=task_data.get("id", ""),
            spec=task_data.get("spec", spec),
            task_title=task_data.get("taskTitle") or task_data.get("task_title", task_title),
            display_name=task_data.get("displayName") or task_data.get("display_name", display_name),
            status=task_data.get("status", "pending"),
            deps=task_data.get("deps", deps or []),
            parent_id=task_data.get("parentId") or task_data.get("parent_id", parent_id),
            run_id=run_id or result.get("runId"),
        )

    async def orchestration_task_list(
        self,
        run_id: str | None = None,
        ready_only: bool = False,
        brief: bool = False,
    ) -> list[OrcaTaskDescriptor]:
        """List tasks belonging to an Orchestration Run, optionally filtering for ready waves."""
        args = ["orchestration", "task-list"]
        if run_id:
            args.extend(["--run", run_id])
        if ready_only:
            args.append("--ready")
        if brief:
            args.append("--brief")

        data = await self._run_orca_cmd(args)
        result = data.get("result", {})
        raw_tasks = result.get("tasks", [])
        resolved_run_id = run_id or result.get("runId")
        return [
            OrcaTaskDescriptor(
                task_id=t.get("id", ""),
                spec=t.get("spec", ""),
                task_title=t.get("taskTitle") or t.get("task_title"),
                display_name=t.get("displayName") or t.get("display_name"),
                status=t.get("status", "pending"),
                deps=t.get("deps", []),
                parent_id=t.get("parentId") or t.get("parent_id"),
                run_id=resolved_run_id,
            )
            for t in raw_tasks
        ]

    async def orchestration_gate_create(
        self,
        task_id: str,
        question: str,
        options: list[str] | None = None,
        from_handle: str | None = None,
    ) -> OrcaGateDescriptor:
        """Create a decision gate blocking a task in Orca Orchestration."""
        args = [
            "orchestration",
            "gate-create",
            "--task",
            task_id,
            "--question",
            question,
        ]
        if options:
            args.extend(["--options", json.dumps(options)])
        if from_handle:
            args.extend(["--from", from_handle])

        data = await self._run_orca_cmd(args)
        result = data.get("result", {})
        gate_data = result.get("gate", result)
        return OrcaGateDescriptor(
            gate_id=gate_data.get("id", ""),
            task_id=gate_data.get("taskId") or gate_data.get("task_id", task_id),
            question=gate_data.get("question", question),
            options=gate_data.get("options", options or []),
            status=gate_data.get("status", "pending"),
            resolution=gate_data.get("resolution"),
            run_id=gate_data.get("runId") or gate_data.get("run_id"),
        )

    async def orchestration_gate_resolve(
        self,
        gate_id: str,
        resolution: str,
        from_handle: str | None = None,
    ) -> bool:
        """Resolve a pending decision gate."""
        args = [
            "orchestration",
            "gate-resolve",
            "--id",
            gate_id,
            "--resolution",
            resolution,
        ]
        if from_handle:
            args.extend(["--from", from_handle])

        data = await self._run_orca_cmd(args)
        return bool(data.get("ok", False))

    async def orchestration_gate_list(
        self,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> list[OrcaGateDescriptor]:
        """List decision gates for a specific task or Run."""
        args = ["orchestration", "gate-list"]
        if task_id:
            args.extend(["--task", task_id])
        if run_id:
            args.extend(["--run", run_id])

        data = await self._run_orca_cmd(args)
        raw_gates = data.get("result", {}).get("gates", [])
        return [
            OrcaGateDescriptor(
                gate_id=g.get("id", ""),
                task_id=g.get("taskId") or g.get("task_id", task_id or ""),
                question=g.get("question", ""),
                options=g.get("options", []),
                status=g.get("status", "pending"),
                resolution=g.get("resolution"),
                run_id=g.get("runId") or g.get("run_id", run_id),
            )
            for g in raw_gates
        ]

    async def orchestration_send(
        self,
        subject: str,
        to_handle: str | None = None,
        run_id: str | None = None,
        from_handle: str | None = None,
        body: str | None = None,
        msg_type: str = "status",
        priority: str | None = None,
        task_id: str | None = None,
        dispatch_id: str | None = None,
        outcome: str | None = None,
        files_modified: list[str] | None = None,
        report_path: str | None = None,
        phase: str | None = None,
    ) -> dict[str, Any]:
        """Send an inter-agent or coordinator lifecycle message."""
        args = ["orchestration", "send", "--subject", subject, "--type", msg_type]
        if to_handle:
            args.extend(["--to", to_handle])
        if run_id:
            args.extend(["--run", run_id])
        if from_handle:
            args.extend(["--from", from_handle])
        if body:
            args.extend(["--body", body])
        if priority:
            args.extend(["--priority", priority])
        if task_id:
            args.extend(["--task-id", task_id])
        if dispatch_id:
            args.extend(["--dispatch-id", dispatch_id])
        if outcome:
            args.extend(["--outcome", outcome])
        if files_modified:
            args.extend(["--files-modified", ",".join(files_modified)])
        if report_path:
            args.extend(["--report-path", report_path])
        if phase:
            args.extend(["--phase", phase])

        data = await self._run_orca_cmd(args)
        return data.get("result", {})

    async def orchestration_check(
        self,
        terminal_handle: str | None = None,
        run_id: str | None = None,
        ack_delivery_id: str | None = None,
        wait: bool = False,
        timeout_ms: int = 10000,
        types: list[str] | None = None,
        peek: bool = False,
        all_messages: bool = False,
    ) -> dict[str, Any]:
        """Check messages for an agent terminal or coordinator Run inbox."""
        args = ["orchestration", "check"]
        if terminal_handle:
            args.extend(["--terminal", terminal_handle])
        if run_id:
            args.extend(["--run", run_id])
        if ack_delivery_id:
            args.extend(["--ack", ack_delivery_id])
        if wait:
            args.extend(["--wait", "--timeout-ms", str(timeout_ms)])
        if types:
            args.extend(["--types", ",".join(types)])
        if peek:
            args.append("--peek")
        if all_messages:
            args.append("--all")

        cmd_timeout = (timeout_ms / 1000.0) + 5.0 if wait else None
        return await self._run_orca_cmd(args, timeout=cmd_timeout)

    async def orchestration_reply(
        self,
        message_id: str,
        body: str,
        run_id: str | None = None,
        from_handle: str | None = None,
    ) -> bool:
        """Reply to a worker question or coordinator inquiry."""
        args = ["orchestration", "reply", "--id", message_id, "--body", body]
        if run_id:
            args.extend(["--run", run_id])
        if from_handle:
            args.extend(["--from", from_handle])

        data = await self._run_orca_cmd(args)
        return bool(data.get("ok", False))

    async def orchestration_ask(
        self,
        question: str,
        options: list[str] | None = None,
        to_handle: str | None = None,
        run_id: str | None = None,
        from_handle: str | None = None,
        timeout_ms: int = 60000,
    ) -> dict[str, Any]:
        """Ask the coordinator a blocking question via Orca ask."""
        args = ["orchestration", "ask", "--question", question]
        if options:
            args.extend(["--options", ",".join(options)])
        if to_handle:
            args.extend(["--to", to_handle])
        if run_id:
            args.extend(["--run", run_id])
        if from_handle:
            args.extend(["--from", from_handle])
        args.extend(["--timeout-ms", str(timeout_ms)])

        cmd_timeout = (timeout_ms / 1000.0) + 10.0
        return await self._run_orca_cmd(args, timeout=cmd_timeout)
