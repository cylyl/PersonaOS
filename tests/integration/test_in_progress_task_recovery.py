"""Step 11a — In-Progress Task Recovery acceptance tests.

Per docs/adr/0016-in-progress-task-recovery.md and
docs/specs/in-progress-task-recovery.md.

These tests define the ACCEPTED BEHAVIOR for v0.2 crash-recovery.
They are written BEFORE the implementation exists. They will fail against
the current codebase because:

  - lock_expires_at column does not exist yet (migration needed)
  - generation column does not exist yet
  - side_effect_classification column does not exist yet
  - kernel.claim() does not set lock_expires_at
  - kernel.complete() / kernel.fail() do not check generation
  - kernel.heartbeat() does not exist
  - kernel.checkpoint_write() does not check generation
  - no orphan-detection query exists in WorkloadService

All tests are SKIPPED until the v0.2 schema migration and production
code are implemented. Once implemented, the skip reason is updated and
each test validates the actual behavior.

Acceptance scenarios (per ADR 0016):

  1. A worker crash is eventually detectable.
  2. A healthy but slow worker is not reclaimed prematurely.
  3. A stale worker cannot overwrite a newer attempt's result (complete).
  4. A stale worker cannot overwrite a newer attempt's result (fail).
  5. A stale worker cannot refresh heartbeat after ownership transferred.
  6. A stale worker cannot write a checkpoint after ownership transferred.
  7. Two recovery workers cannot both acquire ownership (and only one of
     {reclaim, retry-exhaustion} can win for any given stale task).
  8. Checkpoint is cleared on reclaim; recovery starts from scratch.
  9. max_retries limits reclaim attempts: when retry_count >= max_retries
     AND side_effect_classification='IDEMPOTENT', the task transitions to
     'failed' (terminal) with exhaustion_reason='retry_limit_exhausted'.
 10. Tasks with side_effect_classification=UNKNOWN are not auto-reclaimed
     (reclaim predicate requires ='IDEMPOTENT'); at retry exhaustion,
     UNKNOWN tasks route to 'review' status (not silently failed).
 11. Tasks with side_effect_classification=IDEMPOTENT are auto-reclaimed.
 12. Tasks with side_effect_classification=HAS_SIDE_EFFECTS are not
     auto-reclaimed; at retry exhaustion, they route to 'review' status.
 13. Operator-managed recovery remains possible (manual reset to queued).
 14. Operator reset increments generation; stale writes remain fenced.
 15. A fenced heartbeat raises StaleWorkerError.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from personaos.db.models import Task as TaskModel
from personaos.domain.task import Task
from personaos.domain.worker import Persona, WorkerProfile
from personaos.execution.adapters.base import Result
from personaos.execution.context import WorkerExecutionContext
from personaos.execution.kernel import TaskStatusError, WorkerExecutionKernel
from personaos.registry.profile import ProfileRegistry
from personaos.workload.service import WorkloadService

REPO_ROOT = Path(__file__).parents[2]
ALEMBIC_INI = REPO_ROOT / "src" / "personaos" / "db" / "migrations" / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "src" / "personaos" / "db" / "migrations"

pytestmark = pytest.mark.usefixtures("clean_tables")


# ---------- Helpers ----------


def _alembic_config(database_url):
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


async def _register_worker_and_v1_profile(session: AsyncSession, profiles_dir: Path):
    persona = Persona(id="worker-11a")
    profile = WorkerProfile(
        id="profile-11a",
        name="TestProfile",
        version=1,
        description="Step 11a test profile",
        created_at=datetime.now(timezone.utc),
        skills=[],
        capabilities={},
    )
    reg = ProfileRegistry(session, profiles_dir=profiles_dir)
    await persona.register_worker(session)
    await persona.register_profile(session, profile)
    return persona


async def _enqueue(
    session: AsyncSession,
    title: str = "11a test task",
    side_effect_classification: str = "UNKNOWN",
) -> Task:
    """Enqueue a task with explicit side-effect classification.

    classification: 'UNKNOWN' | 'IDEMPOTENT' | 'HAS_SIDE_EFFECTS'
    """
    wl = WorkloadService(session)
    task = await wl.enqueue(
        title=title,
        objective="Step 11a acceptance test",
        input={},
        priority="medium",
    )
    if side_effect_classification != "UNKNOWN":
        await session.execute(
            update(TaskModel)
            .where(TaskModel.id == task.id)
            .values(side_effect_classification=side_effect_classification)
        )
        await session.commit()
    return task


async def _status(session: AsyncSession, task_id: str) -> str:
    row = await session.execute(
        select(TaskModel.status).where(TaskModel.id == task_id)
    )
    return row.scalar_one()


async def _task_row(session: AsyncSession, task_id: str) -> TaskModel:
    row = await session.execute(select(TaskModel).where(TaskModel.id == task_id))
    return row.one()


async def _build_kernel(session: AsyncSession, adapter, persona: Persona):
    """Build a WorkerExecutionKernel for the given persona."""
    return WorkerExecutionKernel(
        session=session,
        adapter=adapter,
        worker_id=persona.id,
        profiles_dir=profiles_dir,  # provided via profiles_dir fixture
    )


# ---------- Test doubles ----------


class OkAdapter:
    """Adapter that returns completed with configurable output."""

    def __init__(self, output: dict | None = None):
        self.output = output or {}

    async def execute(self, context: WorkerExecutionContext, checkpoint=None) -> Result:
        return Result(status="completed", output=self.output, checkpoint=checkpoint)


class SlowAdapter:
    """Adapter that simulates a slow worker; returns completed after a delay."""

    def __init__(self, delay: float = 0.1, output: dict | None = None):
        self.delay = delay
        self.output = output or {}

    async def execute(self, context: WorkerExecutionContext, checkpoint=None) -> Result:
        await asyncio.sleep(self.delay)
        return Result(status="completed", output=self.output, checkpoint=checkpoint)


class FailAdapter:
    """Adapter that always returns failed."""

    def __init__(self, error: str = "adapter error"):
        self.error = error

    async def execute(self, context: WorkerExecutionContext, checkpoint=None) -> Result:
        return Result(status="failed", error=self.error)


# ---------- AC #1: worker crash is eventually detectable ----------


@pytest.mark.skip(reason="v0.2: requires lock_expires_at column + orphan-detection query; not yet implemented")
async def test_worker_crash_is_eventually_detectable(
    profiles_dir, database_url, session_factory
):
    """Orphan detection finds tasks where lock_expires_at has passed.

    Proves: a crashed worker leaves a task that the system can detect
    as stale, without any operator action.

    Setup:
      1. Worker A claims task (lock_expires_at = now + TTL).
      2. lock_expires_at is backdated to the past.
      3. Orphan detection query is executed.
      4. Assert: the task appears in the stale-tasks result.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as s1:
        persona = await _register_worker_and_v1_profile(s1, profiles_dir)
        task = await _enqueue(s1, title="orphan detection test")
        task_id = task.id

        # Worker A claims (sets lock_expires_at)
        kernel = _build_kernel(s1, OkAdapter(), persona)
        await kernel.execute(task)

        # Backdate lock_expires_at to simulate expired lease
        await s1.execute(
            update(TaskModel)
            .where(TaskModel.id == task_id)
            .values(
                lock_expires_at=datetime.now(timezone.utc) - timedelta(minutes=30)
            )
        )
        await s1.commit()

    # Orphan detection query
    async with session_factory() as s2:
        stale = await s2.execute(
            select(TaskModel.id).where(
                TaskModel.status == "in_progress",
                TaskModel.lock_expires_at < datetime.now(timezone.utc),
            )
        )
        stale_ids = [row[0] for row in stale.fetchall()]
        assert task_id in stale_ids, (
            "Orphan detection must find the expired task"
        )


