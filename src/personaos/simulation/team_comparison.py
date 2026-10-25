"""Team comparison simulator for evaluating alternative assignments and model choices.

Per ADR 0032 and docs/specs/team-assignment-and-model-comparison-simulation.md:
Simulates and ranks alternative worker and model configurations for a task.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from personaos.db.models import Task as TaskModel
from personaos.domain.task import Task
from personaos.simulation.calibration import (
    CalibratedEstimate,
    EstimationCalibrationEngine,
    EstimationMetrics,
)
from personaos.telemetry.pricing import ModelPricingRegistry

logger = logging.getLogger(__name__)

# Relative model capability weights for quality forecasting
MODEL_CAPABILITY_TIERS: dict[str, float] = {
    "claude-3-5-sonnet": 1.00,
    "gpt-4o": 0.98,
    "o1": 1.00,
    "gemini-1.5-pro": 0.95,
    "claude-3-opus": 0.94,
    "llama-3.1-70b": 0.88,
    "claude-3-5-haiku": 0.85,
    "gpt-4o-mini": 0.82,
    "gemini-1.5-flash": 0.80,
    "llama-3.1-8b": 0.72,
}
DEFAULT_MODEL_CAPABILITY = 0.75


class OptimizationGoal(StrEnum):
    """Optimization objective for ranking candidate scenarios."""

    BALANCED = "balanced"
    COST_MIN = "cost_min"
    LATENCY_MIN = "latency_min"
    QUALITY_MAX = "quality_max"


@dataclass
class AssignmentScenario:
    """Hypothetical assignment candidate specifying worker and model."""

    scenario_id: str
    worker_id: str
    model_name: str
    worker_skills: list[str] = field(default_factory=list)
    historical_quality: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "worker_id": self.worker_id,
            "model_name": self.model_name,
            "worker_skills": list(self.worker_skills),
            "historical_quality": self.historical_quality,
            "metadata": dict(self.metadata),
        }


@dataclass
class ScenarioEvaluation:
    """Evaluation result for an individual assignment scenario."""

    scenario: AssignmentScenario
    skill_fit: bool
    missing_skills: list[str]
    calibrated_estimate: CalibratedEstimate
    predicted_quality: float
    composite_score: float
    rank: int = 1
    reasoning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario.to_dict(),
            "skill_fit": self.skill_fit,
            "missing_skills": list(self.missing_skills),
            "calibrated_estimate": self.calibrated_estimate.to_dict(),
            "predicted_quality": round(self.predicted_quality, 4),
            "composite_score": round(self.composite_score, 4),
            "rank": self.rank,
            "reasoning": self.reasoning,
        }


@dataclass
class TeamComparisonReport:
    """Comprehensive comparative simulation report across scenarios."""

    task_id: str
    task_title: str
    required_skills: list[str]
    goal: OptimizationGoal
    evaluations: list[ScenarioEvaluation]
    recommended_scenario_id: str | None = None
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_title": self.task_title,
            "required_skills": list(self.required_skills),
            "goal": self.goal.value,
            "recommended_scenario_id": self.recommended_scenario_id,
            "evaluations": [e.to_dict() for e in self.evaluations],
            "generated_at": self.generated_at.isoformat(),
        }


class TeamComparisonSimulator:
    """Simulates and compares alternative team assignments and model choices."""

    def __init__(
        self,
        calibration_engine: EstimationCalibrationEngine | None = None,
        pricing_registry: ModelPricingRegistry | None = None,
    ) -> None:
        self.calibration = calibration_engine or EstimationCalibrationEngine()
        self.pricing = pricing_registry or ModelPricingRegistry()

    def evaluate_scenarios(
        self,
        task: Task | TaskModel,
        scenarios: list[AssignmentScenario],
        baseline: EstimationMetrics | None = None,
        goal: OptimizationGoal = OptimizationGoal.BALANCED,
    ) -> TeamComparisonReport:
        """Simulate and rank candidate worker-model assignments for a given task."""
        if not scenarios:
            return TeamComparisonReport(
                task_id=task.id,
                task_title=getattr(task, "title", "Task"),
                required_skills=list(getattr(task, "required_skills", []) or []),
                goal=goal,
                evaluations=[],
            )

        task_id = task.id
        task_title = getattr(task, "title", "Task")
        req_skills = [s.strip().lower() for s in (getattr(task, "required_skills", []) or [])]
        task_type = getattr(task, "type", "general") or "general"

        # Default baseline: 30s, 2000 in, 500 out
        base_metrics = baseline or EstimationMetrics(
            duration_ms=30_000.0,
            input_tokens=2_000,
            output_tokens=500,
            cost_usd=0.01,
        )

        evaluations: list[ScenarioEvaluation] = []

        for scenario in scenarios:
            # 1. Skill Fit
            worker_skills_norm = {s.strip().lower() for s in scenario.worker_skills}
            missing = [s for s in req_skills if s not in worker_skills_norm]
            skill_fit = len(missing) == 0

            # 2. Calibrated Estimate
            cal = self.calibration.calibrate(
                baseline=base_metrics,
                task_type=task_type,
                worker_id=scenario.worker_id,
                model_name=scenario.model_name,
            )

            # Recompute calibrated cost accurately for specific model choice
            recomputed_cost = self.pricing.calculate_cost(
                model_name=scenario.model_name,
                input_tokens=cal.calibrated.input_tokens,
                output_tokens=cal.calibrated.output_tokens,
            )
            calibrated_with_cost = CalibratedEstimate(
                baseline=cal.baseline,
                calibrated=EstimationMetrics(
                    duration_ms=cal.calibrated.duration_ms,
                    input_tokens=cal.calibrated.input_tokens,
                    output_tokens=cal.calibrated.output_tokens,
                    cost_usd=recomputed_cost,
                ),
                duration_multiplier=cal.duration_multiplier,
                token_multiplier=cal.token_multiplier,
                cost_multiplier=round(recomputed_cost / max(0.0001, base_metrics.cost_usd), 4),
                confidence_score=cal.confidence_score,
                applied_profiles=cal.applied_profiles,
            )

            # 3. Predicted Quality
            model_tier = self._get_model_capability(scenario.model_name)
            skill_coverage = (
                (len(req_skills) - len(missing)) / len(req_skills) if req_skills else 1.0
            )
            # Weighted quality formula: 45% model, 35% worker history, 20% skill coverage
            quality = 0.45 * model_tier + 0.35 * scenario.historical_quality + 0.20 * skill_coverage
            if not skill_fit:
                # Skill deficit penalty
                quality = max(0.1, quality * 0.6)

            # 4. Composite Scoring
            score, reasoning = self._compute_score(
                goal=goal,
                quality=quality,
                duration_ms=calibrated_with_cost.calibrated.duration_ms,
                cost_usd=recomputed_cost,
                skill_fit=skill_fit,
            )

            evaluations.append(
                ScenarioEvaluation(
                    scenario=scenario,
                    skill_fit=skill_fit,
                    missing_skills=missing,
                    calibrated_estimate=calibrated_with_cost,
                    predicted_quality=quality,
                    composite_score=score,
                    reasoning=reasoning,
                )
            )

        # Sort by composite score descending
        evaluations.sort(key=lambda e: e.composite_score, reverse=True)
        for idx, ev in enumerate(evaluations, start=1):
            ev.rank = idx

        recommended_id = evaluations[0].scenario.scenario_id if evaluations else None

        return TeamComparisonReport(
            task_id=task_id,
            task_title=task_title,
            required_skills=req_skills,
            goal=goal,
            evaluations=evaluations,
            recommended_scenario_id=recommended_id,
        )

    def _get_model_capability(self, model_name: str) -> float:
        clean = model_name.strip().lower()
        if clean in MODEL_CAPABILITY_TIERS:
            return MODEL_CAPABILITY_TIERS[clean]
        for prefix, weight in MODEL_CAPABILITY_TIERS.items():
            if clean.startswith(prefix):
                return weight
        return DEFAULT_MODEL_CAPABILITY

    def _compute_score(
        self,
        goal: OptimizationGoal,
        quality: float,
        duration_ms: float,
        cost_usd: float,
        skill_fit: bool,
    ) -> tuple[float, str]:
        # Latency normalized (10s benchmark): ranges 0.0 to 1.0
        norm_speed = 10_000.0 / (10_000.0 + max(0.0, duration_ms))
        # Cost normalized ($0.05 benchmark): ranges 0.0 to 1.0
        norm_cost = 0.05 / (0.05 + max(0.0, cost_usd))
        norm_quality = min(1.0, max(0.0, quality))

        if goal == OptimizationGoal.COST_MIN:
            score = 0.70 * norm_cost + 0.20 * norm_quality + 0.10 * norm_speed
            profile = "cost-optimized"
        elif goal == OptimizationGoal.LATENCY_MIN:
            score = 0.70 * norm_speed + 0.20 * norm_quality + 0.10 * norm_cost
            profile = "speed-optimized"
        elif goal == OptimizationGoal.QUALITY_MAX:
            score = 0.80 * norm_quality + 0.10 * norm_speed + 0.10 * norm_cost
            profile = "quality-optimized"
        else:  # BALANCED
            score = 0.40 * norm_quality + 0.30 * norm_speed + 0.30 * norm_cost
            profile = "balanced"

        if not skill_fit:
            score *= 0.5
            reason = f"Penalized due to missing required skills ({profile})."
        else:
            reason = (
                f"Full skill match. {profile.capitalize()} composite score "
                f"(quality={norm_quality:.2f}, speed={norm_speed:.2f}, cost={norm_cost:.2f})."
            )

        return round(score, 4), reason
