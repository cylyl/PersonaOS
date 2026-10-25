# ADR 0030: Worker and Workload Dashboards

**Status:** Accepted
**Date:** 2026-10-24
**Deciders:** PersonaOS Architecture Team
**Consulted:** API, Control Plane, Telemetry, Observability, Operators
**Informed:** Runtime Adapters

---

## Context & Problem Statement

As PersonaOS scales to multiple persistent workers executing workloads across nodes, human operators and automated supervision layers require real-time visibility into workforce operations:
1. **Workload State:** Queue depths, throughput rates, priority distribution, latency percentiles, and retry frequencies.
2. **Worker Performance:** Individual worker success rates, quality scores, active task concurrency, resource consumption, and cumulative token spend.
3. **Task-Level Telemetry:** Drill-down inspection of execution traces, duration breakdowns (queue wait vs. execution), and cost attribution.
4. **Operator Experience:** A zero-dependency, self-contained dashboard UI and REST API embedded directly into the control plane.

Vikunja Roadmap item **#113 (Phase 3)** calls for worker and workload dashboards.

---

## Decision

We will implement dedicated dashboard routes and an embedded operator dashboard UI in `src/personaos/api/routes/dashboard.py`:

### 1. REST Dashboard Endpoints
Mounted under `/dashboard`:
- `GET /dashboard/workload`: Returns system-wide queue depth, status and priority breakdowns, latency statistics, total token usage, and cumulative cost. Supports optional `window_days` filtering.
- `GET /dashboard/workers`: Returns a workforce summary listing each registered worker, current state, active tasks, profile version, and aggregate performance metrics.
- `GET /dashboard/workers/{worker_id}`: Returns deep performance telemetry and recent execution records for a specific worker.
- `GET /dashboard/tasks/{task_id}/telemetry`: Returns detailed execution latency, token counts, cost, and quality evaluation for a task.
- `GET /dashboard/ui`: Serves an embedded, lightweight HTML/CSS/JavaScript single-page operator dashboard with auto-refreshing metrics and status visualization.

### 2. Integration with TelemetryService
- The dashboard routes leverage `TelemetryService` (ADR 0029) to query and aggregate metrics directly from PostgreSQL `tasks` and `task_events`.
- Queries execute read-only aggregation queries without altering schema or holding table locks.

### 3. Error Handling & Invariants
- Querying a non-existent worker or task returns HTTP 404.
- Inactive or newly created workers with zero completed tasks return valid zero-initialized aggregates rather than failing.

---

## Consequences

### Positive
- Centralized visibility into workforce health, queue bottlenecks, and LLM expenditures.
- Embedded UI requires zero external npm packages, node runtimes, or frontend build steps.
- Built upon `TelemetryService` (ADR 0029), ensuring consistency across API and internal evaluation.

### Negative / Trade-offs
- Aggregating historical tasks over large time windows without dedicated read replicas may increase query latency on high-throughput deployments (mitigated via index usage and `window_days` filters).
