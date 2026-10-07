# WorkerProfile Specification — Versioned Worker Configuration

**Status:** Draft v0.1 (2026-10-07)
**Companion to:** [ADR 0008 Addendum — WorkerProfile Versioning](../adr/0008-addendum-worker-profile-versioning.md)

## Overview

This document specifies the versioned WorkerProfile layer for PersonaOS v0.1. It defines:

1. The conceptual model (Worker, WorkerProfile, Persona)
2. The YAML schema
3. The filesystem layout
4. The service-layer operations
5. The REST API
6. The activation semantics
7. The task snapshot mechanism
8. The invariants and what's deferred
9. The implementation order

This is a **spec document, not implementation**. Code comes after sign-off on this doc.

## 1. Concepts

### Worker

Stable identity. Has a stable `id` and `name`. May persist across model changes, runtime swaps, and configuration evolution.

### Persona

The **behavioral identity** of a worker — style, communication, autonomy, behavioral rules, goals, constraints. Independent of operational concerns (which model, which runtime, which skills).

Example: "cautious, concise, analytical, prefers automation, asks when ambiguous."

### WorkerProfile

A **versioned, immutable assignment** of a Persona + operational config to a Worker. Contains:

```
WorkerProfile (versioned)
├── Identity        (role, description)
├── Persona         (behavioral — style, behavior, goals, constraints)
├── Model           (LLM model reference — placeholder in v0.1)
├── Instructions    (system prompt)
├── Skills          (snapshot of worker_skills for this version)
├── Permissions     (snapshot of workers.permissions for this version)
└── Runtime config  (runtime adapter, max_concurrent_tasks)
```

A Worker has a `current_profile_version` pointer that says "WorkerProfile vN is active." To change anything, you create v(N+1) — never edit N.

### Why separate Persona from WorkerProfile?

Persona = **who** the worker is (behavioral identity).  
WorkerProfile = **how** the worker is configured (everything operational, including which Persona is assigned).

A worker's persona (cautious, analytical) is conceptually stable across most operational changes. A worker might switch from OpenClaw to a future runtime, change skills, or adjust `max_concurrent_tasks` — without those being "persona changes."

Conflating them creates a single configuration object that grows unboundedly. The separation keeps each layer focused and prevents "persona" from absorbing everything.

## 2. YAML Schema

```yaml
# profile.yaml — a single WorkerProfile version
version: 2                   # REQUIRED. Integer. Monotonically increasing per worker.

# Identity
role: devops                 # REQUIRED. One of: devops, developer, qa, architect, security, custom
description: |              # OPTIONAL. One-line role description.
  Infrastructure and deployment specialist

# Persona — the behavioral identity
persona:                     # REQUIRED for v0.1
  style: cautious            # free-form string. Communication / decision style.
  communication: concise     # free-form string. Output verbosity / format preference.
  autonomy: medium           # one of: low | medium | high
  behavior:                  # OPTIONAL. Behavioral rules (truthy flags or values).
    verify_before_destructive_action: true
    prefer_automation: true
    ask_when_ambiguous: true
  goals:                     # OPTIONAL. List of objectives.
    - reliable deployments
    - minimize production risk
  constraints:               # OPTIONAL. Hard rules.
    - never delete production resources without approval

# Operational configuration
model: claude-sonnet         # OPTIONAL. Placeholder for v0.1; not yet used by adapters.
instructions: |              # OPTIONAL. System prompt for the worker.
  You are a DevOps engineer...
skills:                      # OPTIONAL. Snapshot of worker_skills for this version.
  - kubernetes
  - linux
  - ansible
permissions:                # OPTIONAL. Snapshot of permissions for this version.
  - staging.read
  - staging.deploy
runtime: openclaw            # REQUIRED. Adapter identifier (v0.1: openclaw only).
max_concurrent_tasks: 2      # REQUIRED. Capacity limit. 1..16.

# Metadata
metadata:                    # OPTIONAL. Operational metadata.
  author: system             # who created this version
  created_at: 2026-10-07T12:50:00Z
  notes: "More cautious after incident 2026-09"
```

### Minimal valid profile

