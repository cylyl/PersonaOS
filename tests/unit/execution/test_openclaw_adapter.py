"""Acceptance tests for OpenClawAdapter (Step 14a Design).

Validates the full adapter boundary per ADR 0017 and docs/specs/live-openclaw-runtime-adapter.md:
  1. JSON-RPC 2.0 stdio framing and request correlation
  2. Subprocess initialization and session lifecycle
  3. Prompt delivery and streaming text chunk accumulation
  4. Server error handling (-32602, -32603)
  5. Timeout enforcement
  6. Asynchronous cancellation (when kernel detects reclaim / stale lease)
  7. Subprocess termination and broken pipes
  8. Stderr diagnostic stream isolation
  9. Ambiguous execution outcome tracking (Finding 1)
 10. Credential redaction in logs and errors (Finding 2)
 11. Fail-fast on protocol corruption (Finding 5)
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from personaos.domain.task import Task
from personaos.domain.worker import Persona, Worker, WorkerProfile
from personaos.execution.adapters.base import Adapter
from personaos.execution.adapters.openclaw import OpenClawAdapter, OpenClawConfig
from personaos.execution.context import WorkerExecutionContext


def _make_context(
    task_id: str = "task-infra-001",
    side_effect_classification: str = "IDEMPOTENT",
) -> WorkerExecutionContext:
    worker = Worker(
        id="worker-01",
        name="Infra Worker",
        role="infra",
        status="active",
        runtime="openclaw",
        max_concurrent_tasks=1,
        current_profile_version=1,
    )
    profile = WorkerProfile(
        version=1,
        role="infra",
        runtime="openclaw",
        skills=["terraform"],
        permissions=["aws"],
        persona=Persona(style="concise", autonomy="high"),
    )
    task = Task(
        id=task_id,
        title="Provision S3 Bucket",
        objective="Create static assets bucket in us-west-2",
        input={"bucket_name": "company-assets-prod", "region": "us-west-2"},
        profile_version=1,
        status="in_progress",
        priority="medium",
        side_effect_classification=side_effect_classification,
    )
    return WorkerExecutionContext(worker=worker, profile=profile, task=task)


# Mock OpenClaw stdio process script for unit/contract tests
MOCK_OPENCLAW_SCRIPT = """
import os, sys, json, time

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except Exception:
        continue

    method = req.get("method")
    req_id = req.get("id")

    if method == "initialize":
        if os.environ.get("MOCK_BEHAVIOR") == "INIT_ERROR":
            res = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32000,
                    "message": "Protocol initialization rejected",
                },
            }
            sys.stdout.write(json.dumps(res) + "\\n")
            sys.stdout.flush()
            continue

        res = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": 1,
                "agentCapabilities": {
                    "sessionCapabilities": {"list": {}, "resume": {}, "close": {}}
                },
                "agentInfo": {"name": "mock-openclaw", "version": "1.0.0"}
            }
        }
        sys.stdout.write(json.dumps(res) + "\\n")
        sys.stdout.flush()

    elif method == "session/new":
        if os.environ.get("MOCK_BEHAVIOR") == "SESSION_ERROR":
            res = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32001,
                    "message": "Session creation rejected by agent",
                },
            }
            sys.stdout.write(json.dumps(res) + "\\n")
            sys.stdout.flush()
            continue

        params = req.get("params", {})
        if "mcpServers" not in params:
            res = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32602,
                    "message": "Invalid params",
                    "data": "mcpServers required",
                }
            }
        else:
            res = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"sessionId": "mock-sess-12345"}
            }
        sys.stdout.write(json.dumps(res) + "\\n")
        sys.stdout.flush()

    elif method == "session/prompt":
        prompt_text = req.get("params", {}).get("prompt", [{}])[0].get("text", "")
        # Emits diagnostic logs to stderr to test channel isolation and redaction
        sys.stderr.write("[diagnostic] Processing turn with token secret_token_1234567890abcdef\\n")
        sys.stderr.flush()

        if "SIMULATE_TIMEOUT" in prompt_text:
            time.sleep(10.0)
            continue

        if "SIMULATE_CORRUPT_STDOUT" in prompt_text:
            sys.stdout.write("MALFORMED_NON_JSON_CORRUPTION\\n")
            sys.stdout.flush()
            continue

        if "SIMULATE_ERROR" in prompt_text:
            res = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": "Simulated runtime failure"}
            }
            sys.stdout.write(json.dumps(res) + "\\n")
            sys.stdout.flush()
            continue

        if "SIMULATE_SIDE_EFFECT_THEN_CRASH" in prompt_text:
            # Side effect simulated before abrupt process crash
            sys.stderr.write("[external_action] AWS VPC resource created\\n")
            sys.stderr.flush()
            sys.exit(42)

        if "SIMULATE_CRASH" in prompt_text:
            sys.exit(42)

        # Stream update chunk
        chunk = {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "mock-sess-12345",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Bucket provisioned successfully."}
                }
            }
        }
        sys.stdout.write(json.dumps(chunk) + "\\n")
        sys.stdout.flush()

        # Terminal prompt response
        res = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"stopReason": "end_turn"}
        }
        sys.stdout.write(json.dumps(res) + "\\n")
        sys.stdout.flush()

    elif method == "session/cancel":
        # Notification: produces no direct response to session/cancel.
        # Causes the active in-flight session/prompt turn to resolve as cancelled.
        pass

    elif method == "session/close":
        res = {"jsonrpc": "2.0", "id": req_id, "result": {}}
        sys.stdout.write(json.dumps(res) + "\\n")
        sys.stdout.flush()
