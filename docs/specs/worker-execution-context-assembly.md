# Specification: Worker Execution Context Assembly, Dependency Artifact Piping, & Persona Conditioning

**Module:** `personaos.execution`, `personaos.execution.adapters`
**Related ADR:** ADR 0022, ADR 0003, ADR 0008, ADR 0009, ADR 0014, ADR 0017, ADR 0021
**Source of Truth:** `schemas/worker.yaml`, `schemas/task.yaml`, `VIKUNJA.md`

---

## 1. Architecture & Execution Flow

```
                  PostgreSQL Tasks & Profile Files
                   ┌────────────────────────┐
                   │ Upstream Tasks (Done)  │
                   │ Worker Profile (vN)    │
                   │ Prior Experience (Done)│
                   └───────────┬────────────┘
                               │
                               ▼
               ┌─────────────────────────────────┐
               │    ExecutionContextAssembler     │
               │  - Reads upstream tasks.result  │
               │  - Substitutes $dep:... inputs  │
               │  - Reads prior task summaries   │
               └───────────────┬─────────────────┘
                               │
                               ▼
               ┌─────────────────────────────────┐
               │     WorkerExecutionContext      │
               │  - worker + profile (Persona)   │
               │  - task (resolved inputs)       │
               │  - upstream_results             │
               │  - recent_experience            │
               └───────────────┬─────────────────┘
                               │
                               ▼
               ┌─────────────────────────────────┐
               │      WorkerExecutionKernel      │
               │  - Pre-execution policy check   │
               │  - Leased execution loop        │
               └───────────────┬─────────────────┘
                               │
                               ▼
               ┌─────────────────────────────────┐
               │        OpenClawAdapter          │
               │  [WORKER IDENTITY & PERSONA]    │
               │  [UPSTREAM ARTIFACTS]           │
               │  [RECENT EXPERIENCE]            │
               │  [TASK OBJECTIVE & INPUT]       │
               │  [RESUMPTION CONTEXT]           │
               └─────────────────────────────────┘
```

---

## 2. Component Contracts & Interfaces

### 2.1 `WorkerExecutionContext` Extended Contract
- **File:** `src/personaos/execution/context.py`
- **Fields:**
  - `worker: Worker`
  - `profile: WorkerProfile`
  - `task: Task`
  - `upstream_results: dict[str, dict[str, Any]] = field(default_factory=dict)`
  - `recent_experience: list[dict[str, Any]] = field(default_factory=list)`

### 2.2 `ExecutionContextAssembler` Contract
- **File:** `src/personaos/execution/assembler.py`
- **Methods:**
  - `__init__(session: AsyncSession, registry: ProfileRegistry | None = None)`
  - `async def enrich(context: WorkerExecutionContext, max_recent_experience: int = 3) -> WorkerExecutionContext`:
    Takes an existing `WorkerExecutionContext`, queries PostgreSQL for upstream completed task results matching `context.task.dependencies`, substitutes dynamic references in `context.task.input`, queries the last `max_recent_experience` completed tasks for `context.worker.id`, and returns a new enriched `WorkerExecutionContext`.
  - `async def assemble(task: Task, profile_version_override: int | None = None, max_recent_experience: int = 3) -> WorkerExecutionContext`:
    Loads the worker profile snapshot via `ProfileRegistry.resolve_execution_context`, then performs enrichment.
  - `static def resolve_input_references(task_input: Any, upstream_results: dict[str, dict[str, Any]]) -> Any`:
    Recursively scans dictionaries and lists. Evaluates `$dep:<task_id>.<path>` and `$dep:<task_id>` expressions.

### 2.3 Reference Syntax Specification
- **Full Object Reference:**
  `"$dep:task-01"` -> Replaced with the complete `result` dictionary of `task-01`.
- **Property Reference:**
  `"$dep:task-01.output.cluster_id"` -> Evaluates JSON path navigation: `upstream_results["task-01"]["output"]["cluster_id"]`.
- **Type Preservation:**
  If the input value is strictly the expression string, the resolved value retains its native type (e.g. integer, boolean, list, dict).
- **Embedded String Interpolation:**
  If the expression is embedded in a larger string (e.g. `"Cluster is $dep:task-01.cluster_id"`), replaces the placeholder with `str(value)`.
- **Error Handling:**
  If a referenced task ID is missing from `upstream_results` or a key path does not exist, raises `DependencyArtifactResolutionError` with a diagnostic message detailing the task ID, path, and available keys.

### 2.4 Adapter Prompt Ingestion
- **File:** `src/personaos/execution/adapters/openclaw.py`
- Prompt is constructed from modular sections:
  1. `[WORKER IDENTITY & PERSONA]` (Worker ID, name, role, autonomy, style, communication, goals, constraints, role instructions, skills).
  2. `[UPSTREAM DEPENDENCY ARTIFACTS]` (JSON-serialized dependency outputs).
  3. `[RECENT WORKER EXPERIENCE]` (JSON-serialized prior completed task summaries).
  4. `Objective` and `Input` (with all `$dep` references substituted).
  5. `[RESUMPTION CONTEXT]` (intermediate checkpoint state if resuming).

---

## 3. Concurrency & Threat Analysis

| Threat / Risk | Impact | Mitigation |
| :--- | :--- | :--- |
| **Missing Upstream Dependency Output** | Downstream task executes with uninitialized or `None` parameters, corrupting downstream state. | `resolve_input_references` strictly verifies existence; raises `DependencyArtifactResolutionError` before the kernel dispatches to the adapter. |
| **Circular or Malicious Expression Injection** | Prompt injection or deep recursion attack via input strings. | Regex-bounded token matching (`r"\$dep:([a-zA-Z0-9_\-]+)(?:\.([a-zA-Z0-9_\.\-]+))?"`) with maximum depth validation. |
| **Secret Leakage via Upstream Artifacts** | Upstream task output contains unredacted credentials or token strings. | The existing `SecretsManager` continues to execute in `kernel.py`, scanning and redacting secrets from inputs, outputs, and prompts. |
| **Excessive Prompt Overhead** | Too many prior tasks flood LLM context window. | `max_recent_experience` defaults to 3, and only extracts high-level result summaries (`summary` key or truncated string representation). |

---

## 4. Acceptance Criteria & Test Matrix

- **CTX-01:** `test_assemble_resolves_profile_and_persona`: Context assembler enriches worker, profile, and persona.
- **CTX-02:** `test_upstream_dependency_results_loaded`: Downstream task context includes upstream task results.
- **CTX-03:** `test_input_expression_substitution`: Resolves `$dep:task_id.key` with native typing and string interpolation.
- **CTX-04:** `test_missing_dependency_result_handling`: Raises `DependencyArtifactResolutionError` on missing dependency or invalid key.
- **CTX-05:** `test_recent_experience_retrieval`: Loads worker's last completed tasks ordered by `completed_at DESC`.
- **CTX-06:** `test_openclaw_prompt_contains_persona_and_instructions`: ACP runner formats `[WORKER IDENTITY & PERSONA]`.
- **CTX-07:** `test_openclaw_prompt_contains_upstream_artifacts`: ACP runner formats `[UPSTREAM DEPENDENCY ARTIFACTS]`.
- **CTX-08:** `test_compute_replacement_reconstructs_full_context`: Replacement compute node reproduces identical enriched context.
