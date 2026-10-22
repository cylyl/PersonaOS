# ADR 0017: Live OpenClaw Runtime Adapter via JSON-RPC over stdio

**Status:** Proposed (Revised Step 14a Design Phase)
**Date:** 2026-10-22
**Deciders:** PersonaOS Architecture Team
**Consulted:** Liang, OpenClaw Runtime Specification (2026.10.3 / build 59cb447)
**Informed:** Control Plane, Worker Orchestration, Execution Engine

---

## Context & Problem Statement

PersonaOS separates worker identity, profile snapshotting, and task lifecycle management from the execution engine (ADR 0003, ADR 0005, ADR 0009, ADR 0010). Up to Step 13, all task execution and recovery integration tests ran through `SimulationAdapter`.

In Step 14, PersonaOS connects to a live OpenClaw agent runtime.

To maintain system integrity:
1. **Kernel Lifecycle Supremacy**: The execution kernel remains the sole owner of atomic claims (`queued -> in_progress`) and terminal transitions (`completed`, `failed`, `review`). The adapter has zero direct access to database state machines or lease tables.
2. **Ambiguous Outcome Safety**: Subprocess death or timeout occurring *after* a prompt is accepted by OpenClaw may have executed external side effects. For non-idempotent (`UNKNOWN`, `HAS_SIDE_EFFECTS`) tasks, such ambiguous outcomes must not simply be marked as a safe failed retry; the kernel must route them to `review` without automated replay.
3. **Credential Protection**: Command-line arguments (`argv`) are visible via `/proc/<pid>/cmdline` and `ps aux`. Production invocations must never pass raw tokens as command arguments; credentials must be supplied via verified `--token-file <path>` with mode `0600`, and all logs/traces must sanitize secret patterns.
4. **Deterministic Subprocess Cleanup**: Cancellation must follow a bounded multi-stage escalation (protocol cancel $\rightarrow$ `SIGTERM` $\rightarrow$ `SIGKILL` $\rightarrow$ pipe drain $\rightarrow$ future cleanup) without hanging on unresponsive processes or blocked pipes, and must re-raise `asyncio.CancelledError`.
5. **Fail-Fast Protocol Parsing**: Malformed stdout frames, missing `jsonrpc: "2.0"` tags, or unexpected EOF must reject in-flight request futures immediately rather than silently skipping lines and causing request timeouts.

---

## Verified OpenClaw Runtime Protocol (Empirical Evidence)

Testing against the installed OpenClaw 2026.10.3 (`OpenClaw 2026.10.3 (59cb447)`) on Linux confirmed the exact protocol and authentication mechanics:

### 1. Invocation & Authentication Mechanics
OpenClaw provides the **Agent Client Protocol (ACP)** bridge over stdio via `openclaw acp`:
```bash
openclaw acp --url ws://127.0.0.1:18789 --token-file /path/to/secured_token
```

#### Independent CLI Verification Evidence
Inspecting `openclaw acp --help` on the host confirms native support for `--token-file`:
```text
Options:
  --url <url>          Gateway URL (defaults to OPENCLAW_GATEWAY_URL or local discovery)
  --token <token>      Gateway auth token
  --token-file <path>  Path to file containing gateway auth token
```
Furthermore, the official runtime documentation (`/usr/lib/node_modules/openclaw/docs/cli/acp.md`, lines 58-65 and 156-159) explicitly specifies:
```bash
# preferred for local process safety
openclaw acp --url wss://gateway-host:18789 --token-file ~/.openclaw/gateway.token
```

#### Temporary Token File Lifecycle & Zeroing Guarantee
When an in-memory token is provided to the adapter:
1. The adapter creates a dedicated temporary directory with permissions `0700` (`stat.S_IRWXU`) via `tempfile.mkdtemp(prefix="openclaw_token_")`.
2. The token file is created using `os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)` ensuring only the current process user can read or write the file.
3. Execution runs within an asynchronous context manager (`try...finally`).
4. In the `finally` block, the file contents are overwritten with zeros (`b"\x00" * len(token)`) before unlinking with `os.unlink()`, and the directory is removed with `os.rmdir()`. This guarantees immediate cleanup even under unhandled exceptions, task cancellation, or process errors.