# ---------- AC #2: healthy slow worker is not reclaimed prematurely ----------


@pytest.mark.skip(reason="v0.2: requires lock_expires_at column + heartbeat; not yet implemented")
async def test_healthy_slow_worker_is_not_reclaimed_prematurely(
    profiles_dir, database_url, session_factory
):
    """A task with a future lock_expires_at is NOT returned by orphan detection.

    Proves: a healthy but slow worker whose lock has not expired is not
    prematurely reclaimed while it is still processing.

    Setup:
      1. Worker A claims task (lock_expires_at = now + 15 min).
      2. Orphan detection runs (lock_expires_at is not yet passed).
      3. Assert: task is NOT in stale list.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as s1:
        persona = await _register_worker_and_v1_profile(s1, profiles_dir)
        task = await _enqueue(s1, title="slow worker test")
        task_id = task.id

        # Worker A claims: sets lock_expires_at to now + 15 min
        kernel = _build_kernel(s1, OkAdapter(), persona)
        await kernel.execute(task)

        # Verify lock_expires_at is in the future
        row = await _task_row(s1, task_id)
        assert row.lock_expires_at is not None
        assert row.lock_expires_at > datetime.now(timezone.utc)

    # Within TTL window: not stale
    async with session_factory() as s2:
        stale = await s2.execute(
            select(TaskModel.id).where(
                TaskModel.status == "in_progress",
                TaskModel.lock_expires_at < datetime.now(timezone.utc),
            )
        )
        stale_ids = [row[0] for row in stale.fetchall()]
        assert task_id not in stale_ids, (
            "Healthy worker must not be reclaimed before lock_expires_at"
        )


# ---------- AC #3: stale worker cannot overwrite newer result (complete) ----------


@pytest.mark.skip(reason="v0.2: requires generation column + fencing check on complete(); not yet implemented")
async def test_stale_worker_cannot_overwrite_newer_result_via_complete(
    profiles_dir, database_url, session_factory
):
    """A fenced worker cannot commit a result via kernel.complete().

    Proves: after reclaim, the stale worker's attempt to call complete()
    with a stale generation is rejected.

    Setup:
      1. Worker A claims (generation=1).
      2. Worker B reclaims (generation=2).
      3. Worker A calls kernel.complete() with generation=1.
      4. Assert: StaleWorkerError raised; task status is still 'completed' from Worker B.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as s1:
        persona_a = await _register_worker_and_v1_profile(s1, profiles_dir)
        task = await _enqueue(s1, title="fencing complete test")
        task_id = task.id

        # Worker A claims and completes
        kernel_a = _build_kernel(s1, OkAdapter(output={"from": "A"}), persona_a)
        await kernel_a.execute(task)

        # Simulate: backdate, revert to in_progress, keep generation=1
        await s1.execute(
            update(TaskModel)
            .where(TaskModel.id == task_id)
            .values(
                status="in_progress",
                result=None,
                completed_at=None,
                lock_expires_at=datetime.now(timezone.utc) - timedelta(minutes=30),
            )
        )
        await s1.commit()

    # Worker B reclaims (generation -> 2)
    async with session_factory() as s2:
        persona_b = await _register_worker_and_v1_profile(s2, profiles_dir)
        kernel_b = _build_kernel(s2, OkAdapter(output={"from": "B"}), persona_b)
        await kernel_b.reclaim(task)  # v0.2 reclaim()
        row = await _task_row(s2, task_id)
        assert row.generation == 2
        assert row.result == {"from": "B"}

    # Worker A (stale, gen=1) attempts complete -> fenced
    async with session_factory() as s3:
        kernel_a2 = _build_kernel(s3, OkAdapter(output={"from": "A-stale"}), persona_a)
        with pytest.raises(StaleWorkerError):
            await kernel_a2.complete(task, Result(status="completed", output={"from": "A-stale"}))

        # Verify: result is still Worker B's result
        row = await _task_row(s3, task_id)
        assert row.result == {"from": "B"}, "Stale result must not overwrite new result"