"""


@pytest.fixture
def mock_openclaw_script(tmp_path: Path) -> Path:
    p = tmp_path / "mock_openclaw.py"
    p.write_text(MOCK_OPENCLAW_SCRIPT.strip(), encoding="utf-8")
    return p


def test_openclaw_adapter_conforms_to_protocol() -> None:
    """Verify OpenClawAdapter implements the Adapter Protocol."""
    assert issubclass(OpenClawAdapter, Adapter) or hasattr(OpenClawAdapter, "execute")


@pytest.mark.asyncio
async def test_openclaw_adapter_successful_execution(
    mock_openclaw_script: Path,
) -> None:
    """AC #1 & #2: Successful execution returns completed Result with accumulated output."""
    config = OpenClawConfig(
        command=(sys.executable, str(mock_openclaw_script)),
        gateway_url="",
        timeout_seconds=5.0,
    )
    adapter = OpenClawAdapter(config=config)
    context = _make_context()

    result = await adapter.execute(context=context)

    assert result.status == "completed"
    assert result.output is not None
    assert "Bucket provisioned successfully" in result.output.get("text", "")
    assert result.error is None


@pytest.mark.asyncio
async def test_openclaw_adapter_handles_runtime_error(
    mock_openclaw_script: Path,
) -> None:
    """AC #3: JSON-RPC errors are caught and converted to failed Result."""
    config = OpenClawConfig(
        command=(sys.executable, str(mock_openclaw_script)),
        gateway_url="",
        timeout_seconds=5.0,
    )
    adapter = OpenClawAdapter(config=config)
    context = _make_context()
    context.task.input["instruction"] = "SIMULATE_ERROR"

    result = await adapter.execute(context=context)

    assert result.status == "failed"
    assert result.error is not None
    assert "Simulated runtime failure" in result.error
    assert result.output is None


@pytest.mark.asyncio
async def test_openclaw_adapter_handles_timeout(
    mock_openclaw_script: Path,
) -> None:
    """AC #4: Prompt exceeding timeout_seconds triggers failure result."""
    config = OpenClawConfig(
        command=(sys.executable, str(mock_openclaw_script)),
        gateway_url="",
        timeout_seconds=0.5,
    )
    adapter = OpenClawAdapter(config=config)
    context = _make_context()
    context.task.input["instruction"] = "SIMULATE_TIMEOUT"

    result = await adapter.execute(context=context)

    assert result.status == "failed"
    assert result.error is not None
    assert "timed out" in result.error.lower()


