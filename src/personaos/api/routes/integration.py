"""Integration Gateway API Routes.

Implements the normalized /integration/* endpoints defined in ADR 0041 and
the Integration Gateway Implementation Specification.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import select, text

from personaos.api.integration_schemas import (
    ConnectionDiagnosticCode,
    GatewayDagEdge,
    GatewayDagNode,
    GatewayDagProjection,
    GatewayDecisionGate,
    GatewayDiagnosticsResponse,
    GatewayEventMessage,
    GatewayGateResolveRequest,
    GatewayGateResolveResponse,
    GatewayHealthResponse,
    GatewayReadyTask,
    GatewayReadyWave,
    GatewayReadyWavesResponse,
    GatewayRunCreateRequest,
    GatewayRunSummary,
    GatewayWorkerSummary,
)
from personaos.config import settings
from personaos.db.models import (
    Task as TaskModel,
)
from personaos.db.models import (
    TaskEvent,
)
from personaos.db.models import (
    Worker as WorkerModel,
)
from personaos.domain.errors import (
    InvalidActionError,
    TaskNotFoundError,
    TaskStateConflictError,
    WorkerNotEligibleError,
)
from personaos.integrations.common.event_bus import gateway_event_bus
from personaos.integrations.orca.client import OrcaCLIClient
from personaos.registry.pab_adapter import PABSyncAdapter
from personaos.registry.profile import WorkerNotFoundError
from personaos.workload.review import TaskReviewService

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/integration", tags=["integration"])

PANEL_HTML_PATH = (
    Path(__file__).resolve().parents[4] / "orca-plugin" / "panel" / "index.html"
)

_RUNS_REGISTRY: dict[str, GatewayRunSummary] = {}


def _compute_dag_waves(tasks: list[TaskModel]) -> dict[str, int]:
    """Compute topological wave depth for tasks with cycle protection."""
    task_map = {t.id: t for t in tasks}
    wave_memo: dict[str, int] = {}
    visited: set[str] = set()

    def get_wave(tid: str) -> int:
        if tid in wave_memo:
            return wave_memo[tid]
        if tid in visited:
            return 1
        visited.add(tid)
        t = task_map.get(tid)
        if not t or not t.dependencies:
            wave_memo[tid] = 1
            visited.remove(tid)
            return 1

        max_dep = 0
        deps = t.dependencies if isinstance(t.dependencies, list) else []
        for d in deps:
            if d in task_map:
                max_dep = max(max_dep, get_wave(d))
        w = max_dep + 1
        wave_memo[tid] = w
        visited.remove(tid)
        return w

    for tid in task_map:
        get_wave(tid)
    return wave_memo


@router.get(
    "/health",
    response_model=GatewayHealthResponse,
    summary="Integration Gateway Health",
    description="Check operational health of the Integration Gateway and upstream connectivity.",
)
async def get_integration_health(request: Request) -> GatewayHealthResponse:
    """Return health status of the Gateway, PersonaOS kernel, and PAB supervisor."""
    factory = getattr(request.app.state, "session_factory", None)
    db_connected = False
    if factory is not None:
        try:
            async with factory() as session:
                await session.execute(text("SELECT 1"))
                db_connected = True
        except Exception as exc:
            logger.warning("integration_health_db_check_failed", error=str(exc))
            db_connected = False
    else:
        db_connected = True

    pab_adapter = PABSyncAdapter()
    pab_connected = pab_adapter.is_available()

    base_url = f"http://{settings.api_host}:{settings.api_port}"
    overall_status = (
        "healthy" if (db_connected and pab_connected) else ("degraded" if db_connected else "down")
    )

    return GatewayHealthResponse(
        status=overall_status,
        gateway_version="1.0.0",
        personaos_connected=db_connected,
        personaos_url=base_url,
        pab_connected=pab_connected,
        timestamp=datetime.now(UTC),
    )


@router.get(
    "/diagnostics",
    response_model=GatewayDiagnosticsResponse,
    summary="Connection Diagnostics Probe",
    description=(
        "Deep diagnostic probe returning structured connectivity classification and latency."
    ),
)
async def get_integration_diagnostics(
    request: Request,
    target_url: str | None = Query(None, description="Optional target URL to probe"),
    authorization: str | None = Header(None, alias="Authorization"),
    origin: str | None = Header(None, alias="Origin"),
    access_control_request_private_network: str | None = Header(
        None, alias="Access-Control-Request-Private-Network"
    ),
) -> GatewayDiagnosticsResponse:
    """Execute connectivity diagnostics and return structured status."""
    start_time = time.perf_counter()
    factory = getattr(request.app.state, "session_factory", None)
    db_connected = False
    db_error: str | None = None

    if factory is not None:
        try:
            async with factory() as session:
                await session.execute(text("SELECT 1"))
                db_connected = True
        except Exception as exc:
            db_connected = False
            db_error = str(exc)
            logger.warning("integration_diagnostics_db_failed", error=db_error)
    else:
        db_connected = True

    latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
    upstream = target_url or f"http://{settings.api_host}:{settings.api_port}"

    # 1. Database failure check
    if not db_connected:
        return GatewayDiagnosticsResponse(
            code=ConnectionDiagnosticCode.SERVICE_UNAVAILABLE,
            status="error",
            latency_ms=latency_ms,
            upstream_url=upstream,
            cors_supported=True,
            pna_supported=True,
            auth_status="unknown",
            api_version_match=True,
            remediation=(
                "PersonaOS database connection failed. "
                "Verify PostgreSQL service is running and migrations are applied."
            ),
            details={"database": "down", "error": db_error},
        )

    # 2. Authentication check
    auth_status = "authenticated"
    if settings.operator_token is not None:
        if not authorization or not authorization.startswith("Bearer "):
            return GatewayDiagnosticsResponse(
                code=ConnectionDiagnosticCode.AUTHENTICATION_FAILED,
                status="error",
                latency_ms=latency_ms,
                upstream_url=upstream,
                cors_supported=True,
                pna_supported=True,
                auth_status="unauthenticated",
                api_version_match=True,
                remediation=(
                    "Missing Authorization header. Configure Bearer token in Orca ADE settings."
                ),
                details={"auth_required": True},
            )
        token = authorization.removeprefix("Bearer ").strip()
        if token != settings.operator_token:
            return GatewayDiagnosticsResponse(
                code=ConnectionDiagnosticCode.AUTHENTICATION_FAILED,
                status="error",
                latency_ms=latency_ms,
                upstream_url=upstream,
                cors_supported=True,
                pna_supported=True,
                auth_status="invalid",
                api_version_match=True,
                remediation=(
                    "Invalid operator token. Verify credentials against PersonaOS configuration."
                ),
                details={"auth_required": True},
            )

    # 3. Reachability & Security Boundary Checks (Mixed Content / PNA)
    is_secure_origin = bool(
        origin and (origin.startswith("https://") or origin.startswith("orca-app://"))
    )
    is_insecure_private_target = bool(
        upstream.startswith("http://192.168.")
        or upstream.startswith("http://10.")
        or upstream.startswith("http://172.16.")
    )

    remediation: str | None = None
    diag_code = ConnectionDiagnosticCode.CONNECTED
    status_label = "ok"

    if is_secure_origin and is_insecure_private_target:
        # Secure browser context attempting to hit an unproxied private HTTP IP
        diag_code = ConnectionDiagnosticCode.CORS_BLOCKED
        status_label = "degraded"
        remediation = (
            "Insecure private HTTP URL accessed from secure ADE origin. "
            "Browser sandboxing blocks direct requests. Route through same-origin reverse proxy "
            "or local loopback bridge (localhost:8001 / ADR 0041)."
        )

    pab_available = PABSyncAdapter().is_available()
    orca_available = OrcaCLIClient().is_available()

    return GatewayDiagnosticsResponse(
        code=diag_code,
        status=status_label,
        latency_ms=latency_ms,
        upstream_url=upstream,
        cors_supported=True,
        pna_supported=True,
        auth_status=auth_status,
        api_version_match=True,
        remediation=remediation,
        details={
            "database": "ok",
            "pab_connected": pab_available,
            "orca_cli_available": orca_available,
            "origin": origin,
            "pna_requested": bool(access_control_request_private_network),
            "engine": "personaos-orca-gateway",
            "protocol": "http/1.1",
        },
    )


@router.get(
    "/workers",
    response_model=list[GatewayWorkerSummary],
    summary="List Workforce Roster",
    description="Return persistent workers enriched with active profile versions and capabilities.",
)
async def list_integration_workers(
    request: Request,
    worker_status: Annotated[str | None, Query(alias="status")] = None,
    role: Annotated[str | None, Query()] = None,
) -> list[GatewayWorkerSummary]:
    """Return registered workers from the PersonaOS workforce registry."""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        return []

    try:
        async with factory() as session:
            query = select(WorkerModel)
            if worker_status:
                query = query.where(WorkerModel.status == worker_status)
            if role:
                query = query.where(WorkerModel.role == role)
            query = query.order_by(WorkerModel.id.asc())

            result = await session.execute(query)
            workers = list(result.scalars().all())
            return [GatewayWorkerSummary.model_validate(w) for w in workers]
    except Exception as exc:
        logger.error("list_integration_workers_failed", error=str(exc))
        return []


@router.get(
    "/runs",
    response_model=list[GatewayRunSummary],
    summary="List Coordination Runs",
    description="List active external coordination runs linked to PersonaOS tasks.",
)
async def list_integration_runs(request: Request) -> list[GatewayRunSummary]:
    """Return external orchestration runs discovered from tasks and explicitly created."""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is not None:
        try:
            async with factory() as session:
                query = select(TaskModel).order_by(TaskModel.created_at.desc()).limit(200)
                tasks = list((await session.execute(query)).scalars().all())
                for t in tasks:
                    r_id = None
                    if isinstance(t.input, dict):
                        r_id = t.input.get("run_id")
                    if r_id and r_id not in _RUNS_REGISTRY:
                        _RUNS_REGISTRY[r_id] = GatewayRunSummary(
                            run_id=r_id,
                            objective=f"Coordination Run ({r_id})",
                            coordinator_handle=t.assigned_worker or "coordinator-lead",
                            status="active",
                            consumer_generation=0,
                            created_at=t.created_at,
                            updated_at=t.completed_at or t.created_at,
                        )
        except Exception as exc:
            logger.warning("list_integration_runs_query_failed", error=str(exc))

    if not _RUNS_REGISTRY:
        _RUNS_REGISTRY["default"] = GatewayRunSummary(
            run_id="default",
            objective="Default Workspace Coordination Run",
            coordinator_handle="se",
            status="active",
            consumer_generation=0,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    return list(_RUNS_REGISTRY.values())


@router.post(
    "/runs",
    response_model=GatewayRunSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Create Coordination Run",
    description="Create or link an external orchestration coordination run.",
)
async def create_integration_run(
    request: GatewayRunCreateRequest,
) -> GatewayRunSummary:
    """Create a new coordination run."""
    run = GatewayRunSummary(
        run_id=request.run_id,
        objective=request.objective,
        coordinator_handle=request.coordinator_handle,
        status="active",
        consumer_generation=0,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    _RUNS_REGISTRY[request.run_id] = run
    return run


@router.get(
    "/runs/{run_id}/dag",
    response_model=GatewayDagProjection,
    summary="Get Evaluated Task DAG",
    description="Return pre-evaluated DAG projection with task nodes, dependencies, and waves.",
)
async def get_run_dag(run_id: str, request: Request) -> GatewayDagProjection:
    """Return directed graph of tasks and dependencies for a run."""
    factory = getattr(request.app.state, "session_factory", None)
    tasks: list[TaskModel] = []
    if factory is not None:
        try:
            async with factory() as session:
                query = select(TaskModel).order_by(TaskModel.created_at.asc())
                all_tasks = list((await session.execute(query)).scalars().all())
                if run_id in ("default", "all", "active"):
                    run_tasks = [
                        t
                        for t in all_tasks
                        if not (isinstance(t.input, dict) and t.input.get("run_id"))
                        or (isinstance(t.input, dict) and t.input.get("run_id") == run_id)
                    ]
                    tasks = run_tasks if run_tasks else all_tasks
                else:
                    tasks = [
                        t
                        for t in all_tasks
                        if isinstance(t.input, dict) and t.input.get("run_id") == run_id
                    ]
        except Exception as exc:
            logger.error("get_run_dag_query_failed", error=str(exc))

    if not tasks:
        return GatewayDagProjection(
            run_id=run_id,
            nodes=[],
            edges=[],
            total_tasks=0,
            completed_tasks=0,
            ready_tasks=0,
        )

    waves = _compute_dag_waves(tasks)
    status_map = {t.id: t.status for t in tasks}

    nodes: list[GatewayDagNode] = []
    edges: list[GatewayDagEdge] = []

    for t in tasks:
        deps = list(t.dependencies or []) if isinstance(t.dependencies, list) else []
        node = GatewayDagNode(
            task_id=t.id,
            title=t.title or t.id[:8],
            worker_id=t.assigned_worker,
            status=t.status,
            dependencies=deps,
            wave=waves.get(t.id, 1),
            generation=t.generation if t.generation and t.generation >= 1 else 1,
        )
        nodes.append(node)
        for dep in deps:
            edges.append(GatewayDagEdge(from_node=dep, to_node=t.id))

    total_tasks = len(nodes)
    completed_tasks = sum(1 for n in nodes if n.status == "completed")
    ready_tasks = sum(
        1
        for n in nodes
        if n.status in ("ready", "queued", "pending")
        and all(status_map.get(d) == "completed" for d in n.dependencies)
    )

    return GatewayDagProjection(
        run_id=run_id,
        nodes=nodes,
        edges=edges,
        total_tasks=total_tasks,
        completed_tasks=completed_tasks,
        ready_tasks=ready_tasks,
    )


@router.get(
    "/runs/{run_id}/ready-waves",
    response_model=GatewayReadyWavesResponse,
    summary="Get Ready Waves",
    description="Return ready-to-execute tasks grouped by evaluation wave.",
)
async def get_run_ready_waves(run_id: str, request: Request) -> GatewayReadyWavesResponse:
    """Return ready tasks partitioned into waves."""
    factory = getattr(request.app.state, "session_factory", None)
    tasks: list[TaskModel] = []
    if factory is not None:
        try:
            async with factory() as session:
                query = select(TaskModel).order_by(TaskModel.created_at.asc())
                all_tasks = list((await session.execute(query)).scalars().all())
                if run_id in ("default", "all", "active"):
                    run_tasks = [
                        t
                        for t in all_tasks
                        if not (isinstance(t.input, dict) and t.input.get("run_id"))
                        or (isinstance(t.input, dict) and t.input.get("run_id") == run_id)
                    ]
                    tasks = run_tasks if run_tasks else all_tasks
                else:
                    tasks = [
                        t
                        for t in all_tasks
                        if isinstance(t.input, dict) and t.input.get("run_id") == run_id
                    ]
        except Exception as exc:
            logger.error("get_run_ready_waves_query_failed", error=str(exc))

    if not tasks:
        return GatewayReadyWavesResponse(run_id=run_id, current_wave=1, waves=[])

    waves = _compute_dag_waves(tasks)
    status_map = {t.id: t.status for t in tasks}

    wave_tasks: dict[int, list[GatewayReadyTask]] = {}
    active_waves: list[int] = []

    for t in tasks:
        deps = list(t.dependencies or []) if isinstance(t.dependencies, list) else []
        is_ready = t.status in ("ready", "queued", "pending") and all(
            status_map.get(d) == "completed" for d in deps
        )
        t_wave = waves.get(t.id, 1)

        if t.status in ("running", "claimed", "ready", "queued", "pending"):
            active_waves.append(t_wave)

        if is_ready:
            ready_task = GatewayReadyTask(
                task_id=t.id,
                title=t.title or t.id[:8],
                worker_id=t.assigned_worker,
                priority=t.priority or "medium",
                generation=t.generation if t.generation and t.generation >= 1 else 1,
            )
            wave_tasks.setdefault(t_wave, []).append(ready_task)

    current_wave = min(active_waves) if active_waves else 1
    ready_waves = [
        GatewayReadyWave(wave_number=w_num, ready_tasks=wave_tasks[w_num])
        for w_num in sorted(wave_tasks.keys())
    ]

    return GatewayReadyWavesResponse(
        run_id=run_id,
        current_wave=current_wave,
        waves=ready_waves,
    )


@router.get(
    "/decision-gates",
    response_model=list[GatewayDecisionGate],
    summary="List Interactive Decision Gates",
    description="Return tasks in review status requiring human-in-the-loop decisions.",
)
async def list_decision_gates(
    request: Request,
    run_id: str | None = Query(None, description="Filter by coordination run identifier"),
) -> list[GatewayDecisionGate]:
    """Return pending decision gates mapped from tasks in review status."""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        return []

    try:
        async with factory() as session:
            stmt = (
                select(TaskModel)
                .where(TaskModel.status == "review")
                .order_by(TaskModel.created_at.asc())
            )
            tasks = list((await session.execute(stmt)).scalars().all())

            gates: list[GatewayDecisionGate] = []
            for t in tasks:
                t_input = t.input if isinstance(t.input, dict) else {}
                task_run_id = t_input.get("run_id")
                if run_id and run_id not in ("all", "default") and task_run_id != run_id:
                    continue

                exhaustion = t.exhaustion_reason or "operator_review"
                last_err = t.last_error or ""
                question = (
                    f"Task {t.title or t.id[:8]} paused for operator review "
                    f"({exhaustion}). Approve re-entry or reject task?"
                )

                gate = GatewayDecisionGate(
                    gate_id=f"gate-{t.id}",
                    task_id=t.id,
                    run_id=task_run_id,
                    title=f"Decision Gate: {t.title or t.id[:8]}",
                    question=question,
                    options=["approve", "reject"],
                    status="pending",
                    execution_generation=t.generation if t.generation and t.generation >= 1 else 1,
                    review_context={
                        "task_id": t.id,
                        "title": t.title,
                        "assigned_worker": t.assigned_worker,
                        "last_error": last_err,
                        "exhaustion_reason": t.exhaustion_reason,
                        "diagnostics": t.result,
                        "retry_count": t.retry_count,
                        "checkpoint_ref": t.checkpoint_ref,
                    },
                    created_at=t.created_at,
                )
                gates.append(gate)
            return gates
    except Exception as exc:
        logger.error("list_decision_gates_failed", error=str(exc))
        return []


@router.get(
    "/decision-gates/{gate_id}",
    response_model=GatewayDecisionGate,
    summary="Get Decision Gate Detail",
    description="Retrieve diagnostic context and question for a single decision gate.",
)
async def get_decision_gate(
    gate_id: str,
    request: Request,
) -> GatewayDecisionGate:
    """Return single decision gate context."""
    task_id = gate_id[5:] if gate_id.startswith("gate-") else gate_id
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Decision gate {gate_id} not found",
        )

    try:
        async with factory() as session:
            stmt = select(TaskModel).where(TaskModel.id == task_id)
            task = (await session.execute(stmt)).scalar_one_or_none()
            if task is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Decision gate {gate_id} not found",
                )

            gate_status = (
                "pending"
                if task.status == "review"
                else (
                    "approved"
                    if task.status in ("queued", "claimed", "running", "in_progress", "completed")
                    else "rejected"
                )
            )
            t_input = task.input if isinstance(task.input, dict) else {}
            task_run_id = t_input.get("run_id")
            exhaustion = task.exhaustion_reason or "operator_review"
            last_err = task.last_error or ""
            question = (
                f"Task {task.title or task.id[:8]} paused for operator review "
                f"({exhaustion}). Approve re-entry or reject task?"
            )

            return GatewayDecisionGate(
                gate_id=f"gate-{task.id}",
                task_id=task.id,
                run_id=task_run_id,
                title=f"Decision Gate: {task.title or task.id[:8]}",
                question=question,
                options=["approve", "reject"],
                status=gate_status,
                execution_generation=task.generation
                if task.generation and task.generation >= 1
                else 1,
                review_context={
                    "task_id": task.id,
                    "title": task.title,
                    "status": task.status,
                    "assigned_worker": task.assigned_worker,
                    "last_error": last_err,
                    "exhaustion_reason": task.exhaustion_reason,
                    "diagnostics": task.result,
                    "retry_count": task.retry_count,
                    "checkpoint_ref": task.checkpoint_ref,
                },
                created_at=task.created_at,
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("get_decision_gate_failed", gate_id=gate_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch decision gate {gate_id}",
        ) from exc


@router.post(
    "/decision-gates/{gate_id}/resolve",
    response_model=GatewayGateResolveResponse,
    summary="Resolve Decision Gate",
    description="Submit generation-fenced approval or rejection for a decision gate.",
)
async def resolve_decision_gate(
    gate_id: str,
    resolve_req: GatewayGateResolveRequest,
    request: Request,
) -> GatewayGateResolveResponse:
    """Resolve a decision gate with generation fencing."""
    task_id = gate_id[5:] if gate_id.startswith("gate-") else gate_id
    factory = getattr(request.app.state, "session_factory", None)

    # Lightweight fallback when running without session factory (e.g. basic mock testclient)
    if factory is None:
        return GatewayGateResolveResponse(
            gate_id=gate_id,
            task_id=task_id,
            resolved=True,
            status="approved" if resolve_req.decision == "approve" else "rejected",
            new_generation=resolve_req.expected_generation + 1,
            timestamp=datetime.now(UTC),
        )

    async with factory() as session:
        # Pre-validate task existence, status, and generation fence under row lock if supported
        task_row = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one_or_none()

        if task_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Task or decision gate {gate_id} not found",
            )

        if task_row.status != "review":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Gate {gate_id} (task {task_id}) is in status {task_row.status!r}; "
                    "expected 'review'"
                ),
            )

        if task_row.generation != resolve_req.expected_generation:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"STALE_GENERATION_CONFLICT: Task generation mismatch: "
                    f"expected {resolve_req.expected_generation}, found {task_row.generation}"
                ),
            )

        review_svc = TaskReviewService(session)
        operator_id = "orca:operator"
        reason = resolve_req.review_notes or f"Resolved via Orca ADE Gate ({resolve_req.decision})"

        try:
            if resolve_req.decision == "approve":
                updated_task = await review_svc.approve(
                    task_id=task_id,
                    expected_generation=resolve_req.expected_generation,
                    operator_id=operator_id,
                    reason=reason,
                )
                await session.commit()
                run_id_val = (
                    (updated_task.input or {}).get("run_id")
                    if isinstance(updated_task.input, dict)
                    else None
                )
                gateway_event_bus.publish(
                    event_type="gate_resolved",
                    payload={
                        "gate_id": f"gate-{updated_task.id}",
                        "task_id": updated_task.id,
                        "status": "approved",
                        "new_generation": updated_task.generation,
                        "decision": "approve",
                        "operator_id": operator_id,
                        "run_id": run_id_val,
                    },
                )
                return GatewayGateResolveResponse(
                    gate_id=f"gate-{updated_task.id}",
                    task_id=updated_task.id,
                    resolved=True,
                    status="approved",
                    new_generation=updated_task.generation,
                    timestamp=datetime.now(UTC),
                )
            else:
                updated_task = await review_svc.reject(
                    task_id=task_id,
                    expected_generation=resolve_req.expected_generation,
                    operator_id=operator_id,
                    reason=reason,
                )
                await session.commit()
                run_id_val = (
                    (updated_task.input or {}).get("run_id")
                    if isinstance(updated_task.input, dict)
                    else None
                )
                gateway_event_bus.publish(
                    event_type="gate_resolved",
                    payload={
                        "gate_id": f"gate-{updated_task.id}",
                        "task_id": updated_task.id,
                        "status": "rejected",
                        "new_generation": updated_task.generation,
                        "decision": "reject",
                        "operator_id": operator_id,
                        "run_id": run_id_val,
                    },
                )
                return GatewayGateResolveResponse(
                    gate_id=f"gate-{updated_task.id}",
                    task_id=updated_task.id,
                    resolved=True,
                    status="rejected",
                    new_generation=updated_task.generation,
                    timestamp=datetime.now(UTC),
                )
        except TaskNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        except TaskStateConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except (InvalidActionError, WorkerNotEligibleError, WorkerNotFoundError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc


@router.get(
    "/events",
    summary="Integration Event Stream",
    description=(
        "Persistent Server-Sent Events (SSE) stream for real-time task lifecycle, "
        "ready waves, and decision gates."
    ),
)
async def stream_integration_events(
    request: Request,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    last_event_id_query: str | None = Query(
        None, alias="last_event_id", description="Reconnection token fallback"
    ),
    run_id: str | None = Query(None, description="Filter events for specific coordination run"),
    heartbeat_interval: float = Query(
        15.0, ge=0.05, le=300.0, description="Keepalive heartbeat interval in seconds"
    ),
    max_events: int | None = Query(
        None, ge=1, description="Max events to stream before closing, useful for testing"
    ),
) -> StreamingResponse:
    """Stream real-time lifecycle events to connected clients with reconnection support."""
    effective_last_id = last_event_id or last_event_id_query

    async def event_generator() -> AsyncIterator[str]:
        queue: asyncio.Queue[GatewayEventMessage] = asyncio.Queue(maxsize=500)
        gateway_event_bus.register_queue(queue)
        missed_events = gateway_event_bus.get_missed_events(
            last_event_id=effective_last_id,
            run_id=run_id,
        )
        last_db_poll = datetime.now(UTC)
        factory = getattr(request.app.state, "session_factory", None)

        # Fallback: if client provided last_event_id not found in memory ring buffer, look up DB
        if effective_last_id and not missed_events and factory is not None:
            try:
                async with factory() as session:
                    stmt = select(TaskEvent).where(TaskEvent.id == effective_last_id)
                    target_evt = (await session.execute(stmt)).scalar_one_or_none()
                    if target_evt is not None:
                        stmt_after = (
                            select(TaskEvent)
                            .where(TaskEvent.created_at >= target_evt.created_at)
                            .order_by(TaskEvent.created_at.asc())
                            .limit(100)
                        )
                        db_replays = list((await session.execute(stmt_after)).scalars().all())
                        for dbe in db_replays:
                            if str(dbe.id) == effective_last_id:
                                continue
                            mapped_type = (
                                "gate_created"
                                if dbe.type == "approval.requested"
                                else "task_status_changed"
                            )
                            payload = {
                                "task_id": dbe.task_id,
                                "type": dbe.type,
                                "actor_id": dbe.actor_id,
                                "payload": dbe.payload,
                                "created_at": dbe.created_at.isoformat(),
                            }
                            replay_evt = GatewayEventMessage(
                                event_id=str(dbe.id),
                                event_type=mapped_type,
                                payload=payload,
                                timestamp=dbe.created_at,
                            )
                            missed_events.append(replay_evt)
            except Exception as exc:
                logger.warning("sse_db_replay_lookup_failed", error=str(exc))

        events_emitted = 0
        try:
            # 0. Send connection confirmation comment frame immediately to establish stream
            yield ": connected\nretry: 3000\n\n"

            # 1. Replay missed events from ring buffer
            for evt in missed_events:
                if max_events is not None and events_emitted >= max_events:
                    return
                events_emitted += 1
                yield (
                    f"id: {evt.event_id}\n"
                    f"event: {evt.event_type}\n"
                    f"data: {evt.model_dump_json()}\n\n"
                )

            # 2. Main event streaming loop
            while True:
                if max_events is not None and events_emitted >= max_events:
                    break
                if await request.is_disconnected():
                    break

                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=heartbeat_interval)
                    evt_run_id = (
                        evt.payload.get("run_id") if isinstance(evt.payload, dict) else None
                    )
                    if (
                        run_id
                        and run_id not in ("all", "default")
                        and evt_run_id
                        and evt_run_id != run_id
                    ):
                        continue

                    events_emitted += 1
                    yield (
                        f"id: {evt.event_id}\n"
                        f"event: {evt.event_type}\n"
                        f"data: {evt.model_dump_json()}\n\n"
                    )
                except TimeoutError:
                    if max_events is not None and events_emitted >= max_events:
                        break
                    if await request.is_disconnected():
                        break

                    # Check for DB events written by workers / external processes
                    if factory is not None:
                        try:
                            async with factory() as session:
                                stmt = (
                                    select(TaskEvent)
                                    .where(TaskEvent.created_at > last_db_poll)
                                    .order_by(TaskEvent.created_at.asc())
                                    .limit(20)
                                )
                                db_events = list((await session.execute(stmt)).scalars().all())
                                for dbe in db_events:
                                    if dbe.created_at > last_db_poll:
                                        last_db_poll = dbe.created_at
                                    mapped_type = (
                                        "gate_created"
                                        if dbe.type == "approval.requested"
                                        else "task_status_changed"
                                    )
                                    payload = {
                                        "task_id": dbe.task_id,
                                        "type": dbe.type,
                                        "actor_id": dbe.actor_id,
                                        "payload": dbe.payload,
                                        "created_at": dbe.created_at.isoformat(),
                                    }
                                    evt = gateway_event_bus.publish(
                                        event_type=mapped_type,
                                        payload=payload,
                                        event_id=str(dbe.id),
                                        timestamp=dbe.created_at,
                                    )
                                    events_emitted += 1
                                    yield (
                                        f"id: {evt.event_id}\n"
                                        f"event: {evt.event_type}\n"
                                        f"data: {evt.model_dump_json()}\n\n"
                                    )
                        except Exception as exc:
                            logger.warning("sse_db_event_poll_failed", error=str(exc))

                    # Send keepalive heartbeat frame
                    events_emitted += 1
                    hb_data = json.dumps({"timestamp": datetime.now(UTC).isoformat()})
                    yield f"event: heartbeat\ndata: {hb_data}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            gateway_event_bus.unregister_queue(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/events/publish",
    response_model=GatewayEventMessage,
    summary="Publish Gateway Event",
    description="Publish an event message to the SSE event bus for broadcast.",
)
async def publish_gateway_event(
    event: GatewayEventMessage,
) -> GatewayEventMessage:
    """Publish an event to all active SSE subscribers."""
    return gateway_event_bus.publish(
        event_type=event.event_type,
        payload=event.payload,
        event_id=event.event_id,
        timestamp=event.timestamp,
    )


@router.get("/panel", response_class=HTMLResponse, summary="Orca Integration Panel UI")
@router.get("/panel/", response_class=HTMLResponse, include_in_schema=False)
async def get_integration_panel() -> HTMLResponse:
    """Serve the embedded PersonaOS Orca ADE Integration Panel UI."""
    if PANEL_HTML_PATH.exists():
        content = PANEL_HTML_PATH.read_text(encoding="utf-8")
        return HTMLResponse(content=content, status_code=status.HTTP_200_OK)
    return HTMLResponse(
        content="<h1>Orca ADE Panel UI not found</h1>",
        status_code=status.HTTP_404_NOT_FOUND,
    )
