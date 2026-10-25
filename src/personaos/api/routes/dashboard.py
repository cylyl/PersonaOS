"""Dashboard API routes — worker and workload operational observability.

Per ADR 0030 and docs/specs/worker-and-workload-dashboards.md:
Provides REST metrics endpoints and an embedded operator dashboard UI.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from personaos.api.deps import SessionDep
from personaos.api.schemas import (
    TaskTelemetryResponse,
    WorkerDashboardItem,
    WorkerDashboardResponse,
    WorkloadDashboardResponse,
)
from personaos.db.models import Task as TaskModel
from personaos.db.models import Worker as WorkerModel
from personaos.telemetry.service import TelemetryService

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
_telemetry_service = TelemetryService()


@router.get("/workload", response_model=WorkloadDashboardResponse)
async def get_workload_dashboard(
    session: SessionDep,
    window_days: Annotated[int | None, Query(ge=1, le=365)] = None,
) -> WorkloadDashboardResponse:
    """Retrieve overall workload metrics, queue status, latency, and costs."""
    agg = await _telemetry_service.aggregate_workload_metrics(session, window_days=window_days)
    return WorkloadDashboardResponse(
        total_tasks=agg.total_tasks,
        completed_tasks=agg.completed_tasks,
        failed_tasks=agg.failed_tasks,
        queued_tasks=agg.queued_tasks,
        in_progress_tasks=agg.in_progress_tasks,
        review_tasks=agg.review_tasks,
        avg_queue_wait_ms=agg.avg_queue_wait_ms,
        avg_execution_ms=agg.avg_execution_ms,
        retry_rate=agg.retry_rate,
        total_tokens=agg.total_tokens,
        total_cost_usd=agg.total_cost_usd,
        status_breakdown=agg.status_breakdown,
        priority_breakdown=agg.priority_breakdown,
    )


@router.get("/workers", response_model=WorkerDashboardResponse)
async def get_workers_dashboard(
    session: SessionDep,
    window_days: Annotated[int | None, Query(ge=1, le=365)] = None,
) -> WorkerDashboardResponse:
    """Retrieve workforce operational status and aggregated metrics per worker."""
    workers_stmt = select(WorkerModel).order_by(WorkerModel.created_at.asc())
    workers_res = await session.execute(workers_stmt)
    workers = list(workers_res.scalars().all())

    # Count active tasks per worker
    active_stmt = (
        select(TaskModel.assigned_worker, func.count(TaskModel.id))
        .where(TaskModel.status == "in_progress")
        .group_by(TaskModel.assigned_worker)
    )
    active_res = await session.execute(active_stmt)
    active_counts = {row[0]: row[1] for row in active_res.all() if row[0]}

    items: list[WorkerDashboardItem] = []
    for w in workers:
        metrics_agg = await _telemetry_service.aggregate_worker_metrics(
            session, worker_id=w.id, window_days=window_days
        )
        items.append(
            WorkerDashboardItem(
                worker_id=w.id,
                name=w.name,
                role=w.role,
                status=w.status,
                current_profile_version=w.current_profile_version,
                active_tasks_count=active_counts.get(w.id, 0),
                metrics=metrics_agg.to_dict(),
            )
        )

    return WorkerDashboardResponse(workers=items, total_workers=len(items))


@router.get("/workers/{worker_id}", response_model=WorkerDashboardItem)
async def get_worker_dashboard_detail(
    worker_id: str,
    session: SessionDep,
    window_days: Annotated[int | None, Query(ge=1, le=365)] = None,
) -> WorkerDashboardItem:
    """Retrieve detailed metrics for a specific worker."""
    worker_stmt = select(WorkerModel).where(WorkerModel.id == worker_id)
    worker_res = await session.execute(worker_stmt)
    worker = worker_res.scalars().first()
    if not worker:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Worker '{worker_id}' not found",
        )

    active_stmt = select(func.count(TaskModel.id)).where(
        TaskModel.assigned_worker == worker_id, TaskModel.status == "in_progress"
    )
    active_res = await session.execute(active_stmt)
    active_count = active_res.scalar() or 0

    metrics_agg = await _telemetry_service.aggregate_worker_metrics(
        session, worker_id=worker_id, window_days=window_days
    )

    return WorkerDashboardItem(
        worker_id=worker.id,
        name=worker.name,
        role=worker.role,
        status=worker.status,
        current_profile_version=worker.current_profile_version,
        active_tasks_count=active_count,
        metrics=metrics_agg.to_dict(),
    )


@router.get("/tasks/{task_id}/telemetry", response_model=TaskTelemetryResponse)
async def get_task_telemetry_detail(
    task_id: str,
    session: SessionDep,
) -> TaskTelemetryResponse:
    """Retrieve execution telemetry for an individual task."""
    telemetry = await _telemetry_service.get_task_telemetry(session, task_id)
    if not telemetry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found or telemetry unavailable",
        )

    return TaskTelemetryResponse(
        task_id=telemetry.task_id,
        worker_id=telemetry.worker_id,
        node_id=telemetry.node_id,
        model_name=telemetry.model_name,
        status=telemetry.status,
        quality_score=telemetry.quality_score,
        queue_wait_ms=telemetry.queue_wait_ms,
        execution_duration_ms=telemetry.execution_duration_ms,
        total_latency_ms=telemetry.total_latency_ms,
        retry_count=telemetry.retry_count,
        input_tokens=telemetry.input_tokens,
        output_tokens=telemetry.output_tokens,
        total_tokens=telemetry.total_tokens,
        cost_usd=telemetry.cost_usd,
        recorded_at=telemetry.recorded_at,
        metadata=telemetry.metadata,
    )


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>PersonaOS Control Plane — Workload & Worker Dashboard</title>
  <style>
    :root {
      --bg: #0f172a;
      --card: #1e293b;
      --text: #f8fafc;
      --muted: #94a3b8;
      --accent: #38bdf8;
      --success: #4ade80;
      --warning: #facc15;
      --danger: #f87171;
    }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      margin: 0;
      padding: 24px;
    }
    h1, h2 { font-weight: 600; margin-top: 0; }
    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid #334155;
      padding-bottom: 16px;
      margin-bottom: 24px;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 16px;
      margin-bottom: 24px;
    }
    .card {
      background: var(--card);
      border-radius: 8px;
      padding: 18px;
      border: 1px solid #334155;
    }
    .card-title {
      font-size: 0.85rem;
      text-transform: uppercase;
      color: var(--muted);
      letter-spacing: 0.05em;
      margin-bottom: 8px;
    }
    .card-val { font-size: 1.8rem; font-weight: 700; color: var(--text); }
    .table-container {
      background: var(--card);
      border-radius: 8px;
      border: 1px solid #334155;
      overflow: hidden;
      margin-bottom: 24px;
    }
    table { width: 100%; border-collapse: collapse; text-align: left; }
    th, td { padding: 12px 16px; border-bottom: 1px solid #334155; font-size: 0.9rem; }
    th { background: #0b1329; color: var(--muted); font-weight: 600; }
    .status-badge {
      display: inline-block;
      padding: 3px 8px;
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: 600;
    }
    .status-active { background: rgba(74, 222, 128, 0.2); color: var(--success); }
    .status-paused { background: rgba(250, 204, 21, 0.2); color: var(--warning); }
    .status-quarantined { background: rgba(248, 113, 113, 0.2); color: var(--danger); }
  </style>
</head>
<body>
  <div class="header">
    <div>
      <h1>PersonaOS Dashboard</h1>
      <p style="color:var(--muted);margin:0;">Persistent AI Workforce & Workload Operations</p>
    </div>
    <div id="last-updated" style="color:var(--muted);font-size:0.85rem;"></div>
  </div>

  <h2>Workload Overview</h2>
  <div class="grid" id="workload-cards">
    <div class="card">
      <div class="card-title">Queued Tasks</div>
      <div class="card-val" id="val-queued">-</div>
    </div>
    <div class="card">
      <div class="card-title">In Progress</div>
      <div class="card-val" id="val-running">-</div>
    </div>
    <div class="card">
      <div class="card-title">Completed</div>
      <div class="card-val" id="val-completed">-</div>
    </div>
    <div class="card">
      <div class="card-title">Failed</div>
      <div class="card-val" id="val-failed">-</div>
    </div>
    <div class="card">
      <div class="card-title">Avg Latency</div>
      <div class="card-val" id="val-latency">-</div>
    </div>
    <div class="card">
      <div class="card-title">Total Spend (USD)</div>
      <div class="card-val" id="val-cost">-</div>
    </div>
  </div>

  <h2>Worker Fleet</h2>
  <div class="table-container">
    <table>
      <thead>
        <tr>
          <th>Worker ID</th>
          <th>Name</th>
          <th>Role</th>
          <th>Status</th>
          <th>Active Tasks</th>
          <th>Success Rate</th>
          <th>Avg Quality</th>
          <th>Total Cost</th>
        </tr>
      </thead>
      <tbody id="workers-table-body">
        <tr>
          <td colspan="8" style="text-align:center;color:var(--muted);">Loading workers...</td>
        </tr>
      </tbody>
    </table>
  </div>

  <script>
    async function refreshDashboard() {
      try {
        const [wRes, wrkRes] = await Promise.all([
          fetch('/dashboard/workload'),
          fetch('/dashboard/workers')
        ]);
        const wData = await wRes.json();
        const wrkData = await wrkRes.json();

        document.getElementById('val-queued').textContent = wData.queued_tasks;
        document.getElementById('val-running').textContent = wData.in_progress_tasks;
        document.getElementById('val-completed').textContent = wData.completed_tasks;
        document.getElementById('val-failed').textContent = wData.failed_tasks;
        const avgSec = (wData.avg_execution_ms / 1000).toFixed(1);
        document.getElementById('val-latency').textContent = avgSec + 's';
        document.getElementById('val-cost').textContent = '$' + wData.total_cost_usd.toFixed(2);

        const tbody = document.getElementById('workers-table-body');
        tbody.innerHTML = '';
        if (wrkData.workers.length === 0) {
          tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;">No workers.</td></tr>';
        } else {
          wrkData.workers.forEach(w => {
            const m = w.metrics || {};
            const tr = document.createElement('tr');
            tr.innerHTML = `
              <td><code>${w.worker_id}</code></td>
              <td><strong>${w.name}</strong></td>
              <td>${w.role}</td>
              <td><span class="status-badge status-${w.status}">${w.status}</span></td>
              <td>${w.active_tasks_count}</td>
              <td>${((m.success_rate || 0) * 100).toFixed(1)}%</td>
              <td>${(m.avg_quality_score || 0).toFixed(2)}</td>
              <td>$${(m.total_cost_usd || 0).toFixed(3)}</td>
            `;
            tbody.appendChild(tr);
          });
        }
        const nowStr = new Date().toLocaleTimeString();
        document.getElementById('last-updated').textContent = 'Updated: ' + nowStr;
      } catch (err) {
        console.error('Failed to refresh dashboard:', err);
      }
    }
    refreshDashboard();
    setInterval(refreshDashboard, 5000);
  </script>
</body>
</html>
"""


@router.get("/ui", response_class=HTMLResponse)
async def get_dashboard_ui() -> HTMLResponse:
    """Serve the single-page operator dashboard interface."""
    return HTMLResponse(content=DASHBOARD_HTML, status_code=200)
