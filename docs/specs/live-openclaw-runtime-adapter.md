# Specification: Live OpenClaw Runtime Adapter (Step 14a Revised)

**Target Component:** `src/personaos/execution/adapters/openclaw.py`
**Governing ADR:** ADR 0017 (`docs/adr/0017-live-openclaw-runtime-adapter.md`)
**Compliance Contract:** ADR 0005 (Thin Runtime Adapter), ADR 0009 (Frozen WorkerExecutionContext), ADR 0010 (WorkerExecutionKernel Lifecycle), ADR 0016 (In-Progress Task Recovery).

---

## 1. Architectural Boundary & Separation of Concerns

```
+---------------------------------------------------------------------------------+
|                                PersonaOS Worker                                 |
|                                                                                 |
|   +-----------------------+              +----------------------------------+   |
|   |   WorkerRunner        |              |  WorkerExecutionKernel           |   |
|   |  - node binding       |              | - atomic claim (queued->in_prog) |   |
|   |  - runner heartbeat   |              | - active lease heartbeat loop    |   |
|   |  - crash recovery     |              | - generation fencing check       |   |
|   |  - node unbinding     |              | - review routing on ambiguity    |   |
|   +-----------+-----------+              +-----------------+----------------+   |
|               |                                            |                    |
|               v                                            v                    |
|   +-----------------------+              +----------------------------------+   |
|   |   WorkerOrchestrator  | -----------> |  OpenClawAdapter                 |   |
|   |  - process_one()      |   context    |  - Adapter Protocol              |   |
|   +-----------------------+  (snapshot)  |  - JSON-RPC 2.0 stdio framing    |   |
|                                          |  - Credential redaction          |   |
|                                          |  - Bounded multi-stage cleanup   |   |
|                                          +-----------------+----------------+   |
+------------------------------------------------------------|--------------------+
                                                             |
                                               stdin/stdout  |  (JSON-RPC 2.0 lines)
                                               stderr        |  (Sanitized diagnostics)
                                               v
                                           +----------------------------------+
                                           |  openclaw acp subprocess         |
                                           |  (OpenClaw 2026.10.3 / 59cb447)  |
                                           +-----------------+----------------+
                                                             |
                                            ws:// (local/lan)|  (Control Plane)
                                                             v
                                           +----------------------------------+
                                           |  OpenClaw Gateway (Port 18789)   |
                                           +----------------------------------+
```

### Invariant Rules
1. **Zero Domain Logic in Adapter**: The adapter transforms `WorkerExecutionContext` into ACP prompt commands and parses responses into `Result(status="completed" | "failed", output=..., error=...)`. It never queries or mutates the database.
2. **Lifecycle Ownership**: Only `WorkerExecutionKernel` updates `TaskModel.status`, increments `generation`, or touches PostgreSQL leases.
3. **Snapshot Isolation**: The adapter only sees `context.profile`, which is the frozen snapshot loaded at enqueue time. It cannot reach `worker.current_profile_version`.
4. **Side-Effect Safety & Ambiguity Handling**: Database generation fencing guarantees state-machine mutual exclusion in PostgreSQL, but **cannot undo an external side effect** performed by an agent before an abrupt crash. When failures occur after prompt acceptance, the adapter reports the ambiguous execution outcome to allow the kernel to route non-idempotent tasks to `review`.

---

## 2. Configuration & Secure Process Startup

### 2.1 Configuration Schema (`OpenClawConfig`)
```python
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
```

### 2.2 Credential Protection & Independent CLI Verification
To prevent token exposure in process listings (`ps aux`, `/proc/<pid>/cmdline`):
1. **Command Line Policy**: The adapter **never** passes `--token <raw_value>` in `argv`.
2. **Independently Verifiable CLI Evidence**:
   - `openclaw acp --help` on OpenClaw 2026.10.3 (`59cb447`) confirms:
     ```text
     --token-file <path>  Path to file containing gateway auth token
     --url <url>          Gateway URL (defaults to OPENCLAW_GATEWAY_URL or local discovery)
     ```
   - Official runtime documentation (`/usr/lib/node_modules/openclaw/docs/cli/acp.md`, line 158) confirms:
     ```bash
     # preferred for local process safety
     openclaw acp --url wss://gateway-host:18789 --token-file ~/.openclaw/gateway.token
     ```