#### Channel Separation & Environment Flags
- **Environment**:
  - `OPENCLAW_HIDE_BANNER=1`: Suppresses ASCII headers on stdout.
  - `OPENCLAW_SUPPRESS_NOTES=1`: Suppresses runtime update notes on stdout.
- **Channel Separation**:
  - `stdout`: Exclusively line-delimited JSON-RPC 2.0 frames (`{"jsonrpc":"2.0",...}\n`).
  - `stderr`: Diagnostic traces (e.g. SQLite hold warnings, connection notes).

---

### 2. Sanitized Protocol Transcripts (Complete Empirical Records)

The following transcripts are complete JSON frames without truncation or ellipsis:

#### Step 1: Initialization Handshake
Client initiates handshake to exchange protocol version and capabilities:
```json
--> {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1, "clientCapabilities": {}}}
<-- {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1,"agentCapabilities":{"loadSession":true,"promptCapabilities":{"image":true,"audio":false,"embeddedContext":true},"mcpCapabilities":{"http":false,"sse":false},"sessionCapabilities":{"list":{},"resume":{},"close":{}}},"agentInfo":{"name":"openclaw-acp","title":"OpenClaw ACP Gateway","version":"2026.10.3"},"authMethods":[]}}
```

#### Step 2: Isolated Session Creation
Client creates an isolated ACP session bound to the working directory:
```json
--> {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": "/workspace", "mcpServers": []}}
<-- {"jsonrpc":"2.0","id":2,"result":{"sessionId":"fe1a1634-d03a-4fc8-8cfb-6ab71054fea1","configOptions":[{"type":"select","id":"thought_level","value":"default"}],"modes":{"currentModeId":"adaptive"}}}
```

#### Step 3: Prompt Submission & Streaming Notifications
Client sends task instructions as request ID `3`. OpenClaw streams updates via unsolicited `session/update` notifications, and settles request `id: 3` when the turn ends:
```json
--> {"jsonrpc": "2.0", "id": 3, "method": "session/prompt", "params": {"sessionId": "fe1a1634-d03a-4fc8-8cfb-6ab71054fea1", "prompt": [{"type": "text", "text": "Deploy staging VPC"}]}}
<-- {"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"fe1a1634-d03a-4fc8-8cfb-6ab71054fea1","update":{"sessionUpdate":"session_info_update"}}}
<-- {"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"fe1a1634-d03a-4fc8-8cfb-6ab71054fea1","update":{"sessionUpdate":"available_commands_update"}}}
<-- {"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"fe1a1634-d03a-4fc8-8cfb-6ab71054fea1","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"VPC created successfully."}}}}
<-- {"jsonrpc":"2.0","id":3,"result":{"stopReason":"end_turn"}}
```

#### Step 4: Cancellation Mechanics & Settlement Observation
Per the Agent Client Protocol specification (`@agentclientprotocol/sdk` line 508):
- `session/cancel` is an **Agent Notification** (`method: "session/cancel"`), NOT a request. It contains **no request ID** and produces **no direct response frame**.
- Sending `session/cancel` signals the Gateway to abort the active prompt turn.
- **How cancellation completion is observed:** The Gateway aborts the in-flight prompt, which causes the pending `session/prompt` (request `id: 3`) to resolve with `stopReason: "cancelled"`:

```json
--> {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "fe1a1634-d03a-4fc8-8cfb-6ab71054fea1"}}
<-- {"jsonrpc":"2.0","id":3,"result":{"stopReason":"cancelled"}}
```
*(Notice: The response frame `{"jsonrpc":"2.0","id":3,...}` is the settlement of the pending `session/prompt` request ID 3, NOT a response to `session/cancel`)*. If the future does not resolve within the Stage 1 timeout budget (2.0s), cleanup escalates to Stage 2 (`SIGTERM`).

#### Step 5: Session Teardown
```json
--> {"jsonrpc": "2.0", "id": 4, "method": "session/close", "params": {"sessionId": "fe1a1634-d03a-4fc8-8cfb-6ab71054fea1"}}
<-- {"jsonrpc":"2.0","id":4,"result":{}}
```

---

## Decision

We will implement `OpenClawAdapter` in `src/personaos/execution/adapters/openclaw.py` as an asynchronous subprocess transport managing the `openclaw acp` JSON-RPC 2.0 stream over stdio:

