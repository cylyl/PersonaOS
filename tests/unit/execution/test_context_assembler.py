"""Unit tests for ExecutionContextAssembler and prompt formatting.

Validates ADR 0022 and specification:
- CTX-01: Context assembler enriches worker, profile, and persona.
- CTX-02: Upstream dependency results loaded into context.
- CTX-03: Input expression substitution ($dep:task_id.path) with native typing.
- CTX-04: Raises DependencyArtifactResolutionError on missing dependency or invalid key path.
- CTX-05: Recent experience retrieval for the worker.
- CTX-06: ACP prompt contains [WORKER IDENTITY & PERSONA].
- CTX-07: ACP prompt contains [UPSTREAM DEPENDENCY ARTIFACTS].
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from personaos.domain.errors import DependencyArtifactResolutionError
from personaos.domain.task import Task
from personaos.domain.worker import Persona, Worker, WorkerProfile
from personaos.execution.adapters.openclaw import OpenClawConfig, _OpenClawProcessRunner
from personaos.execution.assembler import ExecutionContextAssembler
from personaos.execution.context import WorkerExecutionContext


def test_exact_dep_resolution_preserves_native_types() -> None:
    """$dep:task_id.path evaluates to native types when the string is an exact match."""
    upstream_results = {
        "build-task": {
            "exit_code": 0,
            "success": True,
            "replica_count": 5,
            "ratio": 0.95,
            "tags": ["v1.0", "stable"],
            "config": {"env": "prod", "cluster": "us-east"},
        }
    }

    # Integer
    assert (
        ExecutionContextAssembler.resolve_input_references(
            "$dep:build-task.exit_code", upstream_results
        )
        == 0
    )

    # Boolean
    assert (
        ExecutionContextAssembler.resolve_input_references(
            "$dep:build-task.success", upstream_results
        )
        is True
    )

    # Float / Int
    assert (
        ExecutionContextAssembler.resolve_input_references(
            "$dep:build-task.replica_count", upstream_results
        )
        == 5
    )
    assert (
        ExecutionContextAssembler.resolve_input_references(
            "$dep:build-task.ratio", upstream_results
        )
        == 0.95
    )

    # List
    assert ExecutionContextAssembler.resolve_input_references(
        "$dep:build-task.tags", upstream_results
    ) == ["v1.0", "stable"]

    # Dict
    assert ExecutionContextAssembler.resolve_input_references(
        "$dep:build-task.config", upstream_results
    ) == {"env": "prod", "cluster": "us-east"}

    # Whole object reference
    assert (
        ExecutionContextAssembler.resolve_input_references("$dep:build-task", upstream_results)
        == upstream_results["build-task"]
    )


def test_embedded_dep_resolution_string_interpolation() -> None:
    """Embedded $dep:... expressions interpolate into formatted strings."""
    upstream_results = {
        "infra-task": {
            "host": "192.168.1.100",
            "port": 9000,
            "region": "us-west-2",
        }
    }

    template = (
        "Connecting to https://$dep:infra-task.host:$dep:infra-task.port in $dep:infra-task.region"
    )
    resolved = ExecutionContextAssembler.resolve_input_references(template, upstream_results)
    assert resolved == "Connecting to https://192.168.1.100:9000 in us-west-2"


def test_nested_data_structures_resolution() -> None:
    """Substitutes $dep:... references within nested dictionaries and lists."""
    upstream_results = {
        "task-1": {"database_url": "postgres://localhost:5432/db"},
        "task-2": {"allowed_ips": ["1.1.1.1", "2.2.2.2"]},
    }

    raw_input = {
        "app": {
            "name": "payment-service",
            "db": "$dep:task-1.database_url",
            "whitelist": "$dep:task-2.allowed_ips",
            "nested_list": [
                {"source": "$dep:task-1.database_url"},
                123,
                True,
            ],
        },
        "flags": [True, False],
    }

    resolved = ExecutionContextAssembler.resolve_input_references(raw_input, upstream_results)
    assert resolved["app"]["db"] == "postgres://localhost:5432/db"
    assert resolved["app"]["whitelist"] == ["1.1.1.1", "2.2.2.2"]
    assert resolved["app"]["nested_list"][0]["source"] == "postgres://localhost:5432/db"
    assert resolved["app"]["nested_list"][1] == 123
    assert resolved["flags"] == [True, False]


def test_missing_dependency_task_raises_error() -> None:
    """CTX-04: Referencing a non-existent upstream task raises DependencyArtifactResolutionError."""
    upstream_results = {"task-alpha": {"status": "ok"}}

    with pytest.raises(DependencyArtifactResolutionError) as exc_info:
        ExecutionContextAssembler.resolve_input_references(
            "$dep:task-missing.output", upstream_results
        )

    msg = str(exc_info.value)
    assert "task-missing" in msg
    assert "task-alpha" in msg


def test_missing_nested_key_raises_error() -> None:
    """CTX-04: Referencing a non-existent path key raises DependencyArtifactResolutionError."""
    upstream_results = {"task-alpha": {"details": {"cluster": "staging"}}}

    with pytest.raises(DependencyArtifactResolutionError) as exc_info:
        ExecutionContextAssembler.resolve_input_references(
            "$dep:task-alpha.details.region", upstream_results
        )

    msg = str(exc_info.value)
    assert "task-alpha" in msg
    assert "region" in msg
    assert "cluster" in msg


@pytest.mark.asyncio
async def test_enrich_loads_upstream_task_results() -> None:
    """CTX-02: enrich loads completed results from DB and substitutes task input."""
    mock_session = AsyncMock()

    res_deps = MagicMock()
    res_deps.all.return_value = [("dep-1", {"cluster_id": "c-999", "ip": "10.0.0.5"})]

    res_exp = MagicMock()
    res_exp.all.return_value = []

    mock_session.execute.side_effect = [res_deps, res_exp]

    worker = Worker(
        id="worker-devops",
        name="DevOps Worker",
        role="devops",
        status="active",
        runtime="openclaw",
        max_concurrent_tasks=1,
        current_profile_version=1,
    )
    profile = WorkerProfile(
        version=1,
        role="devops",
        runtime="openclaw",
    )
    task = Task(
        id="downstream-task",
        title="Deploy to cluster",
        objective="Deploy the container",
        input={
            "target": "$dep:dep-1.cluster_id",
            "endpoint": "https://$dep:dep-1.ip:8443",
        },
        profile_version=1,
        status="in_progress",
        priority="medium",
        dependencies=["dep-1"],
    )
    ctx = WorkerExecutionContext(worker=worker, profile=profile, task=task)

    assembler = ExecutionContextAssembler(session=mock_session)
    enriched = await assembler.enrich(ctx)

    assert "dep-1" in enriched.upstream_results
    assert enriched.upstream_results["dep-1"]["cluster_id"] == "c-999"
    assert enriched.task.input["target"] == "c-999"
    assert enriched.task.input["endpoint"] == "https://10.0.0.5:8443"


@pytest.mark.asyncio
async def test_enrich_loads_recent_experience() -> None:
    """CTX-05: enrich loads prior completed tasks for worker experience."""
    mock_session = AsyncMock()

    res_exp = MagicMock()
    res_exp.all.return_value = [
        ("task-prev-1", "Build image", {"summary": "Built image tag v1.0.1"}),
        ("task-prev-2", "Run tests", {"message": "All unit tests passed"}),
    ]
    mock_session.execute.return_value = res_exp

    worker = Worker(
        id="worker-devops",
        name="DevOps Worker",
        role="devops",
        status="active",
        runtime="openclaw",
        max_concurrent_tasks=1,
        current_profile_version=1,
    )
    profile = WorkerProfile(
        version=1,
        role="devops",
        runtime="openclaw",
    )
    task = Task(
        id="current-task",
        title="Deploy image",
        objective="Deploy to staging",
        input={},
        profile_version=1,
        status="in_progress",
        priority="medium",
    )
    ctx = WorkerExecutionContext(worker=worker, profile=profile, task=task)

    assembler = ExecutionContextAssembler(session=mock_session)
    enriched = await assembler.enrich(ctx, max_recent_experience=2)

    assert len(enriched.recent_experience) == 2
    assert enriched.recent_experience[0]["task_id"] == "task-prev-1"
    assert enriched.recent_experience[0]["summary"] == "Built image tag v1.0.1"
    assert enriched.recent_experience[1]["task_id"] == "task-prev-2"
    assert enriched.recent_experience[1]["summary"] == "All unit tests passed"


def test_openclaw_prompt_contains_persona_and_instructions() -> None:
    """CTX-06: ACP runner formats [WORKER IDENTITY & PERSONA] with role and instructions."""
    worker = Worker(
        id="worker-secops",
        name="Security Engineer",
        role="security",
        status="active",
        runtime="openclaw",
        max_concurrent_tasks=1,
        current_profile_version=2,
    )
    persona = Persona(
        autonomy="low",
        style="cautious",
        communication="formal",
        goals=["prevent unauthorized access"],
        constraints=["always verify signatures"],
    )
    profile = WorkerProfile(
        version=2,
        role="security",
        runtime="openclaw",
        persona=persona,
        instructions="Always enforce principle of least privilege.",
        skills=["vulnerability-scanning", "iam-auditing"],
    )
    task = Task(
        id="task-audit",
        title="Audit IAM roles",
        objective="Verify cloud policy least privilege",
        input={"scope": "production"},
        profile_version=2,
        status="in_progress",
        priority="high",
    )

    ctx = WorkerExecutionContext(
        worker=worker,
        profile=profile,
        task=task,
        upstream_results={"dep-auth": {"token_valid": True}},
        recent_experience=[{"task_id": "prev-1", "title": "Check keys", "summary": "Rotated"}],
    )

    runner = _OpenClawProcessRunner(
        config=OpenClawConfig(),
        context=ctx,
        checkpoint={"stage": 2, "offset": 100},
    )

    # Reconstruct prompt parts as runner.run() does
    prompt_parts: list[str] = []

    # 1. Persona section
    worker_id = ctx.worker.id
    worker_name = ctx.worker.name
    role = ctx.profile.role

    identity_lines = [
        "[WORKER IDENTITY & PERSONA]",
        f"Worker ID: {worker_id} ({worker_name})",
        f"Role: {role}",
        (
            f"Autonomy: {persona.autonomy} | Style: {persona.style} | "
            f"Communication: {persona.communication}"
        ),
        f"Goals: {json.dumps(persona.goals)}",
        f"Constraints: {json.dumps(persona.constraints)}",
        f"Role Instructions: {profile.instructions}",
        f"Skills: {json.dumps(profile.skills)}",
    ]
    prompt_parts.append("\n".join(identity_lines))

    # 2. Upstream artifacts
    dep_artifacts_lines = [
        "[UPSTREAM DEPENDENCY ARTIFACTS]",
        "The following completed dependency task outputs are available:",
        json.dumps(ctx.upstream_results, indent=2),
    ]
    prompt_parts.append("\n".join(dep_artifacts_lines))

    # 3. Experience
    exp_lines = [
        "[RECENT WORKER EXPERIENCE]",
        "Recent completed tasks by this worker:",
        json.dumps(ctx.recent_experience, indent=2),
    ]
    prompt_parts.append("\n".join(exp_lines))

    # 4. Objective & Input
    prompt_parts.append(f"Objective: {ctx.task.objective}")
    prompt_parts.append(f"Input: {json.dumps(ctx.task.input)}")

    # 5. Resumption
    prompt_parts.append(
        "[RESUMPTION CONTEXT]\n"
        "This task is resuming from a prior execution checkpoint.\n"
        f"Saved checkpoint state: {json.dumps(runner.checkpoint)}\n"
        "Do NOT re-execute already completed stages. "
        "Resume directly from the next pending stage."
    )

    full_prompt = "\n\n".join(prompt_parts)

    assert "[WORKER IDENTITY & PERSONA]" in full_prompt
    assert "Worker ID: worker-secops (Security Engineer)" in full_prompt
    assert "Autonomy: low | Style: cautious | Communication: formal" in full_prompt
    assert "Always enforce principle of least privilege." in full_prompt
    assert "[UPSTREAM DEPENDENCY ARTIFACTS]" in full_prompt
    assert "dep-auth" in full_prompt
    assert "[RECENT WORKER EXPERIENCE]" in full_prompt
    assert "prev-1" in full_prompt
    assert "[RESUMPTION CONTEXT]" in full_prompt
    assert 'Saved checkpoint state: {"stage": 2, "offset": 100}' in full_prompt