3. **Temporary Token Lifecycle & Zero-Overwriting Guarantee**:
   When `config.token` is provided in-memory rather than via a pre-existing `config.token_file`:
   - A dedicated directory is created via `tempfile.mkdtemp(prefix="openclaw_token_")` with mode `0700` (`stat.S_IRWXU`).
   - The token file is created using `os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)` ensuring only the current process owner can read or write.
   - The entire subprocess lifecycle executes inside a `try...finally` block.
   - In the `finally` cleanup block, the token bytes on disk are overwritten with zeros (`b"\x00" * len(token)`), flushed, and synced before `os.unlink()` and `os.rmdir()`. This provides a zero-leak guarantee even under unhandled exceptions or abrupt cancellations.
4. **Redaction Filter**: All stderr lines, error messages, and trace logs pass through a sanitizer that replaces any occurrence of `token` or hexadecimal patterns `[a-f0-9]{32,64}` with `[REDACTED]`.

### 2.3 Process Invocation
```python
cmd = list(config.command)
if config.gateway_url:
    cmd.extend(["--url", config.gateway_url])

# Mandatory: --token-file keeps credentials out of process argv
token_file = await self._ensure_token_file()
if token_file:
    cmd.extend(["--token-file", str(token_file)])

env = os.environ.copy()
env.update(config.env)
env["OPENCLAW_HIDE_BANNER"] = "1"
env["OPENCLAW_SUPPRESS_NOTES"] = "1"

proc = await asyncio.create_subprocess_exec(
    *cmd,
    stdin=asyncio.subprocess.PIPE,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    env=env,
    cwd=str(config.cwd) if config.cwd else None,
)
```

---

## 3. Protocol Framing, Correlation & Fail-Fast Rules

### 3.1 Line-Delimited JSON-RPC 2.0 Framing
- **Stdout Protocol**: Each message is a single UTF-8 line terminated by `\n`.
- **Request Envelope**: `{"jsonrpc": "2.0", "id": N, "method": M, "params": P}`
- **Correlation**: The adapter maintains `_pending_requests: dict[int, asyncio.Future[dict[str, Any]]]`.
- **Limits**:
  - `MAX_MESSAGE_BYTES = 16 * 1024 * 1024` (16 MiB line limit). Lines exceeding this limit raise `ProtocolViolationError`.
  - `MAX_CONCURRENT_REQUESTS = 16`. Exceeding this limit immediately rejects the request with `OverloadedError`.

### 3.2 Cancellation Notification vs Prompt Request Settlement
Per `@agentclientprotocol/sdk` (lines 507-509):
- `session/cancel` is a JSON-RPC **Notification** (`{"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}}`). It has **no request ID** and produces **no response frame**.
- The Gateway processes this notification by cancelling the turn, which causes the in-flight `session/prompt` (request `id: 3`) to return:
  `{"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "cancelled"}}`.
- The adapter observes cancellation completion by awaiting the settlement of the pending `session/prompt` future with `stopReason == "cancelled"`.

### 3.3 Fail-Fast Protocol Corruption Rules
1. **Malformed JSON**: If a line received on stdout cannot be parsed as JSON:
   - Log critical error with line snippet (redacted).
   - Immediately fail all pending request futures with `ProtocolViolationError("Malformed non-JSON frame received from OpenClaw stdout")`.
2. **Missing `jsonrpc: "2.0"` Tag**:
   - Frames missing `"jsonrpc": "2.0"` fail pending requests with `ProtocolViolationError`.
