# ADR 0022: Worker Execution Context Assembly, Dependency Artifact Piping, and Persona-Conditioned Runtime Execution

**Status:** Accepted (Step 19 Implementation Phase)
**Date:** 2026-10-23
**Deciders:** PersonaOS Architecture Team
**Consulted:** Workload Execution, Runtime Adapters, Security, Control Plane
**Informed:** Operators, Developers

---

## Context & Problem Statement

PersonaOS operates on the founding thesis: **"Compute is replaceable; the worker is persistent."**
The framework's purpose is to build persistent AI workers whose identity, skills, experience, memory, and workload survive changes in execution compute.

Through Steps 1 to 18:
- Worker identities, skills, and immutable profile snapshots (`Persona`, `WorkerProfile`) were formalized (ADRs 0003, 0008, 0009).
- Task dependency DAGs were introduced to gate task runnability (ADR 0014).
- Checkpoint recovery across replacement compute nodes was established (ADR 0021).
- Fail-closed security policies, tool authorization, and quarantine controls were enforced (ADR 0020).

However, an audit of `src/personaos/execution/` and `src/personaos/execution/adapters/openclaw.py` reveals two critical execution-boundary gaps:
1. **Persona & Behavioral Profile Shedding**:
   While `WorkerExecutionContext` loads `WorkerProfile`, runtime adapters (such as `OpenClawAdapter`) only format `task.objective` and `task.input` into the prompt. The worker's `role`, `instructions`, and `Persona` (`style`, `communication`, `autonomy`, `goals`, `constraints`) are completely omitted. When compute is replaced, the new node executes tasks as an unconditioned, generic LLM.
2. **Upstream Dependency Artifact Blindness**:
   `tasks.dependencies` tracks predecessor task IDs to gate when a task becomes runnable (`WorkloadService.next_runnable`). However, when a downstream task executes, it has zero visibility into the results (`tasks.result`) or outputs produced by those predecessor tasks. Multi-stage workflows cannot pipe data between tasks through the durable store.
3. **No Dynamic Input Expression Resolution**:
   Tasks cannot express dynamic bindings to upstream outputs (e.g. `{"cluster_id": "$dep:task-deploy.cluster_id"}`). Downstream tasks require manual out-of-band input parameterization.
4. **Worker Experience Amnesia**:
   When a worker executes sequential tasks, it receives no historical summary of its recent completed tasks on the same project or workload, preventing behavioral continuity across task boundaries.

---

## Decision

We will implement **Worker Execution Context Assembly and Persona Conditioning** in the kernel and adapter layers:

### 1. Extended WorkerExecutionContext Domain Model
Extend `WorkerExecutionContext` in `src/personaos/execution/context.py` to include:
- `upstream_results: dict[str, dict[str, Any]]`: Mapping of `dependency_task_id -> task.result`.
- `recent_experience: list[dict[str, Any]]`: Summary of the worker's last $N$ completed tasks on the same workload (task ID, title, status, concise result summary).

### 2. Execution Context Assembler (`ExecutionContextAssembler`)
Introduce `ExecutionContextAssembler` in `src/personaos/execution/assembler.py`:
- Queries PostgreSQL for all `tasks.id IN (task.dependencies)` where `status = 'completed'` and gathers their `result` payloads.
- Resolves dynamic references in `task.input`:
  - Syntax: `$dep:<task_id>.<json_path>` (e.g., `$dep:task-deploy.cluster_id`) or `$dep:<task_id>` (entire result dict).
  - Recursively substitutes placeholders with verified upstream values before passing input to the adapter.
  - If a referenced dependency output is missing or key cannot be resolved, raises `DependencyArtifactResolutionError` to fail-safe before execution.
- Gathers recent completed tasks for `task.assigned_worker` to provide immediate execution continuity across compute restarts.
- Exposes `enrich(context)` for existing kernel pipelines and `assemble(task)` for direct resolution.

### 3. Kernel Integration
In `WorkerExecutionKernel`:
- In both `execute()` and `reclaim()` paths, the kernel invokes `ExecutionContextAssembler.enrich(context)` prior to pre-execution policy evaluation and adapter dispatch.
- Ensures replacement compute nodes receive identical upstream artifacts, worker persona conditioning, and recent experience.

### 4. Persona-Conditioned Prompt Framing in Runtime Adapters
Update `OpenClawAdapter` and `_OpenClawProcessRunner`:
- Injects a structured `[WORKER IDENTITY & PERSONA]` block into the initial ACP session prompt:
  ```
  [WORKER IDENTITY & PERSONA]
  Worker ID: {worker.id} ({worker.name})
  Role: {profile.role}
  Autonomy: {persona.autonomy} | Style: {persona.style} | Communication: {persona.communication}
  Goals: {persona.goals}
  Constraints: {persona.constraints}
  Role Instructions: {profile.instructions}
  Skills: {profile.skills}
  ```
- Injects an `[UPSTREAM DEPENDENCY ARTIFACTS]` block containing resolved outputs from completed dependencies.
- Injects a `[RECENT WORKER EXPERIENCE]` block containing summaries of prior completed tasks.
- Retains existing `[RESUMPTION CONTEXT]` from ADR 0021 when resuming from intermediate checkpoints.

### 5. Zero Database Migrations
All required data is already present in the existing v0.1 schema:
- `tasks.dependencies` (JSONB)
- `tasks.result` (JSONB)
- `tasks.assigned_worker` (TEXT)
- Worker profile YAMLs on disk
No DDL alterations or migration scripts are required.

---

## Preserved Invariants

1. **Kernel Claim and Fencing Authority**: `WorkerExecutionKernel` remains the sole authority for claims, generation increments, lease checks, and completion.
2. **Snapshot Immutability**: The worker profile is loaded strictly from `task.profile_version` (or explicit replay override), preserving historical audit integrity.
3. **Fail-Closed Artifact Safety**: Missing dependency outputs or unresolvable references immediately halt execution via `DependencyArtifactResolutionError` rather than proceeding with corrupted inputs.
4. **Adapter Isolation**: OpenClaw remains an external process; communication occurs strictly over stdio ACP JSON-RPC.

---

## Consequences

### Positive
- Enforces behavioral consistency: A DevOps worker with `autonomy="low"` and `style="cautious"` behaves according to its profile on any compute node.
- Enables multi-task pipelines: Downstream tasks consume outputs from completed upstream tasks natively through the durable store.
- Strengthens compute replaceability: A replacement node receives the full behavioral identity, upstream artifacts, and prior experience.

### Negative / Trade-offs
- Increases initial prompt token size by ~200-500 tokens.
- Context assembly adds one read query for upstream task rows during claim/preparation.
