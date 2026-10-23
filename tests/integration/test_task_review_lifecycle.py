"""Step 15 Acceptance Tests — Task Review Resolution & Operator Re-Entry Lifecycle.

Validates the 14 acceptance scenarios defined in ADR 0018 and the specification:
  1. Worker Affinity Preserved
  2. Explicit Reassignment
  3. Reassignment 404
  4. Manual Resolution Non-Replay
  5. Forensic Evidence Preservation
  6. Concurrent Race Conflict
  7. Generation Mismatch
  8. Retry Budget Reset
  9. Exhausted Retry Rejection
 10. Guarded Classification Update
 11. Missing Attestation Rejection
 12. Status 409 Conflict
 13. Authentication 401
 14. Review Context Inspection
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from personaos.api.app import create_app
from personaos.config import settings
from personaos.db.models import Task as TaskModel
from personaos.db.models import TaskCheckpoint, TaskEvent
from personaos.db.models import Worker as WorkerModel
from personaos.workload.service import WorkloadService

REPO_ROOT = Path(__file__).parents[2]
ALEMBIC_INI = REPO_ROOT / "src" / "personaos" / "db" / "migrations" / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "src" / "personaos" / "db" / "migrations"

pytestmark = pytest.mark.usefixtures("clean_tables")


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


@pytest.fixture
async def migrated_db(database_url: str) -> None:
    """Run Alembic migrations to head before tests."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")