3. **Unexpected EOF / Process Exit**:
   - If stdout closes while request futures are pending, resolve all pending futures immediately with `BrokenPipeError("OpenClaw stdout closed unexpectedly")`.
4. **Unmatched / Duplicate Response**:
   - Responses with an unknown `id` log `UncorrelatedResponseWarning` and are discarded.
   - Duplicate responses for an already-completed future log `DuplicateResponseWarning` and are discarded.
5. **Permitted Notifications**:
   - Unsolicited notifications (`"method": "session/update"`) are routed to registered streaming listeners and do not affect request correlation.

---

## 4. Execution Lifecycle & Kernel Integration

### 4.1 Execution Phases & Ambiguity Invariant
The adapter partitions task execution into two distinct phases:

```
[Start] --> Phase 0: Pre-Prompt (Process startup, initialize, session/new)
                 |
                 +--> Failure here: Non-ambiguous. No side-effects could have run.
                 |
                 v
            Phase 1: In-Flight Prompt Accepted (session/prompt transmitted)
                 |
                 +--> Crash / Timeout / Broken Pipe here:
                      AMBIGUOUS EXECUTION OUTCOME.
                      Agent may have performed external side-effects!
```

### 4.2 Ambiguous Failure Mapping
When timeout, broken pipe, or child crash occurs during **Phase 1**:
1. **Adapter Output**:
   ```python
   Result(
       status="failed",
       error=(
           "Ambiguous execution outcome: OpenClaw subprocess terminated or timed out "
           "after prompt acceptance; external side effects may have occurred."
       ),
       output={
           "ambiguous": True,
           "phase": "in_flight_prompt_accepted",
           "session_id": session_id,
           "accumulated_text": accumulated_chunks,
       },
   )
   ```

### 4.3 Kernel Lifecycle Integration & PostgreSQL Test Specification
In Step 14b, `WorkerExecutionKernel.fail(task, result)` in `src/personaos/execution/kernel.py` is updated as follows:
```python
async def fail(self, task: Task, result: Result) -> None:
    expected_gen = self._generation if self._generation > 0 else getattr(task, "generation", 1)
    expected_worker = self._worker_id or task.assigned_worker

    is_ambiguous = bool(result.output and result.output.get("ambiguous") is True)
    is_non_idempotent = getattr(task, "side_effect_classification", "UNKNOWN") in ("UNKNOWN", "HAS_SIDE_EFFECTS")

    # Routing invariant: non-idempotent ambiguous failures MUST quarantine to 'review'
    if is_ambiguous and is_non_idempotent:
        target_status = "review"
        exhaustion_reason = "ambiguous_in_flight_failure"
        completed_at_val = None
    else:
        target_status = "failed"
        exhaustion_reason = None
        completed_at_val = func.now()

    stmt = update(TaskModel).where(
        TaskModel.id == task.id,
        TaskModel.generation == expected_gen,
    )
    if expected_worker:
        stmt = stmt.where(TaskModel.assigned_worker == expected_worker)

    res = await self._session.execute(
        stmt.values(
            status=target_status,
            completed_at=completed_at_val,
            result=result.output if result.output else ({"error": result.error} if result.error else None),
            last_error=result.error,
            exhaustion_reason=exhaustion_reason,
            lock_expires_at=None,
        )
    )
    if res.rowcount == 0:
        raise StaleWorkerError(f"task {task.id} was reclaimed; write rejected")
```

#### Step 14b PostgreSQL Integration Test: `test_kernel_routes_ambiguous_failure_to_review`
In `tests/integration/test_worker_execution_kernel.py`:
1. Register a worker with `runtime="openclaw"`.
2. Enqueue a task with `side_effect_classification="HAS_SIDE_EFFECTS"`.
3. Configure an adapter that simulates prompt acceptance, writes a resource side effect, and returns an ambiguous failure `Result(status="failed", error="Subprocess crash", output={"ambiguous": True})`.
4. Call `kernel.execute(context)`.
5. Assert PostgreSQL database state:
   - `task.status == "review"`
   - `task.exhaustion_reason == "ambiguous_in_flight_failure"`
   - `task.lock_expires_at IS NULL`
   - `task.completed_at IS NULL`