```yaml
version: 1
role: devops
persona:
  style: cautious
  autonomy: medium
runtime: openclaw
max_concurrent_tasks: 1
```

### Field validation rules (manual for v0.1)

| Field | Required | Constraints |
|---|---|---|
| `version` | yes | integer ≥ 1; monotonic per worker |
| `role` | yes | one of `devops` / `developer` / `qa` / `architect` / `security` / `custom` |
| `description` | no | free text |
| `persona` | yes | object with at least `style` or `autonomy` |
| `persona.style` | no | free text (e.g., "cautious", "concise", "methodical") |
| `persona.communication` | no | free text (e.g., "concise", "verbose", "structured") |
| `persona.autonomy` | no | `low` / `medium` / `high` |
| `persona.behavior` | no | map of truthy flags |
| `persona.goals` | no | list of strings |
| `persona.constraints` | no | list of strings |
| `model` | no | string (placeholder for v0.1) |
| `instructions` | no | string (system prompt) |
| `skills` | no | list of strings |
| `permissions` | no | list of strings |
| `runtime` | yes | adapter identifier |
| `max_concurrent_tasks` | yes | integer 1..16 |
| `metadata` | no | map |

Pydantic-based schema validation is deferred to v0.2 (see Section 9).

## 3. Filesystem Layout

```
profiles/
├── worker-devops-001/
│   ├── v1.yaml
│   ├── v2.yaml
│   └── v3.yaml        ← workers.current_profile_version points here
├── worker-developer-001/
│   └── v1.yaml
└── worker-qa-001/
    └── v1.yaml
```

Rules:

- One directory per `worker_id`
- Files named `v{N}.yaml` where N is a positive integer
- Files are written **once** and never modified
- Missing file = that version doesn't exist
- Directory created automatically on first write

File-system layout is the v0.1 source of truth. Future versions may move this to a DB table (see Section 9 deferred items).

## 4. Operations (service layer)

`src/personaos/registry/profile.py`:

```python
@dataclass(frozen=True)
class ProfileVersion:
    worker_id: str
    version: int
    path: Path  # profiles/{worker_id}/v{version}.yaml


class ProfileRegistry:
    """Owns the profile lifecycle. The ONLY writer to profiles/."""

    def register_worker(
        self,
        worker_id: str,
        initial_profile: WorkerProfile,
    ) -> None:
        """Write v1.yaml + insert workers row."""
        ...

    def get_worker(self, worker_id: str) -> Worker:
        """Read workers row + current_profile_version."""
        ...

    def get_current_profile(self, worker_id: str) -> WorkerProfile:
        """Load profiles/{id}/v{current_profile_version}.yaml + workers row."""
        ...

    def get_profile_version(
        self,
        worker_id: str,
        version: int,
    ) -> WorkerProfile:
        """Load a specific historical version (immutable read)."""
        ...

    def create_profile_version(
        self,
        worker_id: str,
        profile: WorkerProfile,
    ) -> ProfileVersion:
        """Validate profile, write v(N+1).yaml. Does NOT activate."""
        ...

    def activate_profile_version(
        self,
        worker_id: str,
        version: int,
    ) -> None:
        """Atomic: validate file → update workers table → bump pointer.
        The file at profiles/{id}/v{version}.yaml is NEVER touched."""
        ...

    def list_profile_versions(self, worker_id: str) -> list[int]:
        """Read directory, return [1, 2, 3] sorted."""
        ...
```

### Atomicity guarantee

`activate_profile_version` is a single transaction:

1. Validate file exists and parses
2. `BEGIN TRANSACTION`
3. `UPDATE workers SET role, personality, capabilities, permissions, runtime, max_concurrent_tasks, current_profile_version = ? WHERE id = ?`
4. `COMMIT`

If any step fails, the transaction rolls back. The file is never touched.

### Immutability guarantee

`create_profile_version` rejects overwrites:

```python
def create_profile_version(self, worker_id: str, profile: WorkerProfile) -> ProfileVersion:
    target = self._path(worker_id, profile.version)
    if target.exists():
        raise ProfileVersionExistsError(
            f"profile {target} already exists; create v{profile.version + 1} instead"
        )
    self._write_yaml(target, profile)
    return ProfileVersion(worker_id=worker_id, version=profile.version, path=target)
```

