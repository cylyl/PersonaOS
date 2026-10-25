"""Unit tests for Team Assignment and Model Comparison Simulation (Vikunja #118, ADR 0032)."""

from personaos.domain.task import Task
from personaos.simulation.calibration import EstimationCalibrationEngine, EstimationMetrics
from personaos.simulation.team_comparison import (
    AssignmentScenario,
    OptimizationGoal,
    TeamComparisonSimulator,
)
from personaos.telemetry.pricing import ModelPricingRegistry


def test_empty_scenarios() -> None:
    simulator = TeamComparisonSimulator()
    task = Task(
        id="task-1",
        title="Test Task",
        objective="Do work",
        input={},
        profile_version=1,
        status="queued",
        priority="medium",
        required_skills=["python"],
    )

    report = simulator.evaluate_scenarios(task=task, scenarios=[])
    assert report.task_id == "task-1"
    assert len(report.evaluations) == 0
    assert report.recommended_scenario_id is None


def test_skill_match_and_penalty() -> None:
    simulator = TeamComparisonSimulator()
    task = Task(
        id="task-1",
        title="Deploy to K8s",
        objective="Deploy app",
        input={},
        profile_version=1,
        status="queued",
        priority="high",
        required_skills=["kubernetes", "helm"],
    )

    # Worker 1: Has both skills
    s1 = AssignmentScenario(
        scenario_id="s-qualified",
        worker_id="devops-lead",
        model_name="gpt-4o",
        worker_skills=["kubernetes", "helm", "docker"],
    )

    # Worker 2: Missing helm
    s2 = AssignmentScenario(
        scenario_id="s-unqualified",
        worker_id="junior-dev",
        model_name="gpt-4o",
        worker_skills=["kubernetes"],
    )

    report = simulator.evaluate_scenarios(task=task, scenarios=[s1, s2])
    assert len(report.evaluations) == 2

    # s1 should be ranked #1
    assert report.recommended_scenario_id == "s-qualified"
    ev1 = next(e for e in report.evaluations if e.scenario.scenario_id == "s-qualified")
    ev2 = next(e for e in report.evaluations if e.scenario.scenario_id == "s-unqualified")

    assert ev1.skill_fit is True
    assert len(ev1.missing_skills) == 0

    assert ev2.skill_fit is False
    assert ev2.missing_skills == ["helm"]
    assert ev1.composite_score > ev2.composite_score


def test_cost_vs_quality_optimization_goals() -> None:
    pricing = ModelPricingRegistry()
    simulator = TeamComparisonSimulator(pricing_registry=pricing)

    task = Task(
        id="t-analysis",
        title="Data Analysis",
        objective="Analyze numbers",
        input={},
        profile_version=1,
        status="queued",
        priority="medium",
        required_skills=["python"],
    )

    # Expensive high-capability model
    s_flagship = AssignmentScenario(
        scenario_id="scenario-flagship",
        worker_id="analyst",
        model_name="claude-3-5-sonnet",
        worker_skills=["python"],
        historical_quality=1.0,
    )

    # Cheap fast model
    s_mini = AssignmentScenario(
        scenario_id="scenario-mini",
        worker_id="analyst",
        model_name="gpt-4o-mini",
        worker_skills=["python"],
        historical_quality=0.85,
    )

    # 1. Under COST_MIN, mini model should win
    report_cost = simulator.evaluate_scenarios(
        task=task,
        scenarios=[s_flagship, s_mini],
        goal=OptimizationGoal.COST_MIN,
    )
    assert report_cost.recommended_scenario_id == "scenario-mini"

    # 2. Under QUALITY_MAX, flagship model should win
    report_quality = simulator.evaluate_scenarios(
        task=task,
        scenarios=[s_flagship, s_mini],
        goal=OptimizationGoal.QUALITY_MAX,
    )
    assert report_quality.recommended_scenario_id == "scenario-flagship"


def test_calibrated_worker_duration_in_speed_optimization() -> None:
    calibration = EstimationCalibrationEngine(alpha=0.5)
    # Record that worker 'fast-worker' is 2x faster (0.5x duration)
    baseline = EstimationMetrics(
        duration_ms=20_000.0, input_tokens=1000, output_tokens=200, cost_usd=0.01
    )
    actual_fast = EstimationMetrics(
        duration_ms=10_000.0, input_tokens=1000, output_tokens=200, cost_usd=0.01
    )

    for i in range(3):
        calibration.record_sample(
            task_id=f"sample-{i}",
            task_type="backend",
            worker_id="fast-worker",
            model_name="gpt-4o",
            estimated=baseline,
            actual=actual_fast,
        )
        calibration.record_sample(
            task_id=f"std-sample-{i}",
            task_type="backend",
            worker_id="standard-worker",
            model_name="gpt-4o",
            estimated=baseline,
            actual=baseline,
        )

    simulator = TeamComparisonSimulator(calibration_engine=calibration)
    task = Task(
        id="t-speed",
        title="Quick Fix",
        objective="Fix bug",
        type="backend",
        input={},
        profile_version=1,
        status="queued",
        priority="critical",
    )

    s_fast = AssignmentScenario(
        scenario_id="s-fast",
        worker_id="fast-worker",
        model_name="gpt-4o",
    )
    s_standard = AssignmentScenario(
        scenario_id="s-standard",
        worker_id="standard-worker",
        model_name="gpt-4o",
    )

    report = simulator.evaluate_scenarios(
        task=task,
        scenarios=[s_fast, s_standard],
        baseline=baseline,
        goal=OptimizationGoal.LATENCY_MIN,
    )

    assert report.recommended_scenario_id == "s-fast"
    ev_fast = next(e for e in report.evaluations if e.scenario.scenario_id == "s-fast")
    ev_std = next(e for e in report.evaluations if e.scenario.scenario_id == "s-standard")
    assert (
        ev_fast.calibrated_estimate.calibrated.duration_ms
        < ev_std.calibrated_estimate.calibrated.duration_ms
    )


def test_report_serialization() -> None:
    simulator = TeamComparisonSimulator()
    task = Task(
        id="t-serial",
        title="Check Serialization",
        objective="Verify dict",
        input={},
        profile_version=1,
        status="queued",
        priority="low",
    )
    scenario = AssignmentScenario(
        scenario_id="s-1",
        worker_id="worker-1",
        model_name="gpt-4o",
    )

    report = simulator.evaluate_scenarios(task=task, scenarios=[scenario])
    data = report.to_dict()

    assert data["task_id"] == "t-serial"
    assert data["recommended_scenario_id"] == "s-1"
    assert len(data["evaluations"]) == 1
    assert data["evaluations"][0]["composite_score"] > 0.0
    assert "reasoning" in data["evaluations"][0]
