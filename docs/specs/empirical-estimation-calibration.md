# Specification: Empirical Estimation Calibration Engine

**Reference:** ADR 0031, Vikunja #119 (Phase 4)
**Module:** `personaos.simulation.calibration`

---

## 1. Domain Entities & Schemas

### 1.1 `EstimationMetrics`

```python
@dataclass(frozen=True)
class EstimationMetrics:
    duration_ms: float
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens
```

### 1.2 `CalibrationSample`

```python
@dataclass
class CalibrationSample:
    task_id: str
    task_type: str = "general"
    worker_id: str | None = None
    model_name: str | None = None
    estimated: EstimationMetrics
    actual: EstimationMetrics
    recorded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def duration_ratio(self) -> float:
        return self.actual.duration_ms / max(1.0, self.estimated.duration_ms)

    @property
    def token_ratio(self) -> float:
        return self.actual.total_tokens / max(1, self.estimated.total_tokens)

    @property
    def cost_ratio(self) -> float:
        return self.actual.cost_usd / max(0.0001, self.estimated.cost_usd)
```

### 1.3 `CalibratedEstimate`

```python
@dataclass(frozen=True)
class CalibratedEstimate:
    baseline: EstimationMetrics
    calibrated: EstimationMetrics
    duration_multiplier: float
    token_multiplier: float
    cost_multiplier: float
    confidence_score: float
    applied_profiles: list[str] = field(default_factory=list)
```

---

## 2. Calibration Engine API

```python
class EstimationCalibrationEngine:
    def __init__(
        self,
        alpha: float = 0.25,
        min_multiplier: float = 0.2,
        max_multiplier: float = 5.0,
    ) -> None:
        ...

    def record_sample(
        self,
        task_id: str,
        task_type: str,
        worker_id: str | None,
        model_name: str | None,
        estimated: EstimationMetrics,
        actual: EstimationMetrics,
    ) -> CalibrationSample:
        """Ingest execution telemetry sample and update segmented calibration profiles."""

    def calibrate(
        self,
        baseline: EstimationMetrics,
        task_type: str = "general",
        worker_id: str | None = None,
        model_name: str | None = None,
    ) -> CalibratedEstimate:
        """Apply learned empirical multipliers to adjust baseline estimate."""

    def get_profile(self, segment_key: str) -> CalibrationProfile | None:
        ...

    def list_profiles(self) -> list[CalibrationProfile]:
        ...
```
