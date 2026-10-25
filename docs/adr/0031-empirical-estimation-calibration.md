# ADR 0031: Empirical Estimation Calibration Engine

**Status:** Accepted
**Date:** 2026-10-24
**Deciders:** PersonaOS Architecture Team
**Consulted:** Simulation, Workload Execution, Control Plane, Telemetry
**Informed:** Operators, Runtime Adapters

---

## Context & Problem Statement

Workload scheduling, resource allocation, and counterfactual simulation require accurate estimates of task duration, token consumption, and financial costs.
Initially, static or rule-of-thumb heuristics are used (e.g. assuming a task will take 30 seconds and 2,000 tokens). However:
1. Different workers possess varying efficiency, skills, and memory scopes.
2. Different LLMs display distinct completion speeds, token verbosity, and pricing tiers.
3. Uncalibrated static estimates accumulate systematic drift, distorting capacity planning and budget projections.

Vikunja Roadmap item **#119 (Phase 4)** calls for calibrating estimates against actual execution.

---

## Decision

We will implement an empirical `EstimationCalibrationEngine` in `src/personaos/simulation/calibration.py`:

### 1. Unified Estimation vs. Actual Metrics
For every task execution, we measure:
- `estimated`: Baseline duration, input tokens, output tokens, cost.
- `actual`: Realized telemetry (recorded via `TelemetryService` per ADR 0029).
- `CalibrationSample`: Captures the paired tuple with ratios (`actual / estimated`) and absolute percentage error.

### 2. Segmented Multipliers & Moving Average
Calibration profiles are segmented hierarchically by:
- `worker_id` (worker-specific efficiency).
- `task_type` (domain/task complexity).
- `model_name` (model latency and verbosity characteristics).

When new actual telemetry arrives, multipliers are updated using Exponential Moving Average (EMA):
```
multiplier_new = (1 - alpha) * multiplier_prev + alpha * (actual / estimated)
```
- Multipliers are clamped between safety boundaries (`0.2` and `5.0`) to resist aberrant outlier skew.
- Mean Absolute Percentage Error (MAPE) is continuously tracked to measure calibration confidence.

### 3. Predictive Estimation
`calibrate_estimate(baseline, task_type, worker_id, model)` applies the segmented calibration multipliers to produce a `CalibratedEstimate`, including:
- Adjusted execution duration, token counts, and monetary cost.
- Upper/lower confidence bounds based on historical error variance.
- Explanatory attribution metadata detailing which calibration profiles influenced the adjustment.

---

## Consequences

### Positive
- Closes the feedback loop between execution telemetry (#112) and simulation (#118).
- Transforms static guess-work into adaptive, data-driven workforce planning.
- Versioned, auditable calibration factors with traceable sample history.

### Negative / Trade-offs
- Cold-start: until sufficient samples are recorded, fallback multipliers (1.0) are utilized.
