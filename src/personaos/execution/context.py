"""WorkerExecutionContext — frozen bundle of worker + snapshot profile + task.

Per ADR 0009: adapters receive this context, never a bare task.
The profile is loaded from task.profile_version (NOT worker.current_profile_version),
making the snapshot contract a type-system invariant.

If you find yourself wanting to add a `current_profile_version` field here,
stop. The whole point of this type is that there is no way to reach the
live state from the context. Step 4 closes the loop on Step 3.
"""

from __future__ import annotations

from dataclasses import dataclass

from personaos.domain.task import Task
from personaos.domain.worker import Worker, WorkerProfile


@dataclass(frozen=True)
class WorkerExecutionContext:
    """Bundle of worker + snapshot profile + task.

    The profile is loaded via
    ProfileRegistry.get_profile_version(worker.id, task.profile_version).
    It is the historical contract — never worker.current_profile_version.

    Adapters receive this context, never a bare task, so the snapshot
    contract is in the type system: an adapter cannot accidentally read
    current state because that field is not on this context.
    """

    worker: Worker
    profile: WorkerProfile
    task: Task
