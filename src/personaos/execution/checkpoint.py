"""Checkpoint — durable state snapshot per task.

Per the kernel contract (ADR 0010): the kernel calls checkpoint.load() before
adapter.execute() and checkpoint.write() after if the adapter returned
new_checkpoint. The checkpoint_ref on the task is the linkage.

Storage choice (v0.1): the task_checkpoints table in Postgres (jsonb state).
Object storage (S3/MinIO) is a v0.2 option when state blobs get large.

This is a clean persistence abstraction. The kernel knows WHEN to
checkpoint; OpenClaw (or any other adapter) decides WHAT to put in the
state and what format to use.
"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from personaos.db.models import TaskCheckpoint


async def load(session: AsyncSession, ref: Optional[str]) -> Optional[dict]:
    """Load a checkpoint by ref.

    Returns the state dict, or None if:
      - ref is None
      - no checkpoint exists with that ref
    """
    if ref is None:
        return None
    result = await session.execute(
        select(TaskCheckpoint).where(TaskCheckpoint.ref == ref)
    )
    cp = result.scalar_one_or_none()
    if cp is None:
        return None
    return dict(cp.state)


async def write(session: AsyncSession, task_id: str, state: dict) -> str:
    """Write a new checkpoint. Returns the ref.

    The ref is a fresh UUID; the caller should update task.checkpoint_ref
    to point to it (the kernel does this in _finish_completed).
    """
    ref = str(uuid.uuid4())
    cp = TaskCheckpoint(ref=ref, task_id=task_id, state=state)
    session.add(cp)
    await session.flush()
    return ref