# ---------- AC #4: stale worker cannot overwrite newer result (fail) ----------


@pytest.mark.skip(reason="v0.2: requires generation column + fencing check on fail(); not yet implemented")
async def test_stale_worker_cannot_overwrite_newer_result_via_fail(
    profiles_dir, database_url, session_factory
):
    """A fenced worker cannot transition a reclaimed task to failed.

    Proves: after reclaim, the stale worker's attempt to call kernel.fail()
    with a stale generation is rejected.

    Setup:
      1. Worker A claims (generation=1).
      2. Worker B reclaims and completes (generation=2).
      3. Worker A (stale) calls kernel.fail() with generation=1.
      4. Assert: StaleWorkerError; task remains completed.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as s1:
        persona_a = await _register_worker_and_v1_profile(s1, profiles_dir)
        task = await _enqueue(s1, title="fencing fail test")
        task_id = task.id

        kernel_a = _build_kernel(s1, OkAdapter(output={"from": "A"}), persona_a)
        await kernel_a.execute(task)

        # Simulate: stale in_progress with generation=1
        await s1.execute(
            update(TaskModel)
            .where(TaskModel.id == task_id)
            .values(
                status="in_progress",
                result=None,
                completed_at=None,
                lock_expires_at=datetime.now(timezone.utc) - timedelta(minutes=30),
            )
        )
        await s1.commit()

    # Worker B reclaims and completes
    async with session_factory() as s2:
        persona_b = await _register_worker_and_v1_profile(s2, profiles_dir)
        kernel_b = _build_kernel(s2, OkAdapter(output={"from": "B"}), persona_b)
        await kernel_b.reclaim(task)
        await kernel_b.complete(task, Result(status="completed", output={"from": "B"}))
        row = await _task_row(s2, task_id)
        assert row.status == "completed"
        assert row.generation == 2

    # Worker A (stale) calls fail -> fenced
    async with session_factory() as s3:
        kernel_a2 = _build_kernel(s3, FailAdapter(error="stale-error"), persona_a)
        with pytest.raises(StaleWorkerError):
            await kernel_a2.fail(task, Result(status="failed", error="stale-error"))

        row = await _task_row(s3, task_id)
        assert row.status == "completed", "Stale fail must not overwrite completed"


# ---------- AC #5: stale worker cannot refresh heartbeat after ownership transfer ----------


@pytest.mark.skip(reason="v0.2: requires heartbeat() with generation check; not yet implemented")
async def test_stale_worker_cannot_heartbeat_after_ownership_transfer(
    profiles_dir, database_url, session_factory
):
    """After reclaim, stale worker's heartbeat is rejected.

    Proves: the heartbeat write is fenced. Once a task is reclaimed,
    the stale worker receives StaleWorkerError on its next heartbeat
    and must stop.

    Setup:
      1. Worker A claims (generation=1).
      2. Worker B reclaims (generation=2).
      3. Worker A's heartbeat call is rejected.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as s1:
        persona_a = await _register_worker_and_v1_profile(s1, profiles_dir)
        task = await _enqueue(s1, title="heartbeat fencing test")
        task_id = task.id

        kernel_a = _build_kernel(s1, SlowAdapter(delay=60), persona_a)
        # Start execution but don't wait — let claim happen
        import asyncio
        exec_task = asyncio.create_task(kernel_a.execute(task))
        await asyncio.sleep(0.2)  # claim completes, slow adapter runs in background

        # Backdate to stale
        await s1.execute(
            update(TaskModel)
            .where(TaskModel.id == task_id)
            .values(
                lock_expires_at=datetime.now(timezone.utc) - timedelta(minutes=30),
                status="in_progress",
            )
        )
        await s1.commit()

    # Worker B reclaims
    async with session_factory() as s2:
        persona_b = await _register_worker_and_v1_profile(s2, profiles_dir)
        kernel_b = _build_kernel(s2, OkAdapter(), persona_b)
        await kernel_b.reclaim(task)
        row = await _task_row(s2, task_id)
        assert row.generation == 2

    # Worker A (stale) heartbeat -> fenced
    async with session_factory() as s3:
        kernel_a2 = _build_kernel(s3, SlowAdapter(delay=60), persona_a)
        with pytest.raises(StaleWorkerError):
            await kernel_a2.heartbeat(task)

    exec_task.cancel()
    try:
        await exec_task
    except asyncio.CancelledError:
        pass


