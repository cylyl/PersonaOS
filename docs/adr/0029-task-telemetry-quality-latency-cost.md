# ADR 0029: Task Execution Telemetry — Quality, Latency, Retries, Tokens, and Costs

**Status:** Accepted
**Date:** 2026-10-24
**Deciders:** PersonaOS Architecture Team
**Consulted:** Workload, Reliability, Control Plane, FinOps, Observability
**Informed:** Operators, Runtime Adapters

---

## Context & Problem Statement

In PersonaOS, workers execute distributed tasks across heterogeneous runtimes and LLM providers.
To operate a reliable persistent workforce, operators and supervisory controllers need visibility into:
1. **Quality:** Objective task success rates, output completeness, and quality scores degraded by retries or error incidents.
2. **Latency:** Separate breakdown of queue scheduling delay (`created_at → started_at`) versus execution duration (`started_at → completed_at`).
3. **Retries:** Failure patterns, attempt counts, and recovery behavior per worker and task type.
4. **Token Consumption & Financial Cost:** Prompt tokens, completion tokens, model selection, and accurate USD spend calculation.

Furthermore, these telemetry data streams are direct foundational prerequisites for:
- Worker and Workload Dashboards (Vikunja #113).
- Empirical Estimation Calibration (Vikunja #119).
- Team Assignment and Model Selection Simulations (Vikunja #118).

All telemetry tracking must strictly adhere to the project constraint: **Zero database migrations; preserve PostgreSQL v0.1 schema as source of truth.**

---

## Decision

We will implement a unified `TelemetryService` in `src/personaos/telemetry/`:

### 1. Schema-Preserving Telemetry Storage
- Telemetry records are embedded directly into `tasks.result["telemetry"]` upon task completion or terminal failure.
- In addition, lifecycle events emitted to `task_events` (such as `task.completed` and `task.failed`) attach the telemetry payload.
- This provides durable, queryable telemetry without modifying PostgreSQL v0.1 table schemas.

### 2. Domain Data Structures (`TaskTelemetry`)
A strongly typed telemetry payload containing:
- `task_id`, `worker_id`, `node_id`, `model_name`.
- `status`: task lifecycle status (`completed`, `failed`, `review`).
- `quality_score`: normalized float `0.0 <= score <= 1.0`.
- `queue_wait_ms`: duration between `created_at` and `started_at` in milliseconds.
- `execution_duration_ms`: duration between `started_at` and `completed_at` in milliseconds.
- `total_latency_ms`: end-to-end duration from creation to completion.
- `retry_count`: total retry attempts recorded on the task.
- `input_tokens`, `output_tokens`, `total_tokens`: token counts.
- `cost_usd`: calculated monetary cost.

### 3. Model Pricing Registry (`ModelPricingRegistry`)
- Maintains per-million token pricing rates for major models (GPT-4o, Claude 3.5 Sonnet, Gemini 1.5 Pro, Flash, Haiku, Llama, and configurable local fallbacks).
- Deterministic `calculate_cost(model, input_tokens, output_tokens) -> float`.

### 4. Quality Evaluation Engine (`QualityEvaluator`)
- Evaluates quality based on terminal status:
  - Base: 1.0 for completed, 0.0 for failed.
  - Retry penalty: -0.1 per retry attempt (floored at 0.2 for completed work).
  - Validation adjustments for output payloads.

### 5. Aggregate Analytics (`TelemetryService`)
- Provides SQL-level and in-memory aggregation:
  - `aggregate_worker_metrics(worker_id, window_days)`: calculates throughput, success rate, p50/p95 latency, average cost, and token totals for a worker.
  - `aggregate_workload_metrics(window_days)`: calculates system-wide queue depth, wait times, error rates, and total expenditure.

---

## Consequences

### Positive
- Unified, auditable telemetry across all tasks and workers.
- Zero database migrations needed; leverages JSONB `tasks.result` and `task_events.payload`.
- Transparent financial tracking and cost attribution across workers and tasks.
- Seamlessly unlocks Dashboards (#113), Calibration (#119), and Simulation (#118).

### Negative / Trade-offs
- For historical tasks completed prior to telemetry instrumentation, fallback latency and cost estimates are derived from existing timestamp columns.