@pytest.fixture
async def api_client(
    migrated_db: None,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    """Async HTTP test client bound to test database and temporary profiles directory."""
    monkeypatch.setenv("PERSONAOS_PROFILES_DIR", str(tmp_path / "profiles"))
    app = create_app(engine=engine, session_factory=session_factory)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


# ---------- Scenario 1: Worker Affinity Preserved ---------- #


@pytest.mark.asyncio
async def test_scenario_01_worker_affinity_preserved(
    api_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Approving retains assigned_worker and immutable profile_version."""
    async with session_factory() as session:
        session.add(
            WorkerModel(id="worker-a", name="Worker A", role="devops", current_profile_version=2)
        )
        await session.flush()
        session.add(
            TaskModel(
                id="task-sc-01",
                title="Affinity Task",
                objective="Preserve worker affinity",
                input={},
                assigned_worker="worker-a",
                status="review",
                profile_version=2,
                generation=0,
                side_effect_classification="HAS_SIDE_EFFECTS",
            )
        )
        await session.commit()

    resp = await api_client.post(
        "/tasks/task-sc-01/approve",
        json={
            "reason": "Approving task with preserved affinity",
            "expected_generation": 0,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "queued"
    assert data["assigned_worker"] == "worker-a"
    assert data["profile_version"] == 2
    assert data["generation"] == 1
    assert data["lock_expires_at"] is None

    async with session_factory() as session:
        evt = (
            await session.execute(
                select(TaskEvent)
                .where(TaskEvent.task_id == "task-sc-01", TaskEvent.type == "task.requeued")
            )
        ).scalar_one_or_none()
        assert evt is not None
        assert evt.payload["action"] == "approved_for_requeue"
        assert evt.payload["assigned_worker"] == "worker-a"


# ---------- Scenario 2: Explicit Reassignment ---------- #


@pytest.mark.asyncio
async def test_scenario_02_explicit_reassignment(
    api_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Reassigning to a valid worker updates assignment and consistently snapshots profile."""
    async with session_factory() as session:
        session.add(
            WorkerModel(id="worker-orig", name="Original", role="devops", current_profile_version=1)
        )
        session.add(
            WorkerModel(
                id="worker-new",
                name="Replacement",
                role="devops",
                current_profile_version=3,
            )
        )
        await session.flush()
        session.add(
            TaskModel(
                id="task-sc-02",
                title="Reassign Task",
                objective="Reassign safely",
                input={},
                assigned_worker="worker-orig",
                status="review",
                profile_version=1,
                generation=1,
            )
        )
        await session.commit()

    resp = await api_client.post(
        "/tasks/task-sc-02/approve",
        json={
            "reason": "Reassigning to worker-new due to specialized skill",
            "expected_generation": 1,
            "reassign_worker_id": "worker-new",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "queued"
    assert data["assigned_worker"] == "worker-new"
    assert data["profile_version"] == 3
    assert data["generation"] == 2


# ---------- Scenario 3: Reassignment 404 ---------- #


@pytest.mark.asyncio
async def test_scenario_03_reassignment_404(
    api_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Nonexistent reassign_worker_id returns HTTP 404 without changing task."""
    async with session_factory() as session:
        session.add(
            WorkerModel(id="worker-valid", name="Valid", role="devops", current_profile_version=1)
        )
        await session.flush()
        session.add(
            TaskModel(
                id="task-sc-03",
                title="Missing Target Task",
                objective="Ensure 404 on missing worker",
                input={},
                assigned_worker="worker-valid",
                status="review",
                profile_version=1,
                generation=0,
            )
        )
        await session.commit()

    resp = await api_client.post(
        "/tasks/task-sc-03/approve",
        json={
            "reason": "Attempting reassignment to missing worker",
            "expected_generation": 0,
            "reassign_worker_id": "nonexistent-worker",
        },
    )
    assert resp.status_code == 404

    # Verify task remains in review
    async with session_factory() as session:
        row = (
            await session.execute(select(TaskModel).where(TaskModel.id == "task-sc-03"))
        ).scalar_one()
        assert row.status == "review"
        assert row.generation == 0


# ---------- Scenario 4: Manual Resolution Non-Replay ---------- #


@pytest.mark.asyncio
async def test_scenario_04_manual_resolution_non_replay(
    api_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Resolution marks task completed with verified output and prevents re-execution."""
    async with session_factory() as session:
        session.add(
            WorkerModel(id="worker-w4", name="Worker 4", role="devops")
        )
        await session.flush()
        session.add(
            TaskModel(
                id="task-sc-04",
                title="VPC Provisioning",
                objective="Create VPC in staging",
                input={"cidr": "10.0.0.0/16"},
                assigned_worker="worker-w4",
                status="review",
                generation=1,
                side_effect_classification="HAS_SIDE_EFFECTS",
                exhaustion_reason="ambiguous_in_flight_failure",
            )
        )
        await session.commit()

    resp = await api_client.post(
        "/tasks/task-sc-04/resolve",
        json={
            "reason": "Verified VPC vpc-998877 is active in AWS console",
            "expected_generation": 1,
            "result": {"vpc_id": "vpc-998877", "cidr": "10.0.0.0/16"},
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    assert data["generation"] == 2
    assert data["completed_at"] is not None

    # Verify WorkloadService.next_runnable() does NOT pick up completed task
    async with session_factory() as session:
        wl = WorkloadService(session)
        runnable = await wl.next_runnable()
        assert runnable is None

        # Verify event emitted is task.completed with operator_resolved action
        evt = (
            await session.execute(
                select(TaskEvent)
                .where(TaskEvent.task_id == "task-sc-04", TaskEvent.type == "task.completed")
            )
        ).scalar_one()
        assert evt.payload["action"] == "operator_resolved"
        assert evt.payload["verified_external"] is True


# ---------- Scenario 5: Forensic Evidence Preservation ---------- #


@pytest.mark.asyncio
async def test_scenario_05_forensic_evidence_preservation(
    api_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Resolution nests prior diagnostics under result['prior_execution'] without discarding."""
    prior_output = {
        "ambiguous": True,
        "phase": "in_flight_prompt_accepted",
        "session_id": "session-abc-123",
        "accumulated_text": "Initiating VPC deployment...",
    }

    async with session_factory() as session:
        session.add(WorkerModel(id="worker-w5", name="Worker 5", role="devops"))
        await session.flush()
        session.add(
            TaskModel(
                id="task-sc-05",
                title="Forensics Task",
                objective="Preserve adapter forensics",
                input={},
                assigned_worker="worker-w5",
                status="review",
                generation=2,
                result=prior_output,
                last_error="Ambiguous outcome: timeout",
                exhaustion_reason="ambiguous_in_flight_failure",
            )
        )
        await session.flush()
        session.add(
            TaskCheckpoint(
                ref="cp-ref-001",
                task_id="task-sc-05",
                state={"step": "provisioning_vpc"},
            )
        )
        await session.flush()
        await session.execute(
            update(TaskModel)
            .where(TaskModel.id == "task-sc-05")
            .values(checkpoint_ref="cp-ref-001")
        )
        await session.commit()

    resp = await api_client.post(
        "/tasks/task-sc-05/resolve",
        json={
            "reason": "Manual operator resolution after verifying VPC state",
            "expected_generation": 2,
            "result": {"vpc_id": "vpc-verified-555"},
        },
    )
    assert resp.status_code == 200

    async with session_factory() as session:
        row = (
            await session.execute(select(TaskModel).where(TaskModel.id == "task-sc-05"))
        ).scalar_one()
        assert row.status == "completed"
        assert row.result is not None
        # Verified output is accessible
        assert row.result["verified_output"] == {"vpc_id": "vpc-verified-555"}
        # Prior forensic diagnostics are completely preserved
        prior = row.result["prior_execution"]
        assert prior["last_error"] == "Ambiguous outcome: timeout"
        assert prior["exhaustion_reason"] == "ambiguous_in_flight_failure"
        assert prior["prior_result"]["session_id"] == "session-abc-123"
        assert prior["prior_checkpoint_ref"] == "cp-ref-001"
        assert prior["prior_generation"] == 2


# ---------- Scenario 6: Concurrent Race Conflict ---------- #


@pytest.mark.asyncio
async def test_scenario_06_concurrent_race_conflict(
    api_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two concurrent resolutions produce one HTTP 200 winner and one HTTP 409 conflict."""
    async with session_factory() as session:
        session.add(WorkerModel(id="worker-w6", name="Worker 6", role="devops"))
        await session.flush()
        session.add(
            TaskModel(
                id="task-sc-06",
                title="Concurrent Race Task",
                objective="Verify serialization",
                input={},
                assigned_worker="worker-w6",
                status="review",
                generation=4,
            )
        )
        await session.commit()

    resp_approve, resp_resolve = await asyncio.gather(
        api_client.post(
            "/tasks/task-sc-06/approve",
            json={"reason": "Approving concurrent task", "expected_generation": 4},
        ),
        api_client.post(
            "/tasks/task-sc-06/resolve",
            json={
                "reason": "Resolving concurrent task manually",
                "expected_generation": 4,
                "result": {"status": "ok"},
            },
        ),
    )

    status_codes = sorted([resp_approve.status_code, resp_resolve.status_code])
    assert status_codes == [200, 409]

    # Winning row has generation incremented to 5
    async with session_factory() as session:
        row = (
            await session.execute(select(TaskModel).where(TaskModel.id == "task-sc-06"))
        ).scalar_one()
        assert row.generation == 5


# ---------- Scenario 7: Generation Mismatch ---------- #


@pytest.mark.asyncio
async def test_scenario_07_generation_mismatch(
    api_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Stale expected_generation returns HTTP 409 without mutation."""
    async with session_factory() as session:
        session.add(WorkerModel(id="worker-w7", name="Worker 7", role="devops"))
        await session.flush()
        session.add(
            TaskModel(
                id="task-sc-07",
                title="Stale Gen Task",
                objective="Test optimistic locking failure",
                input={},
                assigned_worker="worker-w7",
                status="review",
                generation=3,
            )
        )
        await session.commit()

    resp = await api_client.post(
        "/tasks/task-sc-07/approve",
        json={"reason": "Attempting approval with stale generation", "expected_generation": 2},
    )
    assert resp.status_code == 409
    assert "generation mismatch" in resp.json()["detail"].lower()

    async with session_factory() as session:
        row = (
            await session.execute(select(TaskModel).where(TaskModel.id == "task-sc-07"))
        ).scalar_one()
        assert row.status == "review"
        assert row.generation == 3


# ---------- Scenario 8: Retry Budget Reset ---------- #


@pytest.mark.asyncio
async def test_scenario_08_retry_budget_reset(
    api_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """reset_retries=True resets retry_count to zero."""
    async with session_factory() as session:
        session.add(WorkerModel(id="worker-w8", name="Worker 8", role="devops"))
        await session.flush()
        session.add(
            TaskModel(
                id="task-sc-08",
                title="Retry Budget Task",
                objective="Reset retries",
                input={},
                assigned_worker="worker-w8",
                status="review",
                generation=0,
                retry_count=3,
                max_retries=3,
            )
        )
        await session.commit()

    resp = await api_client.post(
        "/tasks/task-sc-08/approve",
        json={
            "reason": "Approving and resetting retries for new budget",
            "expected_generation": 0,
            "reset_retries": True,
        },
    )
    assert resp.status_code == 200

    async with session_factory() as session:
        row = (
            await session.execute(select(TaskModel).where(TaskModel.id == "task-sc-08"))
        ).scalar_one()
        assert row.status == "queued"
        assert row.retry_count == 0


# ---------- Scenario 9: Exhausted Retry Rejection ---------- #


@pytest.mark.asyncio
async def test_scenario_09_exhausted_retry_rejection(
    api_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """reset_retries=False on an exhausted task returns HTTP 422."""
    async with session_factory() as session:
        session.add(WorkerModel(id="worker-w9", name="Worker 9", role="devops"))
        await session.flush()
        session.add(
            TaskModel(
                id="task-sc-09",
                title="Exhausted Retry Task",
                objective="Test rejection when retry budget exhausted",
                input={},
                assigned_worker="worker-w9",
                status="review",
                generation=0,
                retry_count=3,
                max_retries=3,
            )
        )
        await session.commit()

    resp = await api_client.post(
        "/tasks/task-sc-09/approve",
        json={
            "reason": "Attempting approval without resetting exhausted retries",
            "expected_generation": 0,
            "reset_retries": False,
        },
    )
    assert resp.status_code == 422
    assert "retry limit is already exhausted" in resp.json()["detail"].lower()


# ---------- Scenario 10: Guarded Classification Update ---------- #


@pytest.mark.asyncio
async def test_scenario_10_guarded_classification_update(
    api_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Updating to IDEMPOTENT succeeds only with valid attestation."""
    async with session_factory() as session:
        session.add(WorkerModel(id="worker-w10", name="Worker 10", role="devops"))
        await session.flush()
        session.add(
            TaskModel(
                id="task-sc-10",
                title="Attestation Task",
                objective="Safely downgrade classification",
                input={},
                assigned_worker="worker-w10",
                status="review",
                generation=0,
                side_effect_classification="UNKNOWN",
            )
        )
        await session.commit()

    resp = await api_client.post(
        "/tasks/task-sc-10/approve",
        json={
            "reason": "Approving task with certified idempotency",
            "expected_generation": 0,
            "attestation": {
                "requested_classification": "IDEMPOTENT",
                "safety_justification": (
                    "Verified external transaction ID ensures idempotent processing"
                ),
                "verification_reference": "TX-998877-CONFIRMED",
            },
        },
    )
    assert resp.status_code == 200

    async with session_factory() as session:
        row = (
            await session.execute(select(TaskModel).where(TaskModel.id == "task-sc-10"))
        ).scalar_one()
        assert row.side_effect_classification == "IDEMPOTENT"

        evt = (
            await session.execute(
                select(TaskEvent)
                .where(TaskEvent.task_id == "task-sc-10", TaskEvent.type == "task.requeued")
            )
        ).scalar_one()
        assert evt.payload["attestation"]["verification_reference"] == "TX-998877-CONFIRMED"


# ---------- Scenario 11: Missing Attestation Rejection ---------- #


@pytest.mark.asyncio
async def test_scenario_11_missing_attestation_rejection(
    api_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Missing or invalid attestation returns HTTP 422."""
    async with session_factory() as session:
        session.add(WorkerModel(id="worker-w11", name="Worker 11", role="devops"))
        await session.flush()
        session.add(
            TaskModel(
                id="task-sc-11",
                title="Bad Attestation Task",
                objective="Reject invalid attestation",
                input={},
                assigned_worker="worker-w11",
                status="review",
                generation=0,
                side_effect_classification="HAS_SIDE_EFFECTS",
            )
        )
        await session.commit()

    # Safety justification too short (< 15 chars)
    resp = await api_client.post(
        "/tasks/task-sc-11/approve",
        json={
            "reason": "Approving task with invalid attestation",
            "expected_generation": 0,
            "attestation": {
                "requested_classification": "IDEMPOTENT",
                "safety_justification": "too short",
            },
        },
    )
    assert resp.status_code == 422


# ---------- Scenario 12: Status 409 Conflict ---------- #


@pytest.mark.asyncio
async def test_scenario_12_status_409_conflict(
    api_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Resolving a task outside review returns HTTP 409."""
    async with session_factory() as session:
        session.add(WorkerModel(id="worker-w12", name="Worker 12", role="devops"))
        await session.flush()
        session.add(
            TaskModel(
                id="task-sc-12",
                title="Queued Task",
                objective="Reject resolution when queued",
                input={},
                assigned_worker="worker-w12",
                status="queued",
                generation=0,
            )
        )
        await session.commit()

    resp_approve = await api_client.post(
        "/tasks/task-sc-12/approve",
        json={"reason": "Attempting to approve a queued task", "expected_generation": 0},
    )
    assert resp_approve.status_code == 409

    resp_resolve = await api_client.post(
        "/tasks/task-sc-12/resolve",
        json={
            "reason": "Attempting to resolve a queued task",
            "expected_generation": 0,
            "result": {"ok": True},
        },
    )
    assert resp_resolve.status_code == 409

    resp_reject = await api_client.post(
        "/tasks/task-sc-12/reject",
        json={"reason": "Attempting to reject a queued task", "expected_generation": 0},
    )
    assert resp_reject.status_code == 409


# ---------- Scenario 13: Authentication 401 ---------- #


@pytest.mark.asyncio
async def test_scenario_13_authentication_401(
    api_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing or invalid credentials return HTTP 401 when operator token is configured."""
    monkeypatch.setattr(settings, "operator_token", "secret-token-xyz")

    async with session_factory() as session:
        session.add(WorkerModel(id="worker-w13", name="Worker 13", role="devops"))
        await session.flush()
        session.add(
            TaskModel(
                id="task-sc-13",
                title="Auth Test Task",
                objective="Verify bearer token check",
                input={},
                assigned_worker="worker-w13",
                status="review",
                generation=0,
            )
        )
        await session.commit()

    # 1. Missing Authorization header
    resp_no_auth = await api_client.post(
        "/tasks/task-sc-13/approve",
        json={"reason": "Valid reason without auth", "expected_generation": 0},
    )
    assert resp_no_auth.status_code == 401
    assert resp_no_auth.headers.get("www-authenticate") == "Bearer"

    # 2. Invalid token
    resp_bad_auth = await api_client.post(
        "/tasks/task-sc-13/approve",
        headers={"Authorization": "Bearer wrong-token"},
        json={"reason": "Valid reason with wrong token", "expected_generation": 0},
    )
    assert resp_bad_auth.status_code == 401
    assert resp_bad_auth.headers.get("www-authenticate") == "Bearer"

    # 3. Valid token succeeds
    resp_good_auth = await api_client.post(
        "/tasks/task-sc-13/approve",
        headers={"Authorization": "Bearer secret-token-xyz"},
        json={"reason": "Valid reason with correct token", "expected_generation": 0},
    )
    assert resp_good_auth.status_code == 200
    assert resp_good_auth.json()["status"] == "queued"


# ---------- Scenario 14: Review Context Inspection ---------- #


@pytest.mark.asyncio
async def test_scenario_14_review_context_inspection(
    api_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Review-context retrieval returns the complete required diagnostic evidence."""
    async with session_factory() as session:
        session.add(
            WorkerModel(
                id="worker-w14",
                name="Security Analyst",
                role="security",
                runtime="openclaw",
                current_profile_version=2,
            )
        )
        await session.flush()
        session.add(
            TaskModel(
                id="task-sc-14",
                title="Review Context Task",
                objective="Inspect complete review context",
                input={"target": "audit"},
                assigned_worker="worker-w14",
                status="review",
                priority="high",
                profile_version=1,
                generation=2,
                side_effect_classification="HAS_SIDE_EFFECTS",
                exhaustion_reason="ambiguous_in_flight_failure",
                last_error="Process crashed during execution",
                result={"ambiguous": True, "session_id": "sess-99", "accumulated_text": "log 1..."},
            )
        )
        await session.flush()
        # Add a checkpoint
        session.add(
            TaskCheckpoint(
                ref="cp-ref-14",
                task_id="task-sc-14",
                state={"step": "phase_2_complete"},
            )
        )
        # Add a task event
        session.add(
            TaskEvent(
                task_id="task-sc-14",
                type="task.failed",
                actor_id="kernel",
                payload={"action": "routed_to_review"},
            )
        )
        await session.commit()

    resp = await api_client.get("/tasks/task-sc-14/review-context")
    assert resp.status_code == 200
    data = resp.json()

    assert data["task"]["id"] == "task-sc-14"
    assert data["assigned_worker_id"] == "worker-w14"
    assert data["worker_role"] == "security"
    assert data["worker_runtime"] == "openclaw"
    assert data["profile_version_snapshot"] == 1
    assert data["worker_current_profile_version"] == 2
    assert data["is_snapshot_outdated"] is True
    # Forensic evidence
    evidence = data["execution_evidence"]
    assert evidence["last_error"] == "Process crashed during execution"
    assert evidence["exhaustion_reason"] == "ambiguous_in_flight_failure"
    assert evidence["diagnostics"]["session_id"] == "sess-99"
    assert evidence["side_effect_classification"] == "HAS_SIDE_EFFECTS"
    # Checkpoints and events
    assert data["checkpoints_count"] == 1
    assert data["latest_checkpoint"]["ref"] == "cp-ref-14"
    assert len(data["recent_events"]) >= 1
    assert data["recent_events"][0]["type"] == "task.failed"