### 1. Failure Semantics & Ambiguous Outcome Routing
To prevent duplicate side-effect execution:
- The adapter tracks execution state across two distinct phases:
  - `Phase 0: Pre-Prompt`: Process starting, `initialize`, `session/new`. No external side effects can have occurred. Failures during Phase 0 are safe adapter errors.
  - `Phase 1: In-Flight Prompt Accepted`: `session/prompt` has been transmitted. The external agent runtime may have triggered real-world mutations.
- If a timeout, broken pipe, or unexpected process termination occurs during `Phase 1`:
  - The adapter returns `Result(status="failed", error="Ambiguous execution outcome: subprocess terminated/timed out after prompt acceptance; side effects may have occurred", output={"ambiguous": True, "phase": "in_flight_prompt_accepted"})`.

### 2. Kernel Lifecycle Integration (Step 14b Contract)
In `WorkerExecutionKernel.fail(task, result)`:
- The kernel inspects `result.output` and `task.side_effect_classification`:
  ```python
  is_ambiguous = bool(result.output and result.output.get("ambiguous") is True)
  is_non_idempotent = getattr(task, "side_effect_classification", "UNKNOWN") in ("UNKNOWN", "HAS_SIDE_EFFECTS")

  if is_ambiguous and is_non_idempotent:
      # Route directly to human review queue without automated retry
      target_status = "review"
      exhaustion_reason = "ambiguous_in_flight_failure"
      completed_at = None
  else:
      # Safe to mark failed and allow queue retry up to max_retries
      target_status = "failed"
      exhaustion_reason = None
      completed_at = func.now()
  ```
- This guarantees that ambiguous in-flight failures for non-idempotent tasks never enter automatic retry or lease reclamation loops.
- In Step 14b, a dedicated PostgreSQL integration test `test_kernel_routes_ambiguous_failure_to_review` will verify this state machine transition against a real database.

### 3. Credential Security & Redaction
- The adapter requires `OpenClawConfig.token_file` (or creates a zero-shredded temporary file with permission `0600`).
- The adapter never passes `--token <raw_value>` in command-line arguments.
- A credential redaction filter sanitizes all log events, exception messages, and stderr capture buffers, replacing any string matching the token value or hexadecimal patterns `[a-f0-9]{32,64}` with `[REDACTED]`.

### 4. Bounded Subprocess Cleanup State Machine
When execution finishes or cancellation is signaled:
1. **Protocol Cancellation (2.0s budget)**: Send `session/cancel` notification over stdin and await settlement of in-flight `session/prompt` future.
2. **Graceful Signal (2.0s budget)**: Send `SIGTERM` to the child process and await process exit.
3. **Escalation Kill (1.0s budget)**: Send `SIGKILL` if process has not exited.
4. **Pipe & Task Drain (1.0s budget)**: Cancel and await stdout/stderr reader asyncio tasks.
5. **Future Settlement**: Cancel any remaining in-flight request futures.
6. **Preserve Cancellation**: If cleanup was invoked due to `asyncio.CancelledError`, re-raise `asyncio.CancelledError`.

### 5. Fail-Fast Protocol Corruption Rules
- Message line length bounded by `MAX_MESSAGE_BYTES = 16 * 1024 * 1024` (16 MiB). Oversized lines trigger immediate protocol violation.
- Lines that cannot be parsed as JSON trigger `ProtocolViolationError` and immediately fail the in-flight request future rather than hanging until timeout.
- Unexpected EOF on stdout immediately fails all pending request futures with `BrokenPipeError`.
- Max concurrent requests bounded by `MAX_CONCURRENT_REQUESTS = 16`.

---

## Consequences

### Positive
- **Proven Compatibility**: Grounded in verified, reproducible OpenClaw ACP protocol transcripts without truncation.
- **Side-Effect Safety**: Ambiguous in-flight failures for non-idempotent tasks are quarantined to `review` via kernel lifecycle enforcement.
- **Zero Credential Exposure**: Protected against `/proc` inspection via verified `--token-file` and zero-shredded temporary files.
- **Bounded Resources**: Cancellation cannot hang indefinitely; child processes are strictly reaped within 6.0 seconds.

### Negative / Trade-offs
- Managing an OS subprocess over stdio requires defensive pipe-drain loops in Python `asyncio`.
- Quarantining ambiguous failures to `review` requires human operator intervention to inspect and unblock non-idempotent tasks.