# ---------- AC #6: stale worker cannot write checkpoint after ownership transfer ----------


@pytest.mark.skip(reason="v0.2: requires checkpoint_write() with generation check; not yet implemented")
async def test_stale_worker_cannot_write_checkpoint_after_ownership_transfer(
    profiles_dir, database_url, session_factory
):
    """After reclaim, stale worker's checkpoint write is rejected.

    Proves: the kernel's checkpoint_ref update is fenced. A stale worker
    cannot overwrite the new owner's checkpoint state.

    Setup:
      1. Worker A claims (generation=1).
      2. Worker B reclaims (generation=2).
      3. Worker A's checkpoint_write is rejected.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as s1:
        persona_a = await _register_worker_and_v1_profile(s1, profiles_dir)
        task = await _enqueue(s1, title="checkpoint fencing test")
        task_id = task.id

        kernel_a = _build_kernel(s1, OkAdapter(), persona_a)
        await kernel_a.execute(task)

        # Simulate stale state
        await s1.execute(
            update(TaskModel)
            .where(TaskModel.id == task_id)
            .values(
                status="in_progress",
                result=None,
                completed_at=None,
                checkpoint_ref=None,
                lock_expires_at=datetime.now(timezone.utc) - timedelta(minutes=30),
            )
        )
        await s1.commit()

    # Worker B reclaims
    async with session_factory() as s2:
        persona_b = await _register_worker_and_v1_profile(s2, profiles_dir)
        kernel_b = _build_kernel(s2, OkAdapter(), persona_b)
        await kernel_b.reclaim(task)
        row = await _task_row(s2, task_id)
        assert row.generation == 2
        assert row.checkpoint_ref is None, "Reclaim must clear checkpoint_ref"

    # Worker A (stale) tries to write a checkpoint -> fenced
    async with session_factory() as s3:
        kernel_a2 = _build_kernel(s3, OkAdapter(), persona_a)
        with pytest.raises(StaleWorkerError):
            await kernel_a2.checkpoint_write(task, "stale-cp-ref", {"step": 99})


# ---------- AC #7: two recovery workers cannot both acquire ownership ----------


@pytest.mark.skip(reason="v0.2: requires atomic reclaim with generation; not yet implemented")
async def test_two_recovery_workers_cannot_both_acquire_ownership(
    profiles_dir, database_url, session_factory
):
    """Concurrent reclaim attempts resolve to exactly one winner.

    Proves: the atomic UPDATE WHERE lock_expires_at < NOW() ensures
    exactly one recovery worker wins; the other gets 0 rows affected.

    This mirrors AC #7 from Step 10a (concurrent claim on queued task)
    but applies to reclaiming a stale in_progress task.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    # Setup: stale in_progress task
    async with session_factory() as s1:
        persona = await _register_worker_and_v1_profile(s1, profiles_dir)
        task = await _enqueue(s1, title="double-reclaim test")
        task_id = task.id

        await s1.execute(
            update(TaskModel)
            .where(TaskModel.id == task_id)
            .values(
                status="in_progress",
                assigned_worker="crashed-worker",
                lock_expires_at=datetime.now(timezone.utc) - timedelta(minutes=30),
                generation=1,
            )
        )
        await s1.commit()

    # Concurrent reclaim attempts
    results: dict[str, str] = {}

    async def reclaim(worker_label: str, s_factory, p: Persona):
        async with s_factory() as s:
            try:
                kernel = _build_kernel(s, OkAdapter(), p)
                await kernel.reclaim(task)  # v0.2 reclaim()
                results[worker_label] = "won"
            except TaskStatusError:
                results[worker_label] = "lost"

    async with session_factory() as s2:
        persona2 = await _register_worker_and_v1_profile(s2, profiles_dir)
        persona3 = await _register_worker_and_v1_profile(s2, profiles_dir)

    await asyncio.gather(
        reclaim("worker-B", session_factory, persona2),
        reclaim("worker-C", session_factory, persona3),
    )

    winners = [k for k, v in results.items() if v == "won"]
    assert len(winners) == 1, (
        f"Exactly one worker must win; got winners={winners}, results={results}"
    )


# ---------- AC #8: checkpoint is cleared on reclaim; recovery starts from scratch ----------