`activate_profile_version` never writes to the file. Activation only updates the DB pointer.

## 5. REST API

```
POST   /workers                              # register + create v1 from inline profile
GET    /workers/{id}                         # current state + current_profile_version
GET    /workers/{id}/persona                 # current resolved profile (workers row + vN.yaml)
POST   /workers/{id}/persona                 # create new version (write file, don't activate)
GET    /workers/{id}/persona/versions        # list all versions on disk
GET    /workers/{id}/persona/{v}            # read specific historical version
POST   /workers/{id}/persona/{v}/activate    # atomic activation
```

Request/response shapes deferred to implementation.

## 6. Activation Semantics

`POST /workers/{id}/persona/{v}/activate`:

1. Validate `profiles/{id}/v{v}.yaml` exists and parses
2. Load profile fields into a `WorkerProfile` dataclass
3. **Atomic DB update** (single transaction):
   - `UPDATE workers SET role, personality, capabilities, permissions, runtime, max_concurrent_tasks, current_profile_version = v WHERE id = ?`
4. New tasks enqueued from this point forward get `tasks.profile_version = v`
5. The file at `profiles/{id}/v{v}.yaml` is never touched

Edge cases:

| Case | Behavior |
|---|---|
| File doesn't exist | 404 / error |
| File parses but invalid | 422 / error |
| `v` already active | No-op (idempotent) |
| `v` less than current | Allowed — this is a rollback |
| `v` greater than current | Allowed — this is an upgrade |
| Concurrent activations | Database transaction isolation prevents partial updates |

## 7. Task Snapshot

When a task is enqueued:

```sql
INSERT INTO tasks (..., profile_version, ...)
VALUES (..., (SELECT current_profile_version FROM workers WHERE id = ?), ...);
```

`tasks.profile_version` is captured at enqueue and **never updated**. This is the snapshot.

> **Wording distinction (Liang, 2026-10-07):** *The profile version records the configuration ASSIGNED to the task, not necessarily the configuration that ultimately EXECUTED it.* This distinction will matter when retries/failover/execution attempts are introduced in v0.2+ — the snapshot is "what was active when this task was assigned," not "what ran."

To reproduce a task's behavior:
1. Load `tasks.profile_version` (e.g., `2`)
2. Load `profiles/{worker_id}/v2.yaml` (immutable, forever)
3. Run the task with that exact persona + config

This enables:

- **Audit**: "why did this task fail?" → load the exact persona it ran with
- **A/B testing**: assign half the workers v2, half v3, compare task outcomes
- **Rollback analysis**: "what was the persona when this incident happened?"
- **Reproducibility**: same persona + same input → same behavior (modulo LLM non-determinism)

## 8. Invariants

These hold without exception:

| # | Invariant | Enforcement |
|---|---|---|
| 1 | A worker has **one active profile version** | `workers.current_profile_version` is a single integer; activation sets it atomically |
| 2 | A profile version is **immutable after activation** | Files written once, no in-place edits; registry rejects version-overwrite |
| 3 | Every task captures the exact **profile version** used | `tasks.profile_version` set at enqueue, never updated |
| 4 | Historical tasks remain reproducible | `tasks.profile_version` + immutable file = exact persona used |
| 5 | Activation is **atomic** | Single transaction; all-or-nothing |
| 6 | The registry owns profile lifecycle | No direct file edits; all writes via registry |

## 9. What's Deferred (not in v0.1)

| Deferred | Rationale |
|---|---|
| `worker_profile_versions` DB table | Defer until SQL-level versioning is needed (audit reports, time-travel queries) |
| `tasks.execution_node`, `tasks.attempt`, `tasks.lease_expires_at` | Per ADR 0008 — the **failover acceptance test** drives the shape. Don't pre-design. |
| Profile diff UI | Not in scope for v0.1 |
| Automatic promotion / canary rollouts | Not in scope for v0.1 |
| Profile inheritance / composition | Not in scope for v0.1 |
| LLM model selection (`model` field) | Placeholder only in v0.1; only one runtime (`openclaw`) |
| Pydantic-based YAML schema validation | Manual review for v0.1; generated pydantic from JSON Schema in v0.2 |
| Profile search / discovery | Registry knows by `worker_id` only |
| Multi-region profile replication | Not in scope for v0.1 |

