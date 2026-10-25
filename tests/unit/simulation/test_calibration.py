"""Unit tests for Empirical Estimation Calibration Engine (Vikunja #119, ADR 0031)."""

from personaos.simulation.calibration import (
    CalibrationProfile,
    CalibrationSample,
    EstimationCalibrationEngine,
    EstimationMetrics,
)


def test_estimation_metrics_model() -> None:
    est = EstimationMetrics(
        duration_ms=5000.0,
        input_tokens=1000,
        output_tokens=250,
        cost_usd=0.015,
    )
    assert est.total_tokens == 1250
    data = est.to_dict()
    assert data["duration_ms"] == 5000.0
    assert data["total_tokens"] == 1250

    rebuilt = EstimationMetrics.from_dict(data)
    assert rebuilt.duration_ms == 5000.0
    assert rebuilt.total_tokens == 1250


def test_calibration_sample_ratios() -> None:
    est = EstimationMetrics(duration_ms=4000.0, input_tokens=1000, output_tokens=500, cost_usd=0.01)
    act = EstimationMetrics(
        duration_ms=6000.0, input_tokens=1500, output_tokens=750, cost_usd=0.015
    )

    sample = CalibrationSample(
        task_id="task-1",
        task_type="code-review",
        worker_id="dev-1",
        model_name="gpt-4o",
        estimated=est,
        actual=act,
    )

    assert sample.duration_ratio == 1.5  # 6000 / 4000
    assert sample.token_ratio == 1.5  # 2250 / 1500
    assert sample.cost_ratio == 1.5  # 0.015 / 0.01
    assert sample.duration_error_pct == 0.5


def test_calibration_profile_ema_and_bounds() -> None:
    profile = CalibrationProfile(key="test-key")
    est = EstimationMetrics(duration_ms=1000.0, input_tokens=100, output_tokens=100, cost_usd=0.01)

    # First sample: 2x actual
    act1 = EstimationMetrics(duration_ms=2000.0, input_tokens=200, output_tokens=200, cost_usd=0.02)
    s1 = CalibrationSample(task_id="t1", estimated=est, actual=act1)
    profile.update(s1, alpha=0.5)

    assert profile.sample_count == 1
    assert profile.duration_multiplier == 2.0

    # Second sample: 1x actual -> EMA with alpha=0.5: (0.5 * 2.0) + (0.5 * 1.0) = 1.5
    act2 = EstimationMetrics(duration_ms=1000.0, input_tokens=100, output_tokens=100, cost_usd=0.01)
    s2 = CalibrationSample(task_id="t2", estimated=est, actual=act2)
    profile.update(s2, alpha=0.5)

    assert profile.sample_count == 2
    assert profile.duration_multiplier == 1.5

    # Outlier sample: 100x actual -> bounded to max_mult (5.0)
    act_outlier = EstimationMetrics(
        duration_ms=100_000.0, input_tokens=100, output_tokens=100, cost_usd=0.01
    )
    s_out = CalibrationSample(task_id="t3", estimated=est, actual=act_outlier)
    profile.update(s_out, alpha=0.5, max_mult=5.0)

    # EMA with clamped ratio 5.0: (0.5 * 1.5) + (0.5 * 5.0) = 3.25
    assert profile.duration_multiplier == 3.25


def test_engine_calibration_flow() -> None:
    engine = EstimationCalibrationEngine(alpha=0.5)

    # Baseline estimate
    baseline = EstimationMetrics(
        duration_ms=10_000.0,
        input_tokens=2_000,
        output_tokens=500,
        cost_usd=0.05,
    )

    # Before any samples, calibration returns 1.0x baseline
    initial_cal = engine.calibrate(baseline, task_type="build", worker_id="builder-1")
    assert initial_cal.duration_multiplier == 1.0
    assert initial_cal.calibrated.duration_ms == 10_000.0
    assert initial_cal.confidence_score == 0.0

    # Record 3 actual samples where worker builder-1 takes 1.5x duration
    for i in range(3):
        est = EstimationMetrics(
            duration_ms=10_000.0, input_tokens=2_000, output_tokens=500, cost_usd=0.05
        )
        act = EstimationMetrics(
            duration_ms=15_000.0, input_tokens=2_500, output_tokens=600, cost_usd=0.065
        )
        engine.record_sample(
            task_id=f"sim-{i}",
            task_type="build",
            worker_id="builder-1",
            model_name="gpt-4o",
            estimated=est,
            actual=act,
        )

    # Calibrate future task for builder-1 on build task
    calibrated = engine.calibrate(baseline, task_type="build", worker_id="builder-1")
    assert calibrated.duration_multiplier == 1.5
    assert calibrated.calibrated.duration_ms == 15_000.0
    assert calibrated.confidence_score > 0.0
    assert "worker:builder-1:type:build" in calibrated.applied_profiles