@pytest.mark.skip(reason="v0.2: requires reclaim() to set checkpoint_ref=NULL; not yet implemented")
async def test_reclaimed_task_starts_from_scratch_not_from_existing_checkpoint(
    profiles_dir, database_url, session_factory
):
    """On reclaim, checkpoint_ref is set to NULL; execution resumes from scratch.

    Proves: the kernel does not reuse the pre-crash checkpoint on reclaim.
    Starting fresh is the safest default (avoids stale pre-crash adapter state).

    Setup:
      1. Task has a checkpoint_ref before crash.
      2. Task is reclaimed.
      3. Assert: checkpoint_ref is NULL on the reclaimed task.
      4. Recovery execution is called with checkpoint=None.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    captured_checkpoints: list = []

    class CheckpointCaptureAdapter:
        async def execute(self, context, checkpoint=None) -> Result:
            captured_checkpoints.append(checkpoint)
            return Result(status="completed", output={})

    async with session_factory() as s1:
        persona = await _register_worker_and_v1_profile(s1, profiles_dir)
        task = await _enqueue(s1, title="checkpoint resume test")
        task_id = task.id

        # Create pre-crash checkpoint
        await s1.execute(
            text(
                "INSERT INTO task_checkpoints (ref, task_id, state) "
                "VALUES ('cp-before-crash', :tid, '{\"step\": 3}'::jsonb)"
            ),
            {"tid": task_id},
        )
        await s1.execute(
            update(TaskModel)
            .where(TaskModel.id == task_id)
            .values(checkpoint_ref="cp-before-crash")
        )
        await s1.commit()

        # Simulate crash: stale in_progress
        await s1.execute(
            update(TaskModel)
            .where(TaskModel.id == task_id)
            .values(
                status="in_progress",
                lock_expires_at=datetime.now(timezone.utc) - timedelta(minutes=30),
            )
        )
        await s1.commit()

    # Recovery: checkpoint_ref must be cleared
    async with session_factory() as s2:
        persona2 = await _register_worker_and_v1_profile(s2, profiles_dir)
        kernel = _build_kernel(s2, CheckpointCaptureAdapter(), persona2)
        await kernel.reclaim(task)

        row = await _task_row(s2, task_id)
        assert row.checkpoint_ref is None, (
            "Reclaim must clear checkpoint_ref to avoid stale adapter state"
        )
        assert captured_checkpoints == [None], (
            "Recovery execution must receive checkpoint=None, not the pre-crash checkpoint"
        )


# ---------- AC #9: max_retries limits reclaim (IDEMPOTENT path) ----------


@pytest.mark.skip(reason="v0.2: requires retry_count / max_retries / exhaustion_reason columns; not yet implemented")
async def test_max_retries_with_idempotent_transitions_to_failed(
    profiles_dir, database_url, session_factory
):
    """When retry_count >= max_retries AND classification=IDEMPOTENT, task -> 'failed'.

    Proves: the retry-exhaustion path is classification-aware. An IDEMPOTENT
    task whose retry budget is spent transitions to 'failed' (terminal) with
    exhaustion_reason='retry_limit_exhausted', and the caller may treat the
    failure as final and re-enqueue if needed.

    Setup:
      1. Task has retry_count=3, max_retries=3 (already at limit).
      2. side_effect_classification='IDEMPOTENT'.
      3. Orphan detection finds it; reclaim predicate fails on retry budget.
      4. Retry-exhaustion UPDATE runs: task -> 'failed'.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as s1:
        persona = await _register_worker_and_v1_profile(s1, profiles_dir)
        task = await _enqueue(
            s1,
            title="max retries idempotent test",
            side_effect_classification="IDEMPOTENT",
        )
        task_id = task.id

        # Simulate: 3 prior recoveries — now at the limit, IDEMPOTENT.
        await s1.execute(
            update(TaskModel)
            .where(TaskModel.id == task_id)
            .values(
                status="in_progress",
                lock_expires_at=datetime.now(timezone.utc) - timedelta(minutes=30),
                generation=4,  # original + 3 recoveries
                retry_count=3,
                side_effect_classification="IDEMPOTENT",
            )
        )
        await s1.commit()

    # Recovery loop: reclaim returns 0 rows (retry budget exhausted),
    # retry-exhaustion UPDATE runs.
    async with session_factory() as s2:
        persona2 = await _register_worker_and_v1_profile(s2, profiles_dir)
        kernel = _build_kernel(s2, OkAdapter(), persona2)

        await kernel.reclaim(task)  # 0 rows: retry_count >= max_retries

        row = await _task_row(s2, task_id)
        assert row.status == "failed", (
            "IDEMPOTENT task with exhausted retries must transition to 'failed'"
        )
        assert row.exhaustion_reason == "retry_limit_exhausted", (
            "exhaustion_reason must be set to 'retry_limit_exhausted' on retry-exhaustion path"
        )
        assert row.completed_at is not None, (
            "IDEMPOTENT retry-exhausted task must have completed_at set (it is terminal)"
        )
        assert "max retries exceeded" in (row.last_error or "").lower()


# ---------- AC #10: UNKNOWN classification blocks auto-reclamation ----------