6. Assert that subsequent `workload.next_runnable()` returns `None`, proving that automated reclaim and duplicate side effects are prevented.

---

## 5. Bounded Subprocess Cleanup State Machine

```
Cancellation / Shutdown Triggered (timeout, reclaim, or error)
                       |
                       v
         Stage 1: Protocol Cancellation (session/cancel)
         - Budget: 2.0 seconds
         - Send notification: {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}}
         - Await settlement of in-flight session/prompt future with stopReason=="cancelled"
         - Close stdin: proc.stdin.close()
                       | (if proc still alive after 2.0s)
                       v
         Stage 2: Graceful OS Signal (SIGTERM)
         - Budget: 2.0 seconds
         - proc.send_signal(signal.SIGTERM)
         - Await proc.wait()
                       | (if proc still alive after 2.0s)
                       v
         Stage 3: Forced Escalation Kill (SIGKILL)
         - Budget: 1.0 second
         - proc.send_signal(signal.SIGKILL)
         - Await proc.wait()
                       |
                       v
         Stage 4: Pipe & Task Drain
         - Budget: 1.0 second
         - Cancel and await stdout_task and stderr_task
         - Close file descriptors
                       |
                       v
         Stage 5: Pending Futures & Cancellation Signal Preservation
         - Cancel all futures in _pending_requests
         - Zero-shred temporary token file and unlink
         - If triggered by asyncio.CancelledError, RE-RAISE asyncio.CancelledError
```

Total bounded cleanup budget: **6.0 seconds maximum**.

---

## 6. Acceptance Test Classification & Milestone Matrix

| Test Name | Test Tier | Skip Reason in Step 14a | Activation Milestone |
|:---|:---|:---|:---|
| `test_openclaw_adapter_conforms_to_protocol` | Unit (Mock) | N/A (Active) | Step 14a (PASS) |
| `test_openclaw_adapter_successful_execution` | Unit (Mock) | Implementation stub in Step 14a | **Mandatory in Step 14b Milestone 1** |
| `test_openclaw_adapter_handles_runtime_error` | Unit (Mock) | Implementation stub in Step 14a | **Mandatory in Step 14b Milestone 2** |
| `test_openclaw_adapter_handles_timeout` | Unit (Mock) | Implementation stub in Step 14a | **Mandatory in Step 14b Milestone 2** |
| `test_openclaw_adapter_handles_subprocess_crash` | Unit (Mock) | Implementation stub in Step 14a | **Mandatory in Step 14b Milestone 2** |
| `test_openclaw_adapter_cancellation_cleans_up_process` | Unit (Mock) | Implementation stub in Step 14a | **Mandatory in Step 14b Milestone 2** |
| `test_openclaw_adapter_redacts_credentials_from_logs` | Unit (Mock) | Implementation stub in Step 14a | **Mandatory in Step 14b Milestone 2** |
| `test_openclaw_adapter_ambiguous_in_flight_failure_flag` | Unit (Mock) | Implementation stub in Step 14a | **Mandatory in Step 14b Milestone 2** |
| `test_openclaw_adapter_fails_fast_on_malformed_stdout` | Unit (Mock) | Implementation stub in Step 14a | **Mandatory in Step 14b Milestone 2** |
| `test_kernel_routes_ambiguous_failure_to_review` | Integration (Postgres) | Planned for Step 14b | **Mandatory in Step 14b Milestone 2** |
| `test_openclaw_live_execution` | Integration (Live Gateway) | Requires running gateway on 18789 | **Step 14b Milestone 3 Live Gate** |
