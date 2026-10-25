# Specification: Task Execution Telemetry — Quality, Latency, Retries, and Costs

**Reference:** ADR 0029, Vikunja #112 (Phase 3)
**Module:** `personaos.telemetry`

---

## 1. Domain Entities & Schemas

### 1.1 `TaskTelemetry`

```python
@dataclass
class TaskTelemetry:
    task_id: str
    worker_id: str | None = None
    node_id: str | None = None
    model_name: str | None = None
    status: str = "completed"
    quality_score: float = 1.0
    queue_wait_ms: float = 0.0
    execution_duration_ms: float = 0.0
    total_latency_ms: float = 0.0
    retry_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    recorded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)
```

### 1.2 `WorkerMetricsAggregate` & `WorkloadMetricsAggregate`

```python
@dataclass
class WorkerMetricsAggregate:
    worker_id: str
    total_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    retried_tasks: int = 0
    success_rate: float = 0.0
    avg_quality_score: float = 0.0
    avg_execution_ms: float = 0.0
    p95_execution_ms: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0

@dataclass
class WorkloadMetricsAggregate:
    total_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    queued_tasks: int = 0
    in_progress_tasks: int = 0
    review_tasks: int = 0
    avg_queue_wait_ms: float = 0.0
    avg_execution_ms: float = 0.0
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    retry_rate: float = 0.0
    status_breakdown: dict[str, int] = field(default_factory=dict)
    priority_breakdown: dict[str, int] = field(default_factory=dict)
```

---

## 2. Model Pricing Registry

```python
@dataclass(frozen=True)
class ModelPrice:
    input_per_million: float
    output_per_million: float

class ModelPricingRegistry:
    def __init__(self, custom_rates: dict[str, ModelPrice] | None = None):
        ...

    def calculate_cost(
        self,
        model_name: str | None,
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        ...
```

---

## 3. Telemetry Service API

```python
class TelemetryService:
    def __init__(self, pricing_registry: ModelPricingRegistry | None = None):
        ...

    def record_telemetry(
        self,
        task: Task | TaskModel,
        output: dict[str, Any] | None = None,
        model_name: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        quality_score: float | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> TaskTelemetry:
        ...

    async def get_task_telemetry(
        self,
        session: AsyncSession,
        task_id: str,
    ) -> TaskTelemetry | None:
        ...

    async def aggregate_worker_metrics(
        self,
        session: AsyncSession,
        worker_id: str,
        window_days: int | None = None,
    ) -> WorkerMetricsAggregate:
        ...

    async def aggregate_workload_metrics(
        self,
        session: AsyncSession,
        window_days: int | None = None,
    ) -> WorkloadMetricsAggregate:
        ...
```
