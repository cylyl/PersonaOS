"""Empirical estimation calibration engine.

Per ADR 0031 and docs/specs/empirical-estimation-calibration.md:
Calibrates estimated task execution duration, token counts, and costs against
actual telemetry measurements across workers, models, and task types.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EstimationMetrics:
    """Estimated or actual resource metrics for a task."""

    duration_ms: float
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_ms": self.duration_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EstimationMetrics:
        return cls(
            duration_ms=float(data.get("duration_ms", 0.0)),
            input_tokens=int(data.get("input_tokens", 0)),
            output_tokens=int(data.get("output_tokens", 0)),
            cost_usd=float(data.get("cost_usd", 0.0)),
        )


@dataclass
class CalibrationSample:
    """Paired estimated and actual metrics for an executed task."""

    task_id: str
    task_type: str = "general"
    worker_id: str | None = None
    model_name: str | None = None
    estimated: EstimationMetrics = field(default_factory=lambda: EstimationMetrics(0.0))
    actual: EstimationMetrics = field(default_factory=lambda: EstimationMetrics(0.0))
    recorded_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def duration_ratio(self) -> float:
        est = max(1.0, self.estimated.duration_ms)
        return self.actual.duration_ms / est

    @property
    def token_ratio(self) -> float:
        est = max(1, self.estimated.total_tokens)
        return self.actual.total_tokens / est

    @property
    def cost_ratio(self) -> float:
        est = max(0.0001, self.estimated.cost_usd)
        return self.actual.cost_usd / est

    @property
    def duration_error_pct(self) -> float:
        est = max(1.0, self.estimated.duration_ms)
        return abs(self.actual.duration_ms - self.estimated.duration_ms) / est

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "worker_id": self.worker_id,
            "model_name": self.model_name,
            "estimated": self.estimated.to_dict(),
            "actual": self.actual.to_dict(),
            "recorded_at": self.recorded_at.isoformat(),
        }


@dataclass
class CalibrationProfile:
    """Segment-specific calibration factors and historical error statistics."""

    key: str
    sample_count: int = 0
    duration_multiplier: float = 1.0
    token_multiplier: float = 1.0
    cost_multiplier: float = 1.0
    mape_duration: float = 0.0
    last_updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def update(
        self,
        sample: CalibrationSample,
        alpha: float = 0.25,
        min_mult: float = 0.2,
        max_mult: float = 5.0,
    ) -> None:
        """Update multipliers using Exponential Moving Average."""
        d_ratio = min(max_mult, max(min_mult, sample.duration_ratio))
        t_ratio = min(max_mult, max(min_mult, sample.token_ratio))
        c_ratio = min(max_mult, max(min_mult, sample.cost_ratio))

        if self.sample_count == 0:
            self.duration_multiplier = d_ratio
            self.token_multiplier = t_ratio
            self.cost_multiplier = c_ratio
            self.mape_duration = sample.duration_error_pct
        else:
            self.duration_multiplier = (1.0 - alpha) * self.duration_multiplier + alpha * d_ratio
            self.token_multiplier = (1.0 - alpha) * self.token_multiplier + alpha * t_ratio
            self.cost_multiplier = (1.0 - alpha) * self.cost_multiplier + alpha * c_ratio
            self.mape_duration = (
                1.0 - alpha
            ) * self.mape_duration + alpha * sample.duration_error_pct

        self.sample_count += 1
        self.last_updated_at = datetime.now(UTC)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "sample_count": self.sample_count,
            "duration_multiplier": round(self.duration_multiplier, 4),
            "token_multiplier": round(self.token_multiplier, 4),
            "cost_multiplier": round(self.cost_multiplier, 4),
            "mape_duration": round(self.mape_duration, 4),
            "last_updated_at": self.last_updated_at.isoformat(),
        }


@dataclass(frozen=True)
class CalibratedEstimate:
    """Adjusted estimate resulting from empirical calibration."""

    baseline: EstimationMetrics
    calibrated: EstimationMetrics
    duration_multiplier: float
    token_multiplier: float
    cost_multiplier: float
    confidence_score: float
    applied_profiles: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline.to_dict(),
            "calibrated": self.calibrated.to_dict(),
            "duration_multiplier": round(self.duration_multiplier, 4),
            "token_multiplier": round(self.token_multiplier, 4),
            "cost_multiplier": round(self.cost_multiplier, 4),
            "confidence_score": round(self.confidence_score, 4),
            "applied_profiles": list(self.applied_profiles),
        }


class EstimationCalibrationEngine:
    """Engine for tracking actual-vs-estimated execution and calibrating future estimates."""

    def __init__(
        self,
        alpha: float = 0.25,
        min_multiplier: float = 0.2,
        max_multiplier: float = 5.0,
    ) -> None:
        self.alpha = alpha
        self.min_multiplier = min_multiplier
        self.max_multiplier = max_multiplier
        self._profiles: dict[str, CalibrationProfile] = {}
        self._samples: list[CalibrationSample] = []
        self._lock = threading.RLock()

    def record_sample(
        self,
        task_id: str,
        task_type: str,
        worker_id: str | None,
        model_name: str | None,
        estimated: EstimationMetrics,
        actual: EstimationMetrics,
    ) -> CalibrationSample:
        """Record paired actual-vs-estimated execution metrics and update calibration profiles."""
        sample = CalibrationSample(
            task_id=task_id,
            task_type=task_type.strip().lower() if task_type else "general",
            worker_id=worker_id,
            model_name=model_name,
            estimated=estimated,
            actual=actual,
        )

        with self._lock:
            self._samples.append(sample)
            # Update segment profiles:
            # 1. Global profile
            self._update_profile("global", sample)

            # 2. Task type profile
            if sample.task_type:
                self._update_profile(f"type:{sample.task_type}", sample)

            # 3. Worker profile
            if sample.worker_id:
                self._update_profile(f"worker:{sample.worker_id}", sample)

            # 4. Model profile
            if sample.model_name:
                self._update_profile(f"model:{sample.model_name.lower()}", sample)

            # 5. Combined worker + type profile
            if sample.worker_id and sample.task_type:
                self._update_profile(f"worker:{sample.worker_id}:type:{sample.task_type}", sample)

        logger.info(
            "Recorded calibration sample for task %s "
            "(dur_ratio=%.2f, tok_ratio=%.2f, cost_ratio=%.2f)",
            task_id,
            sample.duration_ratio,
            sample.token_ratio,
            sample.cost_ratio,
        )
        return sample

    def calibrate(
        self,
        baseline: EstimationMetrics,
        task_type: str = "general",
        worker_id: str | None = None,
        model_name: str | None = None,
    ) -> CalibratedEstimate:
        """Produce a calibrated estimate based on learned empirical multipliers."""
        clean_type = task_type.strip().lower() if task_type else "general"

        with self._lock:
            # Select candidate profile in order of specificity
            candidate_keys = []
            if worker_id and clean_type:
                candidate_keys.append(f"worker:{worker_id}:type:{clean_type}")
            if worker_id:
                candidate_keys.append(f"worker:{worker_id}")
            if clean_type:
                candidate_keys.append(f"type:{clean_type}")
            if model_name:
                candidate_keys.append(f"model:{model_name.lower()}")
            candidate_keys.append("global")

            dur_mults: list[float] = []
            tok_mults: list[float] = []
            cost_mults: list[float] = []
            applied: list[str] = []
            total_samples = 0

            for k in candidate_keys:
                prof = self._profiles.get(k)
                if prof and prof.sample_count > 0:
                    dur_mults.append(prof.duration_multiplier)
                    tok_mults.append(prof.token_multiplier)
                    cost_mults.append(prof.cost_multiplier)
                    applied.append(k)
                    total_samples += prof.sample_count
                    # If high specificity profile found with at least 3 samples, stop traversal
                    if prof.sample_count >= 3:
                        break

            if dur_mults:
                final_dur_mult = sum(dur_mults) / len(dur_mults)
                final_tok_mult = sum(tok_mults) / len(tok_mults)
                final_cost_mult = sum(cost_mults) / len(cost_mults)
            else:
                final_dur_mult = 1.0
                final_tok_mult = 1.0
                final_cost_mult = 1.0

            # Confidence increases with sample volume (asymptotic to 1.0)
            confidence = min(0.99, round(1.0 - (1.0 / (1.0 + 0.2 * total_samples)), 4))

            calibrated = EstimationMetrics(
                duration_ms=round(max(0.0, baseline.duration_ms * final_dur_mult), 2),
                input_tokens=int(round(max(0, baseline.input_tokens * final_tok_mult))),
                output_tokens=int(round(max(0, baseline.output_tokens * final_tok_mult))),
                cost_usd=round(max(0.0, baseline.cost_usd * final_cost_mult), 6),
            )

            return CalibratedEstimate(
                baseline=baseline,
                calibrated=calibrated,
                duration_multiplier=round(final_dur_mult, 4),
                token_multiplier=round(final_tok_mult, 4),
                cost_multiplier=round(final_cost_mult, 4),
                confidence_score=confidence,
                applied_profiles=applied,
            )

    def get_profile(self, key: str) -> CalibrationProfile | None:
        with self._lock:
            return self._profiles.get(key)

    def list_profiles(self) -> list[CalibrationProfile]:
        with self._lock:
            return list(self._profiles.values())

    def _update_profile(self, key: str, sample: CalibrationSample) -> None:
        prof = self._profiles.setdefault(key, CalibrationProfile(key=key))
        prof.update(
            sample=sample,
            alpha=self.alpha,
            min_mult=self.min_multiplier,
            max_mult=self.max_multiplier,
        )
