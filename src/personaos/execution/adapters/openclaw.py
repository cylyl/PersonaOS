"""OpenClaw runtime adapter — live JSON-RPC 2.0 stdio bridge.

Per ADR 0005: thin runtime adapter translating between OpenClaw ACP protocol
and PersonaOS domain types. Zero domain logic, zero direct database access.

Per ADR 0009: receives frozen WorkerExecutionContext (worker + snapshot profile + task).
Per ADR 0010: returns Result. Kernel owns state machine transitions and leases.
Per ADR 0017: implements Agent Client Protocol (ACP) JSON-RPC 2.0 framing over stdio
with bounded cleanup, fail-fast protocol validation, credential redaction, and
conservative ambiguous-outcome reporting for non-idempotent task safety.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from personaos.execution.context import WorkerExecutionContext
from personaos.execution.result import Result

if TYPE_CHECKING:
    from personaos.execution.adapters.base import Adapter

logger = structlog.get_logger(__name__)
std_logger = logging.getLogger(__name__)

# Protocol framing constants per ADR 0017 & docs/specs/live-openclaw-runtime-adapter.md
MAX_MESSAGE_BYTES: int = 16 * 1024 * 1024  # 16 MiB line limit
MAX_CONCURRENT_REQUESTS: int = 16


class OpenClawProtocolError(Exception):
    """Raised when JSON-RPC framing or protocol contract is violated."""


@dataclass(frozen=True)
class OpenClawConfig:
    """Configuration for OpenClaw runtime adapter."""

    command: tuple[str, ...] = ("openclaw", "acp")
    gateway_url: str = "ws://127.0.0.1:18789"
    token: str | None = None
    token_file: Path | None = None
    timeout_seconds: float = 300.0
    cwd: Path | None = None
    env: dict[str, str] = field(default_factory=dict)


def _redact_credentials(text: str, token: str | None = None) -> str:
    """Redacts sensitive credentials from log and error strings.

    Never exposes auth tokens in diagnostics, logs, or error returns.
    """
    if not text:
        return text
    if token and token in text:
        text = text.replace(token, "[REDACTED]")
    text = re.sub(r"secret_token_[a-zA-Z0-9]+", "[REDACTED]", text)
    text = re.sub(r"\b[a-f0-9]{32,64}\b", "[REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(r"(token[\s:=]+)(\S+)", r"\1[REDACTED]", text, flags=re.IGNORECASE)
    return text


def _extract_chunk_text(update: dict[str, Any]) -> str:
    """Extracts streaming text content from ACP session/update payloads."""
    content = update.get("content")
    if isinstance(content, dict):
        return str(content.get("text", ""))
    if isinstance(content, list):
        return "".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    if isinstance(content, str):
        return content
    if "text" in update:
        return str(update["text"])
    return ""


class _OpenClawProcessRunner:
    """Internal runner managing the lifecycle and JSON-RPC transport of an OpenClaw subprocess."""

    def __init__(
        self,
        config: OpenClawConfig,
        context: WorkerExecutionContext,
        checkpoint: Any | None = None,
    ) -> None:
        self.config = config
        self.context = context
        self.checkpoint = checkpoint
        self.proc: asyncio.subprocess.Process | None = None
        self._pending_requests: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id: int = 1
        self._accumulated_chunks: list[str] = []
        self._phase: str = "pre_prompt"
        self._token_path: Path | None = None
        self._temp_dir: Path | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._protocol_error: Exception | None = None

    def _redact(self, text: str) -> str:
        return _redact_credentials(text, token=self.config.token)

    async def _setup_token_file(self) -> None:
        """Sets up token authentication file with restrictive 0600 permissions."""
        if self.config.token_file is not None:
            self._token_path = self.config.token_file
            return
        if self.config.token:
            temp_dir = Path(tempfile.mkdtemp(prefix="openclaw_token_"))
            os.chmod(temp_dir, 0o700)
            token_file = temp_dir / "gateway.token"
            fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, self.config.token.encode("utf-8"))
                os.fdatasync(fd)
            finally:
                os.close(fd)
            self._token_path = token_file
            self._temp_dir = temp_dir

    def _cleanup_token_file(self) -> None:
        """Best-effort zero-overwrite and removal of temporary token file.

        Note: On journaling and copy-on-write filesystems or wear-leveling SSDs,
        zero-overwriting before unlink provides defense-in-depth rather than a
        cryptographic hardware-level shred guarantee.
        """
        if self._token_path and self._temp_dir and self._token_path.exists():
            try:
                size = self._token_path.stat().st_size
                if size > 0:
                    fd = os.open(self._token_path, os.O_WRONLY)
                    try:
                        os.write(fd, b"\x00" * size)
                        os.fdatasync(fd)
                    finally:
                        os.close(fd)
                self._token_path.unlink(missing_ok=True)
            except Exception as e:
                logger.warning("token_file_shred_failed", error=str(e))
                self._token_path.unlink(missing_ok=True)
        if self._temp_dir and self._temp_dir.exists():
            try:
                self._temp_dir.rmdir()
            except Exception as e:
                logger.warning("token_dir_remove_failed", error=str(e))

    def _fail_all_pending(self, exc: Exception) -> None:
        if self._protocol_error is None:
            self._protocol_error = exc
        for fut in list(self._pending_requests.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending_requests.clear()

    async def _stdout_reader_loop(self) -> None:
        """Reads and dispatches newline-delimited JSON-RPC frames from stdout."""
        assert self.proc is not None
        assert self.proc.stdout is not None
        try:
            while True:
                line_bytes = await self.proc.stdout.readline()
                if not line_bytes:
                    err: Exception = BrokenPipeError(
                        "OpenClaw stdout closed unexpectedly (process EOF)"
                    )
                    self._fail_all_pending(err)
                    break

                if len(line_bytes) > MAX_MESSAGE_BYTES:
                    err = OpenClawProtocolError(
                        f"Frame exceeded line limit ({len(line_bytes)} > {MAX_MESSAGE_BYTES})"
                    )
                    self._fail_all_pending(err)
                    break

                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                try:
                    msg = json.loads(line)
                except Exception as e:
                    snippet = self._redact(line[:120])
                    err = OpenClawProtocolError(
                        f"Malformed non-JSON frame received from stdout: {snippet} ({e})"
                    )
                    self._fail_all_pending(err)
                    break

                if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
                    err = OpenClawProtocolError(
                        f"Frame missing valid jsonrpc 2.0 version: {self._redact(line[:120])}"
                    )
                    self._fail_all_pending(err)
                    break

                # Response correlation by request id
                if "id" in msg:
                    req_id = msg["id"]
                    if isinstance(req_id, int) and req_id in self._pending_requests:
                        fut = self._pending_requests.pop(req_id)
                        if not fut.done():
                            fut.set_result(msg)
                    else:
                        logger.debug("uncorrelated_openclaw_response", id=req_id)

                # Unsolicited notifications
                elif "method" in msg:
                    method = msg["method"]
                    params = msg.get("params", {})
                    if method == "session/update":
                        update_payload = params.get("update", {})
                        chunk_text = _extract_chunk_text(update_payload)
                        if chunk_text:
                            self._accumulated_chunks.append(chunk_text)
                    else:
                        logger.debug("unhandled_openclaw_notification", method=method)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._fail_all_pending(e)

    async def _stderr_reader_loop(self) -> None:
        """Sanitizes diagnostic lines from stderr and logs them without secret leakage."""
        assert self.proc is not None
        assert self.proc.stderr is not None
        try:
            while True:
                line_bytes = await self.proc.stderr.readline()
                if not line_bytes:
                    break
                raw_line = line_bytes.decode("utf-8", errors="replace").rstrip()
                if not raw_line:
                    continue
                redacted_line = self._redact(raw_line)
                logger.debug("openclaw_stderr", line=redacted_line)
                std_logger.info(redacted_line)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _send_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Sends a JSON-RPC 2.0 request and returns a future for its response."""
        if self._protocol_error is not None:
            raise self._protocol_error
        if len(self._pending_requests) >= MAX_CONCURRENT_REQUESTS:
            raise RuntimeError(
                f"Maximum concurrent requests limit reached ({MAX_CONCURRENT_REQUESTS})"
            )
        assert self.proc is not None
        assert self.proc.stdin is not None

        req_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending_requests[req_id] = fut

        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        line = json.dumps(payload) + "\n"
        self.proc.stdin.write(line.encode("utf-8"))
        await self.proc.stdin.drain()

        return await fut

    async def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        """Sends a JSON-RPC 2.0 notification (no id field, no expected response frame)."""
        if self.proc is None or self.proc.stdin is None or self.proc.stdin.is_closing():
            return
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        line = json.dumps(payload) + "\n"
        self.proc.stdin.write(line.encode("utf-8"))
        await self.proc.stdin.drain()

    async def _spawn_process(self) -> None:
        cmd = list(self.config.command)
        if self.config.gateway_url:
            cmd.extend(["--url", self.config.gateway_url])
        if self._token_path:
            cmd.extend(["--token-file", str(self._token_path)])

        env = os.environ.copy()
        env.update(self.config.env)
        env["OPENCLAW_HIDE_BANNER"] = "1"
        env["OPENCLAW_SUPPRESS_NOTES"] = "1"

        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(self.config.cwd) if self.config.cwd else None,
        )

        self._stdout_task = asyncio.create_task(self._stdout_reader_loop())
        self._stderr_task = asyncio.create_task(self._stderr_reader_loop())

    async def _cleanup(self, session_id: str | None = None) -> None:
        """Multi-stage bounded child process cleanup state machine (budget: <= 6s)."""
        # Stage 1: Protocol Cancellation notification
        if self.proc is not None and self.proc.returncode is None and session_id is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    self._send_notification("session/cancel", {"sessionId": session_id}),
                    timeout=0.5,
                )

        # Close stdin
        if self.proc is not None and self.proc.stdin is not None:
            with contextlib.suppress(Exception):
                if not self.proc.stdin.is_closing():
                    self.proc.stdin.close()
                    await asyncio.wait_for(self.proc.stdin.wait_closed(), timeout=0.5)

        # Stage 2: Graceful SIGTERM
        if self.proc is not None and self.proc.returncode is None:
            try:
                self.proc.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=2.0)
                except TimeoutError:
                    # Stage 3: Escalation SIGKILL
                    if self.proc.returncode is None:
                        self.proc.send_signal(signal.SIGKILL)
                        await asyncio.wait_for(self.proc.wait(), timeout=1.0)
            except ProcessLookupError:
                pass
            except Exception as e:
                logger.warning("subprocess_cleanup_error", error=str(e))

        # Stage 4: Pipe & Task Drain
        for task in (self._stdout_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(TimeoutError, asyncio.CancelledError, Exception):
                    await asyncio.wait_for(task, timeout=0.5)

        # Stage 5: Pending Futures & Token shredding
        if self._pending_requests:
            err = BrokenPipeError("OpenClaw process terminated during cleanup")
            self._fail_all_pending(err)

        self._cleanup_token_file()

    async def run(self) -> Result:
        """Executes the full ACP lifecycle over stdio."""
        session_id: str | None = None
        try:
            await self._setup_token_file()
            await self._spawn_process()

            # Phase 0: Pre-Prompt (process startup and handshake)
            self._phase = "pre_prompt"

            # 1. initialize
            init_res = await asyncio.wait_for(
                self._send_request(
                    "initialize",
                    {
                        "protocolVersion": 1,
                        "clientCapabilities": {
                            "sessionCapabilities": {"list": {}, "resume": {}, "close": {}}
                        },
                        "clientInfo": {"name": "personaos-worker", "version": "0.1.0"},
                    },
                ),
                timeout=self.config.timeout_seconds,
            )
            if "error" in init_res:
                err_msg = self._redact(str(init_res["error"].get("message", "Initialize error")))
                return Result(status="failed", error=f"Initialize failed: {err_msg}", output=None)

            # 2. session/new
            cwd_path = self.config.cwd or Path.cwd()
            sess_res = await asyncio.wait_for(
                self._send_request(
                    "session/new",
                    {
                        "cwd": str(cwd_path),
                        "mcpServers": [],
                    },
                ),
                timeout=self.config.timeout_seconds,
            )
            if "error" in sess_res:
                err_msg = self._redact(
                    str(sess_res["error"].get("message", "Session creation error"))
                )
                return Result(status="failed", error=f"session/new failed: {err_msg}", output=None)

            session_id = sess_res.get("result", {}).get("sessionId")
            if not session_id:
                return Result(
                    status="failed",
                    error="OpenClaw session/new returned empty sessionId",
                    output=None,
                )

            # Phase 1: In-Flight Prompt Accepted
            self._phase = "in_flight_prompt_accepted"

            prompt_parts: list[str] = []
            if self.context.task.objective:
                prompt_parts.append(f"Objective: {self.context.task.objective}")
            if self.context.task.input:
                if (
                    isinstance(self.context.task.input, dict)
                    and "instruction" in self.context.task.input
                ):
                    prompt_parts.append(str(self.context.task.input["instruction"]))
                prompt_parts.append(f"Input: {json.dumps(self.context.task.input)}")

            if self.checkpoint:
                prompt_parts.append(
                    "[RESUMPTION CONTEXT]\n"
                    "This task is resuming from a prior execution checkpoint.\n"
                    f"Saved checkpoint state: {json.dumps(self.checkpoint)}\n"
                    "Do NOT re-execute already completed stages. "
                    "Resume directly from the next pending stage."
                )

            prompt_text = "\n\n".join(prompt_parts) if prompt_parts else self.context.task.title

            prompt_res = await asyncio.wait_for(
                self._send_request(
                    "session/prompt",
                    {
                        "sessionId": session_id,
                        "prompt": [{"type": "text", "text": prompt_text}],
                    },
                ),
                timeout=self.config.timeout_seconds,
            )

            # Clean JSON-RPC error response from model/runtime
            if "error" in prompt_res:
                err_msg = self._redact(str(prompt_res["error"].get("message", "Runtime error")))
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        self._send_request("session/close", {"sessionId": session_id}),
                        timeout=1.0,
                    )
                return Result(status="failed", error=err_msg, output=None)

            result_payload = prompt_res.get("result", {})
            stop_reason = result_payload.get("stopReason", "end_turn")

            # Normal session/close
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    self._send_request("session/close", {"sessionId": session_id}),
                    timeout=1.0,
                )

            full_text = "".join(self._accumulated_chunks)
            return Result(
                status="completed",
                output={
                    "text": full_text,
                    "stop_reason": stop_reason,
                    "session_id": session_id,
                },
                error=None,
            )

        except TimeoutError:
            if self._phase == "in_flight_prompt_accepted":
                return Result(
                    status="failed",
                    error=(
                        "Ambiguous execution outcome: OpenClaw execution timed out "
                        "after prompt acceptance; external side effects may have occurred."
                    ),
                    output={
                        "ambiguous": True,
                        "phase": "in_flight_prompt_accepted",
                        "session_id": session_id,
                        "accumulated_text": "".join(self._accumulated_chunks),
                    },
                )
            return Result(
                status="failed",
                error=f"OpenClaw execution timed out during {self._phase}",
                output=None,
            )

        except asyncio.CancelledError:
            # Propagate coroutine cancellation to caller after bounded cleanup
            raise

        except Exception as exc:
            err_msg = self._redact(str(exc))
            if self._phase == "in_flight_prompt_accepted":
                return Result(
                    status="failed",
                    error=(
                        f"Ambiguous execution outcome: OpenClaw subprocess terminated: {err_msg}"
                    ),
                    output={
                        "ambiguous": True,
                        "phase": "in_flight_prompt_accepted",
                        "session_id": session_id,
                        "accumulated_text": "".join(self._accumulated_chunks),
                    },
                )
            return Result(
                status="failed",
                error=err_msg,
                output=None,
            )

        finally:
            await self._cleanup(session_id=session_id)


class OpenClawAdapter:
    """OpenClaw runtime adapter (implemented in Step 14b per ADR 0017)."""

    def __init__(
        self,
        api_url: str | None = None,
        config: OpenClawConfig | None = None,
    ) -> None:
        self._api_url = api_url
        if config is not None:
            self._config = config
        elif api_url:
            self._config = OpenClawConfig(gateway_url=api_url)
        else:
            self._config = OpenClawConfig()

    async def execute(
        self,
        context: WorkerExecutionContext,
        checkpoint: Any | None = None,
    ) -> Result:
        """Execute task via OpenClaw ACP JSON-RPC over stdio."""
        runner = _OpenClawProcessRunner(config=self._config, context=context, checkpoint=checkpoint)
        return await runner.run()


# Protocol conformance marker (mypy-only)
_: Adapter = OpenClawAdapter  # type: ignore[assignment]
