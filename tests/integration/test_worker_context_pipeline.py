"""Integration tests for Step 19: Worker Execution Context Assembly and Persona Conditioning.

Validates ADR 0022 and specification:
- CTX-01: Context assembler enriches worker, profile, and persona.
- CTX-02: Upstream dependency results loaded from PostgreSQL into context.
- CTX-03: Dynamic input expression resolution ($dep:task_id.path) with native typing.
- CTX-04: Fail-safe behavior on missing dependency outputs.
- CTX-05: Worker recent experience retrieval from prior completed tasks.
- CTX-06: ACP adapter prompt formats [WORKER IDENTITY & PERSONA].
- CTX-07: ACP adapter prompt formats [UPSTREAM DEPENDENCY ARTIFACTS].
- CTX-08: Replacement compute nodes reconstruct full context, artifacts, and experience.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from personaos.domain.errors import DependencyArtifactResolutionError
from personaos.domain.worker import Persona, WorkerProfile
from personaos.execution.adapters.base import Result
from personaos.execution.context import WorkerExecutionContext
from personaos.execution.kernel import WorkerExecutionKernel
from personaos.registry.profile import ProfileRegistry
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
def profiles_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "profiles"
    monkeypatch.setenv("PERSONAOS_PROFILES_DIR", str(d))
    return d


@dataclass
class CapturingAdapter:
    result_to_return: Result
    captured_context: WorkerExecutionContext | None = None
    captured_checkpoint: Any | None = None

    async def execute(
        self,
        context: WorkerExecutionContext,
        checkpoint: Any | None = None,
    ) -> Result:
        self.captured_context = context
        self.captured_checkpoint = checkpoint
        return self.result_to_return


@pytest.mark.asyncio
async def test_end_to_end_dependency_artifact_piping_and_persona(
    database_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    profiles_dir: Path,
) -> None:
    """CTX-01, CTX-02, CTX-03, CTX-05: Verifies end-to-end task chaining with artifact piping."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    worker_id = "worker-pipeline-01"

    # 1. Register worker with distinct Persona
    async with session_factory() as session:
        reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
        profile = WorkerProfile(
            version=1,
            role="devops",
            runtime="openclaw",
            skills=["helm", "gitops"],
            persona=Persona(
                autonomy="high",
                style="meticulous",
                communication="concise",
                goals=["zero-downtime deployment"],
                constraints=["never skip health checks"],
            ),
            instructions="Strictly verify service health after deployment.",
        )
        await reg.register_worker(worker_id, profile)

    # 2. Stage 1 Task: Produce build output
    t1_id: str = ""
    async with session_factory() as session:
        wl = WorkloadService(session=session)
        task1 = await wl.enqueue(
            worker_id=worker_id,
            title="Build and push image",
            objective="Compile application container",
            input={"repo": "git@github.com:org/app.git"},
        )
        t1_id = task1.id

        kernel1 = WorkerExecutionKernel(
            session=session,
            adapter=CapturingAdapter(
                result_to_return=Result(
                    status="completed",
                    output={
                        "image_tag": "v1.4.2",
                        "digest": "sha256:4a5b6c7d8e9f",
                        "port": 8080,
                        "metadata": {"cluster": "prod-us-east-1", "canary": True},
                    },
                )
            ),
            session_factory=session_factory,
            profiles_dir=profiles_dir,
            worker_id=worker_id,
        )
        res1 = await kernel1.execute(task1)
        assert res1.status == "completed"

    # 3. Stage 2 Task: Depend on Task 1 and reference its outputs dynamically
    adapter2 = CapturingAdapter(
        result_to_return=Result(status="completed", output={"deployed": True})
    )
    async with session_factory() as session:
        wl = WorkloadService(session=session)
        task2 = await wl.enqueue(
            worker_id=worker_id,
            title="Deploy canary image",
            objective="Roll out canary to production",
            input={
                "target_image": f"$dep:{t1_id}.image_tag",
                "target_digest": f"$dep:{t1_id}.digest",
                "target_port": f"$dep:{t1_id}.port",
                "is_canary": f"$dep:{t1_id}.metadata.canary",
                "service_endpoint": f"http://$dep:{t1_id}.metadata.cluster:$dep:{t1_id}.port/health",
                "complete_build_artifact": f"$dep:{t1_id}",
            },
            dependencies=[t1_id],
        )

        kernel2 = WorkerExecutionKernel(
            session=session,
            adapter=adapter2,
            session_factory=session_factory,
            profiles_dir=profiles_dir,
            worker_id=worker_id,
        )
        res2 = await kernel2.execute(task2)
        assert res2.status == "completed"

    # 4. Verify context delivered to adapter2
    ctx = adapter2.captured_context
    assert ctx is not None

    # CTX-01: Persona verification
    assert ctx.worker.id == worker_id
    assert ctx.profile.role == "devops"
    assert ctx.profile.persona.style == "meticulous"
    assert ctx.profile.persona.autonomy == "high"
    assert ctx.profile.persona.goals == ["zero-downtime deployment"]
    assert ctx.profile.instructions == "Strictly verify service health after deployment."

    # CTX-02: Upstream dependency results loaded
    assert t1_id in ctx.upstream_results
    assert ctx.upstream_results[t1_id]["image_tag"] == "v1.4.2"

    # CTX-03: Dynamic expression resolution with native typing & string interpolation
    assert ctx.task.input["target_image"] == "v1.4.2"
    assert ctx.task.input["target_digest"] == "sha256:4a5b6c7d8e9f"
    assert ctx.task.input["target_port"] == 8080
    assert isinstance(ctx.task.input["target_port"], int)
    assert ctx.task.input["is_canary"] is True
    assert isinstance(ctx.task.input["is_canary"], bool)
    assert ctx.task.input["service_endpoint"] == "http://prod-us-east-1:8080/health"
    assert ctx.task.input["complete_build_artifact"]["image_tag"] == "v1.4.2"

    # CTX-05: Worker recent experience retrieval
    assert len(ctx.recent_experience) >= 1
    assert any(exp["task_id"] == t1_id for exp in ctx.recent_experience)