@pytest.mark.skip(reason="v0.2: requires side_effect_classification column; not yet implemented")
async def test_unknown_classification_blocks_auto_reclamation(
    profiles_dir, database_url, session_factory
):
    """A task with side_effect_classification=UNKNOWN is not auto-reclaimed.

    Proves: a task whose side-effect behavior has not been explicitly classified
    is never auto-reclaimed. The unknown state is a safety default.

    Setup:
      1. Task has side_effect_classification=UNKNOWN (default).
      2. It is in stale in_progress state.
      3. Assert: orphan detection finds it (for visibility)
         but the reclaim is blocked because classification is not IDEMPOTENT.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as s1:
        persona = await _register_worker_and_v1_profile(s1, profiles_dir)
        # Default classification is UNKNOWN (not explicitly set)
        task = await _enqueue(s1, title="unknown classification test")
        task_id = task.id

        # Verify: classification is UNKNOWN
        row = await _task_row(s1, task_id)
        assert row.side_effect_classification == "UNKNOWN"

        # Claim + simulate crash
        await s1.execute(
            update(TaskModel)
            .where(TaskModel.id == task_id)
            .values(
                status="in_progress",
                lock_expires_at=datetime.now(timezone.utc) - timedelta(minutes=30),
            )
        )
        await s1.commit()

    # Orphan detection: task IS found (for visibility)
    async with session_factory() as s2:
        stale = await s2.execute(
            select(TaskModel.id).where(
                TaskModel.status == "in_progress",
                TaskModel.lock_expires_at < datetime.now(timezone.utc),
            )
        )
        all_stale_ids = [row[0] for row in stale.fetchall()]
        assert task_id in all_stale_ids, (
            "Orphan detection must still find the task for visibility"
        )

    # Reclaim: blocked by UNKNOWN classification
    async with session_factory() as s3:
        persona2 = await _register_worker_and_v1_profile(s3, profiles_dir)
        kernel = _build_kernel(s3, OkAdapter(), persona2)
        with pytest.raises(TaskStatusError):
            await kernel.reclaim(task)  # UNKNOWN -> rejected

    # At retry-exhaustion, UNKNOWN task routes to 'review' (NOT silently failed).
    # This is the "no silent discard" property: a non-idempotent task is
    # never dropped into 'failed' by the kernel.
    async with session_factory() as s4:
        # Force retry_count >= max_retries to trigger retry-exhaustion path
        await s4.execute(
            update(TaskModel)
            .where(TaskModel.id == task_id)
            .values(retry_count=3, side_effect_classification="UNKNOWN")
        )
        await s4.commit()
        row_before_retry_exhaust = await _task_row(s4, task_id)

        # Simulate the recovery loop: when reclaim returns 0 rows, the
        # retry-exhaustion UPDATE runs. UNKNOWN -> 'review'.
        # The WHERE includes `generation = :observed_generation` as the
        # recovery-loop fence: a stale decision cannot terminate a task
        # whose ownership has changed since the read.
        await s4.execute(
            text(
                "UPDATE tasks SET "
                "  status = CASE "
                "    WHEN side_effect_classification = 'IDEMPOTENT' THEN 'failed' "
                "    WHEN side_effect_classification IN ('UNKNOWN', 'HAS_SIDE_EFFECTS') THEN 'review' "
                "  END, "
                "  exhaustion_reason = 'retry_limit_exhausted', "
                "  lock_expires_at = NULL, "
                "  updated_at = NOW() "
                "WHERE id = :tid AND status = 'in_progress' "
                "  AND generation = :gen "
                "  AND lock_expires_at < NOW() "
                "  AND retry_count >= max_retries "
                "  AND side_effect_classification IN ('IDEMPOTENT', 'UNKNOWN', 'HAS_SIDE_EFFECTS')"
            ),
            {"tid": task_id, "gen": row_before_retry_exhaust.generation},
        )
        await s4.commit()

        row = await _task_row(s4, task_id)
        assert row.status == "review", (
            "UNKNOWN task with exhausted retries MUST route to 'review' (operator handoff), "
            "NOT 'failed' (silent discard). The operator must classify and reset."
        )
        assert row.exhaustion_reason == "retry_limit_exhausted", (
            "exhaustion_reason must be set on retry-exhaustion path"
        )


# ---------- AC #11: IDEMPOTENT classification permits auto-reclamation ----------


@pytest.mark.skip(reason="v0.2: requires side_effect_classification column; not yet implemented")
async def test_idempotent_classification_permits_auto_reclamation(
    profiles_dir, database_url, session_factory
):
    """A task with side_effect_classification=IDEMPOTENT is auto-reclaimed.

    Proves: a task that has been explicitly classified as idempotent
    (no non-idempotent external side effects) is eligible for auto-reclaim.

    Setup:
      1. Task has side_effect_classification=IDEMPOTENT.
      2. It is in stale in_progress state.
      3. Assert: reclaim succeeds because the task is classified as IDEMPOTENT.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as s1:
        persona = await _register_worker_and_v1_profile(s1, profiles_dir)
        task = await _enqueue(
            s1,
            title="idempotent classification test",
            side_effect_classification="IDEMPOTENT",
        )
        task_id = task.id

        # Verify: classification is IDEMPOTENT
        row = await _task_row(s1, task_id)
        assert row.side_effect_classification == "IDEMPOTENT"

        # Claim + simulate crash
        await s1.execute(
            update(TaskModel)
            .where(TaskModel.id == task_id)
            .values(
                status="in_progress",
                lock_expires_at=datetime.now(timezone.utc) - timedelta(minutes=30),
            )
        )
        await s1.commit()

    # Reclaim: succeeds for IDEMPOTENT task
    async with session_factory() as s2:
        persona2 = await _register_worker_and_v1_profile(s2, profiles_dir)
        kernel = _build_kernel(s2, OkAdapter(output={"reclaimed": True}), persona2)
        await kernel.reclaim(task)

        row = await _task_row(s2, task_id)
        assert row.status == "in_progress", (
            "IDEMPOTENT task must be successfully reclaimed"
        )
        assert row.generation == 2, "Generation must be incremented on reclaim"
        assert row.retry_count == 1, "retry_count must be incremented on reclaim"


