"""PAB Execution Adapter for PersonaOS.

Implements the Adapter protocol (ADR 0005, ADR 0010, ADR 0038, ADR 0039).
Delegates external agent execution to the PAB Agent Control Plane via out-of-process
subprocess invocation (`pab control ... --json`).

Invariants:
- Adapter conforms to the Adapter Protocol: returns Result (never directly settles DB state).
- Receives frozen WorkerExecutionContext (profile loaded from task.profile_version).
- Subprocess arguments only (no shell=True).
- Generates deterministic idempotency keys and generation fencing flags.
- Environment is scrubbed of cleartext credentials and API keys.
- Failure isolated: PAB unavailability or errors map cleanly to Result(status="failed").
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from personaos.execution.adapters.base import Adapter
from personaos.execution.context import WorkerExecutionContext
from personaos.execution.result import Result

logger = logging.getLogger(__name__)

SAFE_ID_REGEX = re.compile(r"^[a-zA-Z0-9_\-]+$")
SENSITIVE_KEY_PATTERNS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASS",
    "AUTH",
    "BEARER",
    "CREDENTIAL",
    "PRIVATE",
)


@dataclass(frozen=True)
class PABExecutionConfig:
    """Configuration for PABExecutionAdapter."""
    pab_binary: str | None = None
    default_workspace: Path | None = None
    poll_interval_seconds: float = 0.5
    timeout_seconds: float = 120.0
    execution_mode: str = "managed_process"  # 'managed_process' or 'orca_terminal'


class PABExecutionAdapter(Adapter):
    """Execution adapter delegating task execution to PAB control plane."""

    def __init__(self, config: PABExecutionConfig | None = None) -> None:
        self.config = config or PABExecutionConfig()
        resolved_bin = self.config.pab_binary or os.environ.get("PAB_BIN")
        if not resolved_bin:
            resolved_bin = shutil.which("pab") or "/root/.local/bin/pab"
        self._pab_binary = resolved_bin

    def is_available(self) -> bool:
        """Check if PAB binary is accessible on disk or PATH."""
        if not self._pab_binary:
            return False
        p = Path(self._pab_binary)
        return (p.is_file() and os.access(p, os.X_OK)) or bool(shutil.which(self._pab_binary))

    def _sanitize_env(self) -> dict[str, str]:
        """Scrub environment of sensitive API keys and tokens before subprocess launch."""
        env = os.environ.copy()
        for k in list(env.keys()):
            upper_k = k.upper()
            if any(pat in upper_k for pat in SENSITIVE_KEY_PATTERNS):
                env.pop(k, None)
        # Ensure PYTHONPATH includes PAB if running in dev environment
        pab_src = Path("/root/projects/pab/src")
        if pab_src.is_dir():
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = f"{pab_src}:{existing}" if existing else str(pab_src)
        return env

    async def _run_pab_cmd(
        self,
        args: list[str],
        timeout: float = 15.0,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Execute a `pab control ...` command and parse JSON stdout."""
        if not self.is_available():
            raise RuntimeError(f"PAB binary '{self._pab_binary}' is not available.")

        cmd = [self._pab_binary] + args
        env = self._sanitize_env()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd) if cwd else None,
                env=env,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            try:
                proc.kill()
            except Exception:
                pass
            raise TimeoutError(f"PAB command '{cmd[0]} {cmd[1]}' timed out after {timeout}s") from exc

        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            # Try to parse structured error JSON from stdout
            try:
                data = json.loads(stdout_text)
                return data
            except Exception:
                raise RuntimeError(
                    f"PAB command failed (exit code {proc.returncode}): {stderr_text or stdout_text}"
                )

        try:
            return json.loads(stdout_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"PAB returned invalid JSON output: {stdout_text[:200]}"
            ) from exc

    async def list_agents(self) -> list[dict[str, Any]]:
        """Query `pab control list-agents --json`."""
        data = await self._run_pab_cmd(["control", "list-agents", "--json"])
        return data.get("agents", [])

    async def execute(
        self,
        context: WorkerExecutionContext,
        checkpoint: Any | None = None,
    ) -> Result:
        """Execute a task context via PAB Agent Control Plane.

        Steps:
        1. Resolve workspace and provider runtime.
        2. Formulate deterministic idempotency key and launch arguments.
        3. Issue `pab control launch ... --json`.
        4. Poll execution status until terminal state or timeout.
        5. Return normalized Result to PersonaOS WorkerExecutionKernel.
        """
        task = context.task
        worker = context.worker
        profile = context.profile

        provider = getattr(profile, "runtime", None) or "openclaw"
        # If worker requested litellm or local mock, map or pass through
        if provider == "litellm":
            # For litellm requests, PAB minimax/dsh/pi or mock can be targeted
            provider = "minimax" if "minimax" in (task.objective or "") else "mock"

        workspace = (
            self.config.default_workspace
            or Path(task.input.get("workspace", os.getcwd())).resolve()
            if isinstance(task.input, dict)
            else Path(os.getcwd())
        )

        gen = getattr(task, "generation", 1)
        req_id = f"req-{task.id[:8]}-{uuid.uuid4().hex[:6]}"
        idempotency_key = f"idem-{task.id}-gen{gen}"

        # Build prompt from objective and input
        prompt_parts = []
        if task.title:
            prompt_parts.append(f"Task: {task.title}")
        if task.objective:
            prompt_parts.append(f"Objective: {task.objective}")
        if isinstance(task.input, dict) and task.input:
            prompt_parts.append(f"Input: {json.dumps(task.input)}")
        prompt_text = " | ".join(prompt_parts)

        # Build launch arguments
        launch_args = [
            "control",
            "launch",
            "--provider",
            provider,
            "--worker",
            worker.id,
            "--task",
            task.id,
            "--workspace",
            str(workspace),
            "--request-id",
            req_id,
            "--idempotency-key",
            idempotency_key,
            "--generation",
            str(gen),
            "--mode",
            self.config.execution_mode,
            "--timeout",
            str(self.config.timeout_seconds),
            "--json",
        ]
        if prompt_text:
            launch_args.extend(["--prompt", prompt_text])
        if isinstance(task.input, dict) and task.input.get("command"):
            launch_args.extend(["--command", str(task.input["command"])])

        try:
            launch_res = await self._run_pab_cmd(launch_args, timeout=30.0)
        except Exception as exc:
            logger.error("Failed to launch task %s via PAB: %s", task.id, exc)
            return Result(
                status="failed",
                error=f"PAB launch invocation failed: {exc}",
            )

        if launch_res.get("status") == "error":
            return Result(
                status="failed",
                error=f"PAB reported launch error: {launch_res.get('message', 'unknown error')}",
            )

        exec_data = launch_res.get("execution", {})
        execution_id = exec_data.get("execution_id")
        if not execution_id:
            return Result(
                status="failed",
                error="PAB did not return an execution_id",
            )

        # Poll status until terminal state
        start_time = datetime.now(UTC)
        deadline = self.config.timeout_seconds

        while (datetime.now(UTC) - start_time).total_seconds() < deadline:
            await asyncio.sleep(self.config.poll_interval_seconds)
            try:
                status_res = await self._run_pab_cmd(
                    ["control", "status", execution_id, "--json"],
                    timeout=10.0,
                )
            except Exception as poll_exc:
                logger.warning("Transient error polling PAB execution %s: %s", execution_id, poll_exc)
                continue

            current_exec = status_res.get("execution", {})
            st = current_exec.get("status")

            if st == "completed":
                # Retrieve final events for output summary
                output_summary = f"Execution {execution_id} completed successfully via {provider}."
                return Result(
                    status="completed",
                    output={
                        "execution_id": execution_id,
                        "provider": provider,
                        "exit_code": current_exec.get("exit_code", 0),
                        "summary": output_summary,
                        "native_session_id": current_exec.get("native_session_id"),
                        "terminal_handle": current_exec.get("terminal_handle"),
                        "workspace": current_exec.get("workspace"),
                    },
                    new_checkpoint={
                        "execution_id": execution_id,
                        "last_event_sequence": current_exec.get("last_event_sequence", 0),
                        "completed_at": datetime.now(UTC).isoformat(),
                    },
                )
            elif st in ("failed", "cancelled", "interrupted"):
                err_msg = current_exec.get("error_message") or f"Execution reached terminal status '{st}'"
                return Result(
                    status="failed",
                    error=f"PAB execution {st}: {err_msg}",
                    output={
                        "execution_id": execution_id,
                        "provider": provider,
                        "exit_code": current_exec.get("exit_code"),
                    },
                )

        # Timeout reached: attempt to stop execution
        try:
            await self._run_pab_cmd(["control", "stop", execution_id, "--force", "--json"])
        except Exception:
            pass

        return Result(
            status="failed",
            error=f"PAB execution {execution_id} timed out after {deadline}s",
            output={"execution_id": execution_id, "provider": provider},
        )
