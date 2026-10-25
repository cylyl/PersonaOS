# ADR 0032: Comparative Simulation for Team Assignments and Model Choices

**Status:** Accepted
**Date:** 2026-10-24
**Deciders:** PersonaOS Architecture Team
**Consulted:** Simulation, Workload Execution, Control Plane, FinOps, Schedulers
**Informed:** Operators, Runtime Adapters

---

## Context & Problem Statement

Workforce managers and autonomous orchestrators frequently face assignment decisions:
- Which worker has the optimal combination of skills and past performance for a task?
- Should a task be executed with a high-capability frontier model (e.g. `claude-3-5-sonnet` or `gpt-4o`) or an efficient, lower-cost model (e.g. `gpt-4o-mini` or `gemini-1.5-flash`)?
- What are the concrete trade-offs in execution latency, token consumption, quality, and cost across alternative staffing options?

Without comparative simulation, assignment decisions are either rigid hardcodings or uninformed guesses.

Vikunja Roadmap item **#118 (Phase 4)** calls for comparing alternative team assignments and model choices.

---

## Decision

We will implement a multi-scenario comparative simulation service in `src/personaos/simulation/team_comparison.py`:

### 1. Scenario Formulation (`AssignmentScenario`)
Each scenario specifies an alternative assignment hypothesis:
- `scenario_id`: Unique identifier (e.g. `"worker-alice:claude-3-5-sonnet"`).
- `worker_id`: Candidate worker identity.
- `model_name`: Candidate LLM model.
- `worker_skills`: Set of skills possessed by the worker.
- `worker_historical_quality`: Average quality score from telemetry (default 1.0).

### 2. Multi-Dimensional Evaluation Engine
For each candidate scenario evaluated against a target task:
1. **Skill & Capability Matching:**
   Validates whether `task.required_skills` is a subset of `scenario.worker_skills`. Incomplete skill coverage incurs severe quality and viability penalties.
2. **Empirical Calibration:**
   Leverages `EstimationCalibrationEngine` (ADR 0031) to project execution duration and token volume calibrated to the specific `(worker, task_type, model)` combination.
3. **Financial Pricing:**
   Calculates expected USD spend using `ModelPricingRegistry` (ADR 0029) for the candidate model.
4. **Quality Forecasting:**
   Calculates predicted quality from worker competence history, model capability tier, and skill match completeness.
5. **Optimization Scoring:**
   Scores and ranks scenarios based on operator objective:
   - `OptimizationGoal.BALANCED`: 40% quality, 30% speed, 30% cost efficiency.
   - `OptimizationGoal.COST_MIN`: 70% cost efficiency, 20% quality, 10% speed.
   - `OptimizationGoal.LATENCY_MIN`: 70% speed, 20% quality, 10% cost efficiency.
   - `OptimizationGoal.QUALITY_MAX`: 80% quality, 10% speed, 10% cost efficiency.

### 3. Structured Simulation Output (`TeamComparisonReport`)
Produces an auditable report:
- `task_id`, `objective`, `required_skills`.
- Evaluated scenarios sorted by score descending.
- Recommended scenario.
- Full comparative matrix (cost, latency, quality, skill fit).

---

## Consequences

### Positive
- Enables evidence-based team formation and model routing.
- Eliminates guesswork by grounding simulations in calibrated historical telemetry.
- Strictly read-only and non-destructive: does not modify task state or trigger external side effects.

### Negative / Trade-offs
- Accuracy depends on the calibration engine's sample volume; cold-start fallbacks apply reasonable baseline assumptions.