# ---------- AC #11b: HAS_SIDE_EFFECTS classification blocks auto-reclamation ----------


@pytest.mark.skip(reason="v0.2: requires side_effect_classification column; not yet implemented")
async def test_has_side_effects_classification_blocks_auto_reclamation(
    profiles_dir, database_url, session_factory
):
    """A task with side_effect_classification=HAS_SIDE_EFFECTS is not auto-reclaimed.

    Proves: a task explicitly declared to have non-idempotent external side effects
    is never auto-reclaimed. Operator must manually assess and reset.

    Setup:
      1. Task has side_effect_classification=HAS_SIDE_EFFECTS.
      2. It is in stale in_progress state.
      3. Assert: orphan detection finds it but reclaim is blocked.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as s1:
        persona = await _register_worker_and_v1_profile(s1, profiles_dir)
        task = await _enqueue(
            s1,
            title="side-effect task",
            side_effect_classification="HAS_SIDE_EFFECTS",
        )
        task_id = task.id

        row = await _task_row(s1, task_id)
        assert row.side_effect_classification == "HAS_SIDE_EFFECTS"

        await s1.execute(
            update(TaskModel)
            .where(TaskModel.id == task_id)
            .values(
                status="in_progress",
                lock_expires_at=datetime.now(timezone.utc) - timedelta(minutes=30),
            )
        )
        await s1.commit()

    # Orphan detection: task IS found (for visibility)
    async with session_factory() as s2:
        stale = await s2.execute(
            select(TaskModel.id).where(
                TaskModel.status == "in_progress",
                TaskModel.lock_expires_at < datetime.now(timezone.utc),
            )
        )
        all_stale_ids = [row[0] for row in stale.fetchall()]
        assert task_id in all_stale_ids

    # Reclaim: blocked by HAS_SIDE_EFFECTS
    async with session_factory() as s3:
        persona2 = await _register_worker_and_v1_profile(s3, profiles_dir)
        kernel = _build_kernel(s3, OkAdapter(), persona2)
        with pytest.raises(TaskStatusError):
            await kernel.reclaim(task)

    # At retry-exhaustion, HAS_SIDE_EFFECTS task routes to 'review' (NOT silently failed).
    # The operator must assess the partial external state and decide whether to
    # retry, compensate, or give up.
    # The WHERE includes `generation = :observed_generation` as the recovery-loop fence.
    async with session_factory() as s4:
        await s4.execute(
            update(TaskModel)
            .where(TaskModel.id == task_id)
            .values(retry_count=3)
        )
        await s4.commit()
        row_before_retry_exhaust = await _task_row(s4, task_id)

        await s4.execute(
            text(
                "UPDATE tasks SET "
                "  status = CASE "
                "    WHEN side_effect_classification = 'IDEMPOTENT' THEN 'failed' "
                "    WHEN side_effect_classification IN ('UNKNOWN', 'HAS_SIDE_EFFECTS') THEN 'review' "
                "  END, "
                "  exhaustion_reason = 'retry_limit_exhausted', "
                "  lock_expires_at = NULL, "
                "  updated_at = NOW() "
                "WHERE id = :tid AND status = 'in_progress' "
                "  AND generation = :gen "
                "  AND lock_expires_at < NOW() "
                "  AND retry_count >= max_retries "
                "  AND side_effect_classification IN ('IDEMPOTENT', 'UNKNOWN', 'HAS_SIDE_EFFECTS')"
            ),
            {"tid": task_id, "gen": row_before_retry_exhaust.generation},
        )
        await s4.commit()

        row = await _task_row(s4, task_id)
        assert row.status == "review", (
            "HAS_SIDE_EFFECTS task with exhausted retries MUST route to 'review' "
            "(operator handoff), NOT 'failed' (silent discard)."
        )
        assert row.exhaustion_reason == "retry_limit_exhausted"


# ---------- AC #12: operator-managed recovery remains possible ----------


@pytest.mark.skip(reason="v0.1 compatible: operator can already reset in_progress tasks manually; v0.2 adds fencing to the reset flow")
async def test_operator_can_manually_reset_in_progress_to_queued(
    profiles_dir, database_url, session_factory
):
    """An operator can reset a stale in_progress task back to queued.

    Proves: manual reset by an operator remains a supported path.
    This already works in v0.1 (ADR 0015). v0.2 adds fencing so
    concurrent recovery attempts are blocked during reset.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as s1:
        persona = await _register_worker_and_v1_profile(s1, profiles_dir)
        task = await _enqueue(s1, title="operator reset test")
        task_id = task.id

        await s1.execute(
            update(TaskModel)
            .where(TaskModel.id == task_id)
            .values(
                status="in_progress",
                assigned_worker="dead-worker",
                lock_expires_at=datetime.now(timezone.utc) - timedelta(minutes=30),
                generation=1,
            )
        )
        await s1.commit()

    # Operator reset
    async with session_factory() as s2:
        await s2.execute(
            update(TaskModel)
            .where(TaskModel.id == task_id)
            .values(
                status="queued",
                assigned_worker=None,
                lock_expires_at=None,
                generation=2,  # operator reset increments generation
                started_at=None,
                result=None,
            )
        )
        await s2.commit()

    # Task is re-queueable
    async with session_factory() as s3:
        await _register_worker_and_v1_profile(s3, profiles_dir)
        wl = WorkloadService(s3)
        found = await wl.next_runnable()
        assert found is not None
        assert found.id == task_id


