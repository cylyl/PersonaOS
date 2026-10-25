# Specification: Worker and Workload Dashboards

**Reference:** ADR 0030, Vikunja #113 (Phase 3)
**Module:** `personaos.api.routes.dashboard`

---

## 1. API Endpoints

### 1.1 `GET /dashboard/workload`
- **Query Parameters:**
  - `window_days` (optional `int`): Limit aggregation to the past N days.
- **Response `200 OK`:**
  - `total_tasks: int`
  - `queued_tasks: int`
  - `in_progress_tasks: int`
  - `completed_tasks: int`
  - `failed_tasks: int`
  - `review_tasks: int`
  - `avg_queue_wait_ms: float`
  - `avg_execution_ms: float`
  - `retry_rate: float`
  - `total_tokens: int`
  - `total_cost_usd: float`
  - `status_breakdown: dict[str, int]`
  - `priority_breakdown: dict[str, int]`

### 1.2 `GET /dashboard/workers`
- **Query Parameters:**
  - `window_days` (optional `int`)
- **Response `200 OK`:**
  - List of worker summaries:
    - `worker_id: str`
    - `name: str`
    - `role: str`
    - `status: str`
    - `current_profile_version: int`
    - `active_tasks_count: int`
    - `metrics: WorkerMetricsAggregate`

### 1.3 `GET /dashboard/workers/{worker_id}`
- **Path Parameters:**
  - `worker_id: str`
- **Response `200 OK`:**
  - Full worker profile and aggregate metrics.
- **Response `404 Not Found`:** If worker does not exist.

### 1.4 `GET /dashboard/tasks/{task_id}/telemetry`
- **Path Parameters:**
  - `task_id: str`
- **Response `200 OK`:**
  - `TaskTelemetry` object.
- **Response `404 Not Found`:** If task does not exist.

### 1.5 `GET /dashboard/ui`
- **Response `200 OK` (`text/html`):**
  - Standalone HTML operator console.