@pytest.mark.asyncio
async def test_missing_dependency_fails_task_and_raises(
    database_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    profiles_dir: Path,
) -> None:
    """CTX-04: Missing upstream dependency fails task cleanly and raises error."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    worker_id = "worker-fail-safe"

    async with session_factory() as session:
        reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
        profile = WorkerProfile(version=1, role="tester", runtime="openclaw")
        await reg.register_worker(worker_id, profile)

        wl = WorkloadService(session=session)
        task = await wl.enqueue(
            worker_id=worker_id,
            title="Downstream task with bad ref",
            objective="Test fail-closed behavior",
            input={"data": "$dep:nonexistent-task-id.missing_key"},
            dependencies=["nonexistent-task-id"],
        )

        kernel = WorkerExecutionKernel(
            session=session,
            adapter=CapturingAdapter(Result(status="completed")),
            session_factory=session_factory,
            profiles_dir=profiles_dir,
            worker_id=worker_id,
        )

        with pytest.raises(DependencyArtifactResolutionError) as exc_info:
            await kernel.execute(task)

        assert "nonexistent-task-id" in str(exc_info.value)

    # Verify task in DB transitioned to 'failed' rather than hanging 'in_progress'
    async with session_factory() as session:
        result = await session.execute(
            text("SELECT status, last_error FROM tasks WHERE id = :id"),
            {"id": task.id},
        )
        row = result.first()
        assert row is not None
        assert row[0] == "failed"
        assert "Dependency artifact resolution failed" in str(row[1])


@pytest.mark.asyncio
async def test_compute_replacement_reconstructs_context_across_nodes(
    database_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    profiles_dir: Path,
) -> None:
    """CTX-08: Replacement compute node reproduces identical enriched context."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    worker_id = "worker-persist-node"

    # Step 1: Register worker
    async with session_factory() as session:
        reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
        profile = WorkerProfile(
            version=1,
            role="architect",
            runtime="openclaw",
            persona=Persona(autonomy="high", style="strategic"),
            instructions="Maintain architecture principles.",
        )
        await reg.register_worker(worker_id, profile)

    # Step 2: Node 1 runs stage 1 task
    t1_id: str = ""
    async with session_factory() as session_node1:
        wl = WorkloadService(session=session_node1)
        task1 = await wl.enqueue(
            worker_id=worker_id,
            title="Design database schema",
            objective="Create tables",
            input={"system": "billing"},
        )
        t1_id = task1.id

        kernel_node1 = WorkerExecutionKernel(
            session=session_node1,
            adapter=CapturingAdapter(
                Result(status="completed", output={"tables": ["invoices", "ledger"]})
            ),
            session_factory=session_factory,
            profiles_dir=profiles_dir,
            worker_id=worker_id,
            node_id="compute-node-alpha",
        )
        res1 = await kernel_node1.execute(task1)
        assert res1.status == "completed"

    # Step 3: Compute node-alpha terminates. Replacement node-beta boots up.
    # Node-beta executes stage 2 task referencing Task 1's output.
    adapter_node2 = CapturingAdapter(Result(status="completed", output={"migrated": True}))
    async with session_factory() as session_node2:
        wl = WorkloadService(session=session_node2)
        task2 = await wl.enqueue(
            worker_id=worker_id,
            title="Apply migrations",
            objective="Run migration scripts",
            input={"target_tables": f"$dep:{t1_id}.tables"},
            dependencies=[t1_id],
        )

        kernel_node2 = WorkerExecutionKernel(
            session=session_node2,
            adapter=adapter_node2,
            session_factory=session_factory,
            profiles_dir=profiles_dir,
            worker_id=worker_id,
            node_id="compute-node-beta",
        )
        res2 = await kernel_node2.execute(task2)
        assert res2.status == "completed"

    # Step 4: Verify node-beta received complete context
    ctx = adapter_node2.captured_context
    assert ctx is not None
    assert ctx.worker.id == worker_id
    assert ctx.profile.role == "architect"
    assert ctx.profile.persona.style == "strategic"
    assert ctx.task.input["target_tables"] == ["invoices", "ledger"]
    assert t1_id in ctx.upstream_results
    assert len(ctx.recent_experience) >= 1
    assert any(exp["task_id"] == t1_id for exp in ctx.recent_experience)
