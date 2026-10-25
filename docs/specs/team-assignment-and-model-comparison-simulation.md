# Specification: Comparative Simulation for Team Assignments and Model Choices

**Reference:** ADR 0032, Vikunja #118 (Phase 4)
**Module:** `personaos.simulation.team_comparison`

---

## 1. Domain Entities & Schemas

### 1.1 Enums

```python
class OptimizationGoal(str, Enum):
    BALANCED = "balanced"
    COST_MIN = "cost_min"
    LATENCY_MIN = "latency_min"
    QUALITY_MAX = "quality_max"
```

### 1.2 Data Structures

```python
@dataclass
class AssignmentScenario:
    scenario_id: str
    worker_id: str
    model_name: str
    worker_skills: list[str] = field(default_factory=list)
    historical_quality: float = 1.0

@dataclass
class ScenarioEvaluation:
    scenario: AssignmentScenario
    skill_fit: bool
    missing_skills: list[str]
    calibrated_estimate: CalibratedEstimate
    predicted_quality: float
    composite_score: float
    rank: int = 1
    reasoning: str = ""

@dataclass
class TeamComparisonReport:
    task_id: str
    task_title: str
    required_skills: list[str]
    goal: OptimizationGoal
    evaluations: list[ScenarioEvaluation]
    recommended_scenario_id: str | None = None
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
```

---

## 2. Comparative Simulator API

```python
class TeamComparisonSimulator:
    def __init__(
        self,
        calibration_engine: EstimationCalibrationEngine | None = None,
        pricing_registry: ModelPricingRegistry | None = None,
    ) -> None:
        ...

    def evaluate_scenarios(
        self,
        task: Task | TaskModel,
        scenarios: list[AssignmentScenario],
        baseline: EstimationMetrics | None = None,
        goal: OptimizationGoal = OptimizationGoal.BALANCED,
    ) -> TeamComparisonReport:
        """Simulate and rank candidate worker-model assignments for a task."""
```