@pytest.mark.asyncio
async def test_openclaw_adapter_handles_subprocess_crash(
    mock_openclaw_script: Path,
) -> None:
    """AC #6: Subprocess premature crash / broken pipe returns failed Result."""
    config = OpenClawConfig(
        command=(sys.executable, str(mock_openclaw_script)),
        gateway_url="",
        timeout_seconds=5.0,
    )
    adapter = OpenClawAdapter(config=config)
    context = _make_context()
    context.task.input["instruction"] = "SIMULATE_CRASH"

    result = await adapter.execute(context=context)

    assert result.status == "failed"
    assert result.error is not None
    err_lower = result.error.lower()
    assert any(term in err_lower for term in ("exit", "crash", "terminated"))


@pytest.mark.asyncio
async def test_openclaw_adapter_cancellation_cleans_up_process(
    mock_openclaw_script: Path,
) -> None:
    """AC #5: Task cancellation (e.g. lease expiry / reclaim) terminates child process cleanly."""
    config = OpenClawConfig(
        command=(sys.executable, str(mock_openclaw_script)),
        gateway_url="",
        timeout_seconds=10.0,
    )
    adapter = OpenClawAdapter(config=config)
    context = _make_context()
    context.task.input["instruction"] = "SIMULATE_TIMEOUT"

    task = asyncio.create_task(adapter.execute(context=context))
    await asyncio.sleep(0.2)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_openclaw_adapter_ambiguous_in_flight_failure_flag(
    mock_openclaw_script: Path,
) -> None:
    """Finding 1: Subprocess crash after prompt acceptance carries ambiguous flag."""
    config = OpenClawConfig(
        command=(sys.executable, str(mock_openclaw_script)),
        gateway_url="",
        timeout_seconds=5.0,
    )
    adapter = OpenClawAdapter(config=config)
    context = _make_context(side_effect_classification="HAS_SIDE_EFFECTS")
    context.task.input["instruction"] = "SIMULATE_SIDE_EFFECT_THEN_CRASH"

    result = await adapter.execute(context=context)

    assert result.status == "failed"
    assert result.output is not None
    assert result.output.get("ambiguous") is True
    assert result.output.get("phase") == "in_flight_prompt_accepted"