# ---------- AC #13: operator reset increments generation; stale writes remain fenced ----------


@pytest.mark.skip(reason="v0.2: requires generation increment on operator reset; not yet implemented")
async def test_operator_reset_increments_generation_fencing_stale_writes(
    profiles_dir, database_url, session_factory
):
    """After operator reset, stale worker's generation is outdated; writes remain fenced.

    Proves: operator reset increments generation, so any stale worker
    that somehow still holds the old generation cannot write to the
    newly queued task.

    Setup:
      1. Worker A has task (generation=1, in_progress, stale).
      2. Operator resets to queued (generation -> 2).
      3. Worker A (holding generation=1) attempts any write -> fenced.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as s1:
        persona_a = await _register_worker_and_v1_profile(s1, profiles_dir)
        task = await _enqueue(s1, title="operator reset gen test")
        task_id = task.id

        await s1.execute(
            update(TaskModel)
            .where(TaskModel.id == task_id)
            .values(
                status="in_progress",
                assigned_worker="dead-worker",
                lock_expires_at=datetime.now(timezone.utc) - timedelta(minutes=30),
                generation=1,
            )
        )
        await s1.commit()

    # Operator reset (generation -> 2)
    async with session_factory() as s2:
        await s2.execute(
            update(TaskModel)
            .where(TaskModel.id == task_id)
            .values(
                status="queued",
                assigned_worker=None,
                lock_expires_at=None,
                generation=2,
            )
        )
        await s2.commit()

    # Task re-queued
    async with session_factory() as s3:
        persona_b = await _register_worker_and_v1_profile(s3, profiles_dir)
        wl = WorkloadService(s3)
        found = await wl.next_runnable()
        assert found is not None
        assert found.id == task_id

        row = await _task_row(s3, task_id)
        assert row.generation == 2

    # Stale worker A (gen=1) attempts heartbeat -> fenced
    async with session_factory() as s4:
        kernel_a = _build_kernel(s4, OkAdapter(), persona_a)
        with pytest.raises(StaleWorkerError):
            await kernel_a.heartbeat(task)


# ---------- Additional: heartbeat failure behavior ----------


@pytest.mark.skip(reason="v0.2: requires heartbeat() with fenced UPDATE; not yet implemented")
async def test_worker_receives_stale_worker_error_on_heartbeat_after_reclaim(
    profiles_dir, database_url, session_factory
):
    """When a heartbeat is fenced, StaleWorkerError is raised.

    Proves: the worker knows it has lost ownership immediately on the
    next heartbeat. The recommended behavior is to stop execution.

    This is a variant of AC #5 with a different error trigger.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as s1:
        persona = await _register_worker_and_v1_profile(s1, profiles_dir)
        task = await _enqueue(s1, title="heartbeat stale error test")
        task_id = task.id

        kernel = _build_kernel(s1, SlowAdapter(delay=60), persona)
        exec_task = asyncio.create_task(kernel.execute(task))
        await asyncio.sleep(0.2)

        # Make stale
        await s1.execute(
            update(TaskModel)
            .where(TaskModel.id == task_id)
            .values(
                lock_expires_at=datetime.now(timezone.utc) - timedelta(minutes=30),
                status="in_progress",
            )
        )
        await s1.commit()

    # Worker B reclaims
    async with session_factory() as s2:
        persona_b = await _register_worker_and_v1_profile(s2, profiles_dir)
        kernel_b = _build_kernel(s2, OkAdapter(), persona_b)
        await kernel_b.reclaim(task)

    # Worker A heartbeat -> fenced
    async with session_factory() as s3:
        kernel_a = _build_kernel(s3, SlowAdapter(delay=60), persona)
        with pytest.raises(StaleWorkerError):
            await kernel_a.heartbeat(task)

    exec_task.cancel()
    try:
        await exec_task
    except asyncio.CancelledError:
        pass