## 10. Example Usage

```python
# Register a new worker with initial profile (v1)
registry.register_worker(
    worker_id="worker-devops-001",
    initial_profile=WorkerProfile(
        version=1,
        role="devops",
        persona=Persona(style="cautious", autonomy="medium"),
        runtime="openclaw",
        max_concurrent_tasks=2,
    ),
)

# Create v2 — change persona (more cautious)
registry.create_profile_version(
    worker_id="worker-devops-001",
    profile=WorkerProfile(
        version=2,
        role="devops",
        persona=Persona(
            style="cautious",
            autonomy="low",  # changed: more conservative
            constraints=["never delete production resources without approval"],
        ),
        runtime="openclaw",
        max_concurrent_tasks=2,
    ),
)

# Activate v2
registry.activate_profile_version(
    worker_id="worker-devops-001",
    version=2,
)

# Read current state
worker = registry.get_worker("worker-devops-001")
assert worker.current_profile_version == 2

# Read historical v1 (still available — immutable)
v1 = registry.get_profile_version("worker-devops-001", 1)
assert v1.persona.autonomy == "medium"  # unchanged from creation

# List versions
versions = registry.list_profile_versions("worker-devops-001")
assert versions == [1, 2]

# Enqueue a task — tasks.profile_version captures v2
task = workload.enqueue(
    task_type="deploy_service",
    input={"version": "1.4.2"},
    worker_id="worker-devops-001",
)
assert task.profile_version == 2  # snapshot

# Later, activate v3 — task still references v2
registry.create_profile_version(...)
registry.activate_profile_version("worker-devops-001", 3)

# Reproduce the original task's behavior
original_task = workload.get(task.id)
v2_profile = registry.get_profile_version(
    "worker-devops-001", original_task.profile_version
)
runner.execute(task, profile=v2_profile)  # exact same persona as before
```

## 11. File Structure in Repo

```
personaos/
├── docs/
│   ├── adr/
│   │   └── 0008-addendum-worker-profile-versioning.md   ← this spec's ADR
│   └── specs/
│       └── worker-profile.md                            ← this document
├── profiles/                                              ← versioned profiles (NEW)
│   ├── worker-devops-001/
│   │   └── v1.yaml
│   ├── worker-developer-001/
│   │   └── v1.yaml
│   └── worker-qa-001/
│       └── v1.yaml
├── src/personaos/
│   ├── domain/
│   │   └── worker.py                ← add WorkerProfile + Persona dataclasses
│   ├── registry/
│   │   └── profile.py               ← NEW — ProfileRegistry service
│   └── db/
│       └── models.py                ← add current_profile_version + profile_version
└── tests/
    └── integration/
        └── test_profile_versioning.py  ← NEW — immutability + atomicity + snapshot tests
```

Note: `examples/` stays separate from `profiles/`. `examples/` holds seed YAMLs for initial DB population (no version); `profiles/` holds versioned profile documents.

## 12. Implementation Order (matches agreed plan)

1. **ADR addendum** — `docs/adr/0008-addendum-worker-profile-versioning.md` (this ADR)
2. **Spec doc** — `docs/specs/worker-profile.md` (this document)
3. **DB migration** — `db/migrations/versions/0002_profile_version.py` adds the two columns
4. **ORM updates** — `db/models.py` adds `current_profile_version` + `profile_version`
5. **Domain objects** — `domain/worker.py` adds `WorkerProfile` + `Persona` dataclasses
6. **Profile files** — initial v1 YAMLs for the 3 example workers in `profiles/`
7. **Registry service** — `registry/profile.py` with the 6 operations
8. **Tests** — immutability of profile files, activation atomicity, task snapshot correctness
9. **Migration test** — `tests/integration/test_profile_versioning.py` verifies the new migration

After sign-off on this spec, implementation proceeds in this order.