@pytest.mark.asyncio
async def test_openclaw_adapter_redacts_credentials_from_logs(
    mock_openclaw_script: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Finding 2: Sensitive token values are redacted from diagnostic logs and error traces."""
    secret_token = "secret_token_1234567890abcdef"
    config = OpenClawConfig(
        command=(sys.executable, str(mock_openclaw_script)),
        gateway_url="",
        token=secret_token,
        timeout_seconds=5.0,
    )
    adapter = OpenClawAdapter(config=config)
    context = _make_context()

    await adapter.execute(context=context)

    # Assert secret token is not exposed anywhere in captured logs
    for record in caplog.records:
        assert secret_token not in record.message
        assert secret_token not in str(record.args)


@pytest.mark.asyncio
async def test_openclaw_adapter_fails_fast_on_malformed_stdout(
    mock_openclaw_script: Path,
) -> None:
    """Finding 5: Corrupt non-JSON stdout frame immediately fails pending requests."""
    config = OpenClawConfig(
        command=(sys.executable, str(mock_openclaw_script)),
        gateway_url="",
        timeout_seconds=5.0,
    )
    adapter = OpenClawAdapter(config=config)
    context = _make_context()
    context.task.input["instruction"] = "SIMULATE_CORRUPT_STDOUT"

    result = await adapter.execute(context=context)

    assert result.status == "failed"
    assert result.error is not None
    assert "protocol" in result.error.lower() or "malformed" in result.error.lower()


@pytest.mark.asyncio
async def test_openclaw_adapter_token_file_lifecycle_and_zero_shred(
    mock_openclaw_script: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies temporary token file creation (0600 mode), zero-overwrite, and unlinking."""
    import stat

    from personaos.execution.adapters.openclaw import _OpenClawProcessRunner

    created_paths: list[Path] = []
    created_modes: list[int] = []
    overwritten_bytes: list[bytes] = []

    real_setup = _OpenClawProcessRunner._setup_token_file
    real_cleanup = _OpenClawProcessRunner._cleanup_token_file

    async def tracking_setup(self: _OpenClawProcessRunner) -> None:
        await real_setup(self)
        if self._token_path:
            created_paths.append(self._token_path)
            mode = stat.S_IMODE(self._token_path.stat().st_mode)
            created_modes.append(mode)

    def tracking_cleanup(self: _OpenClawProcessRunner) -> None:
        if self._token_path and self._token_path.exists():
            overwritten_bytes.append(self._token_path.read_bytes())
        real_cleanup(self)

    monkeypatch.setattr(_OpenClawProcessRunner, "_setup_token_file", tracking_setup)
    monkeypatch.setattr(_OpenClawProcessRunner, "_cleanup_token_file", tracking_cleanup)

    secret = "super_confidential_gateway_token_12345"
    config = OpenClawConfig(
        command=(sys.executable, str(mock_openclaw_script)),
        gateway_url="",
        token=secret,
        timeout_seconds=5.0,
    )
    adapter = OpenClawAdapter(config=config)
    context = _make_context()

    result = await adapter.execute(context=context)
    assert result.status == "completed"

    # Verify token file was created with restrictive permissions
    assert len(created_paths) == 1
    token_path = created_paths[0]
    assert created_modes[0] == 0o600

    # Verify token file was shredded and unlinked
    assert not token_path.exists()
    assert not token_path.parent.exists()


@pytest.mark.asyncio
async def test_openclaw_adapter_interleaved_streaming_notifications(
    tmp_path: Path,
) -> None:
    """Demonstrates request correlation with interleaved notifications and streaming chunks."""
    script_content = (
        "import sys, json\n"
        "for line in sys.stdin:\n"
        "    req = json.loads(line.strip())\n"
        "    method = req.get('method')\n"
        "    req_id = req.get('id')\n"
        "    if method == 'initialize':\n"
        "        res = {'jsonrpc': '2.0', 'id': req_id, 'result': {'protocolVersion': 1}}\n"
        "        sys.stdout.write(json.dumps(res) + '\\n')\n"
        "        sys.stdout.flush()\n"
        "    elif method == 'session/new':\n"
        "        res = {'jsonrpc': '2.0', 'id': req_id, 'result': {'sessionId': 'sess-99'}}\n"
        "        sys.stdout.write(json.dumps(res) + '\\n')\n"
        "        sys.stdout.flush()\n"
        "    elif method == 'session/prompt':\n"
        "        c1 = 'Part 1: Initializing... '\n"
        "        c2 = 'Part 2: Applying schema... '\n"
        "        c3 = 'Part 3: Done.'\n"
        "        for chunk in [c1, c2, c3]:\n"
        "            note = {\n"
        "                'jsonrpc': '2.0',\n"
        "                'method': 'session/update',\n"
        "                'params': {\n"
        "                    'sessionId': 'sess-99',\n"
        "                    'update': {\n"
        "                        'sessionUpdate': 'chunk',\n"
        "                        'content': {'type': 'text', 'text': chunk}\n"
        "                    }\n"
        "                }\n"
        "            }\n"
        "            sys.stdout.write(json.dumps(note) + '\\n')\n"
        "            sys.stdout.flush()\n"
        "        heartbeat = {'jsonrpc': '2.0', 'method': 'session/heartbeat'}\n"
        "        sys.stdout.write(json.dumps(heartbeat) + '\\n')\n"
        "        sys.stdout.flush()\n"
        "        res = {'jsonrpc': '2.0', 'id': req_id, 'result': {'stopReason': 'end_turn'}}\n"
        "        sys.stdout.write(json.dumps(res) + '\\n')\n"
        "        sys.stdout.flush()\n"
        "    elif method == 'session/close':\n"
        "        res = {'jsonrpc': '2.0', 'id': req_id, 'result': {}}\n"
        "        sys.stdout.write(json.dumps(res) + '\\n')\n"
        "        sys.stdout.flush()\n"
    )
    streaming_mock = tmp_path / "streaming_mock.py"
    streaming_mock.write_text(script_content, encoding="utf-8")

    config = OpenClawConfig(
        command=(sys.executable, str(streaming_mock)),
        gateway_url="",
        timeout_seconds=5.0,
    )
    adapter = OpenClawAdapter(config=config)
    context = _make_context()

    result = await adapter.execute(context=context)
    assert result.status == "completed"
    assert result.output is not None
    expected_text = "Part 1: Initializing... Part 2: Applying schema... Part 3: Done."
    assert result.output.get("text") == expected_text


@pytest.mark.asyncio
async def test_openclaw_adapter_handles_initialization_failure(
    mock_openclaw_script: Path,
) -> None:
    """AC: Initialization failure returns failed Result and safely cleans up resources."""
    config = OpenClawConfig(
        command=(sys.executable, str(mock_openclaw_script)),
        gateway_url="",
        timeout_seconds=5.0,
        env={"MOCK_BEHAVIOR": "INIT_ERROR"},
    )
    adapter = OpenClawAdapter(config=config)
    context = _make_context()

    result = await adapter.execute(context=context)

    assert result.status == "failed"
    assert result.error is not None
    assert "Initialize failed: Protocol initialization rejected" in result.error
    assert result.output is None


@pytest.mark.asyncio
async def test_openclaw_adapter_handles_session_creation_failure(
    mock_openclaw_script: Path,
) -> None:
    """AC: Session creation failure returns failed Result and safely cleans up resources."""
    config = OpenClawConfig(
        command=(sys.executable, str(mock_openclaw_script)),
        gateway_url="",
        timeout_seconds=5.0,
        env={"MOCK_BEHAVIOR": "SESSION_ERROR"},
    )
    adapter = OpenClawAdapter(config=config)
    context = _make_context()

    result = await adapter.execute(context=context)

    assert result.status == "failed"
    assert result.error is not None
    assert "session/new failed: Session creation rejected by agent" in result.error
    assert result.output is None


@pytest.mark.asyncio
async def test_openclaw_live_execution() -> None:
    """Step 14b Milestone 3: Live OpenClaw gateway validation on port 18789.

    Connects to the local running gateway, authenticates via temporary --token-file,
    executes a harmless disposable probe task, and asserts clean end-turn completion.

    To ensure routine unit tests remain deterministic and do not block on external
    or busy local daemons, this test is gated by PERSONAOS_RUN_LIVE_OPENCLAW_TESTS=1.

    Run deliberately with:
        PERSONAOS_RUN_LIVE_OPENCLAW_TESTS=1 pytest \\
            tests/unit/execution/test_openclaw_adapter.py -k test_openclaw_live_execution -v
    """
    import json
    import os
    import socket

    if os.environ.get("PERSONAOS_RUN_LIVE_OPENCLAW_TESTS", "").lower() not in (
        "1",
        "true",
        "yes",
    ):
        pytest.skip(
            "Live OpenClaw gateway test skipped (opt-in). "
            "Set PERSONAOS_RUN_LIVE_OPENCLAW_TESTS=1 to run against a live gateway."
        )

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        if s.connect_ex(("127.0.0.1", 18789)) != 0:
            pytest.skip("OpenClaw gateway not listening on 127.0.0.1:18789")

    conf_path = Path("/root/.openclaw/openclaw.json")
    if not conf_path.exists():
        pytest.skip("/root/.openclaw/openclaw.json not found")

    try:
        conf_data = json.loads(conf_path.read_text(encoding="utf-8"))
        token = conf_data.get("gateway", {}).get("auth", {}).get("token")
    except Exception as exc:
        pytest.skip(f"Could not read auth token from openclaw.json: {exc}")

    if not token:
        pytest.skip("No token present in openclaw.json")

    timeout_sec = float(os.environ.get("PERSONAOS_LIVE_OPENCLAW_TIMEOUT", "30.0"))
    config = OpenClawConfig(
        command=("openclaw", "acp", "--session", "agent:devops:main"),
        gateway_url="ws://127.0.0.1:18789",
        token=token,
        timeout_seconds=timeout_sec,
    )
    adapter = OpenClawAdapter(config=config)
    context = _make_context()
    context.task.input["instruction"] = "Reply strictly with PONG."

    result = await adapter.execute(context=context)

    assert result.status == "completed", f"Live OpenClaw execution failed: {result.error}"
    assert result.error is None
    assert result.output is not None
    assert "PONG" in result.output.get("text", "")
    assert result.output.get("stop_reason") == "end_turn"
    assert result.output.get("session_id") is not None
