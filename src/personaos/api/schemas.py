"""Pydantic request and response schemas for the PersonaOS control plane API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    """Health check response."""

    status: str = Field(..., description="Overall health: ok | degraded | down")
    db: str = Field(..., description="Database connectivity: ok | down")
    last_migration: str | None = Field(None, description="Current Alembic revision")
    version: str = Field("0.1.0", description="PersonaOS version")


class WorkerPersonaSchema(BaseModel):
    """Behavioral personality configuration for a worker profile."""

    model_config = ConfigDict(extra="ignore")

    style: str | None = None
    communication: str | None = None
    autonomy: str | None = None
    behavior: dict[str, Any] = Field(default_factory=dict)
    goals: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)


class WorkerCreateRequest(BaseModel):
    """Request payload to register a new worker."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., description="Worker identifier, e.g. worker-devops-001")
    name: str | None = Field(None, description="Human readable name")
    role: str = Field(..., description="Worker role, e.g. devops, developer, qa")
    runtime: str = Field("openclaw", description="Runtime adapter name")
    description: str | None = Field(None, description="Role description")
    max_concurrent_tasks: int = Field(1, ge=1, description="Max concurrent tasks")
    skills: list[str] = Field(default_factory=list, description="Skills list")
    permissions: list[str] = Field(default_factory=list, description="Permissions list")
    persona: WorkerPersonaSchema = Field(default_factory=WorkerPersonaSchema)
    instructions: str | None = None
    model: str | None = None


class WorkerProfileCreateRequest(BaseModel):
    """Request payload to create a new profile version for a worker."""

    model_config = ConfigDict(extra="ignore")

    version: int = Field(..., ge=1, description="Monotonically increasing version")
    role: str
    runtime: str = "openclaw"
    max_concurrent_tasks: int = Field(1, ge=1)
    description: str | None = None
    persona: WorkerPersonaSchema = Field(default_factory=WorkerPersonaSchema)
    model: str | None = None
    instructions: str | None = None
    skills: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    activate: bool = Field(False, description="Whether to activate immediately upon creation")


class WorkerResponse(BaseModel):
    """Worker model representation."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    role: str
    status: str
    runtime: str
    max_concurrent_tasks: int
    current_profile_version: int
    description: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    personality: dict[str, Any] = Field(default_factory=dict)
    memory_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_seen_at: datetime | None = None


class WorkerDetailResponse(WorkerResponse):
    """Detailed worker representation with profile info."""

    current_profile: dict[str, Any] | None = None
    available_versions: list[int] = Field(default_factory=list)


class WorkerQuarantineRequest(BaseModel):
    """Request payload to quarantine a worker."""

    reason: str | None = Field(None, description="Reason for quarantine")
    quarantined_by: str | None = Field(None, description="Actor requesting quarantine")


class TaskCreateRequest(BaseModel):
    """Request payload to enqueue a new task."""

    model_config = ConfigDict(extra="ignore")

    worker_id: str = Field(..., description="Target worker ID")
    title: str = Field(..., description="Short task title")
    objective: str = Field(..., description="Task objective / goal")
    input: dict[str, Any] = Field(default_factory=dict, description="Task inputs")
    required_skills: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    priority: str = Field("medium", description="low, medium, high, critical")
    type: str | None = Field(None, description="Optional task type")
    dependencies: list[str] = Field(default_factory=list, description="IDs of prerequisite tasks")
    deadline: datetime | None = None
    max_retries: int = Field(3, ge=0)
    profile_version: int | None = Field(
        None, description="Override specific profile version snapshot"
    )
    side_effect_classification: str = Field(
        "UNKNOWN", description="UNKNOWN | IDEMPOTENT | HAS_SIDE_EFFECTS"
    )


class TaskResponse(BaseModel):
    """Task representation."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    objective: str
    status: str
    priority: str
    type: str | None = None
    assigned_worker: str | None = None
    assigned_node: str | None = None
    required_skills: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    input: dict[str, Any] = Field(default_factory=dict)
    checkpoint_ref: str | None = None
    result: dict[str, Any] | None = None
    retry_count: int = 0
    max_retries: int = 3
    last_error: str | None = None
    lock_expires_at: datetime | None = None
    generation: int = 0
    side_effect_classification: str = "UNKNOWN"
    exhaustion_reason: str | None = None
    deadline: datetime | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    profile_version: int = 1


class TaskCheckpointResponse(BaseModel):
    """Checkpoint representation."""

    model_config = ConfigDict(from_attributes=True)

    ref: str
    task_id: str
    state: dict[str, Any]
    last_step: str | None = None
    created_at: datetime


class TaskActionRequest(BaseModel):
    """Request payload for legacy manual review actions."""

    reason: str | None = None
    actor_id: str | None = None


class ClassificationAttestation(BaseModel):
    """Accountable operator declaration for downgrading task classification to IDEMPOTENT."""

    model_config = ConfigDict(extra="forbid")

    requested_classification: Literal["IDEMPOTENT"] = "IDEMPOTENT"
    safety_justification: str = Field(
        ...,
        min_length=15,
        max_length=1000,
        description="Detailed operator attestation justifying why automatic replay is safe",
    )
    verification_reference: str | None = Field(
        None, max_length=200, description="Ticket, commit, or run reference confirming safety"
    )


class TaskApproveRequest(BaseModel):
    """Payload for approving and re-queuing a task from review state."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        ...,
        min_length=10,
        max_length=1000,
        description="Operational justification for approval and re-queue",
    )
    expected_generation: int = Field(
        ..., ge=0, description="Mandatory generation fence to prevent concurrent lost updates"
    )
    reassign_worker_id: str | None = Field(
        None, description="Explicitly reassign task to a different valid worker ID"
    )
    reset_retries: bool = Field(
        True, description="Reset retry_count to 0 to provide a fresh retry budget"
    )
    max_retries: int | None = Field(
        None, ge=1, le=20, description="Optionally increase max_retries limit"
    )
    attestation: ClassificationAttestation | None = Field(
        None, description="Mandatory attestation if changing classification to IDEMPOTENT"
    )
    resume_from_checkpoint: bool = Field(
        True, description="Whether to resume task from existing checkpoint if available"
    )
    checkpoint_ref_override: str | None = Field(
        None, description="Explicit checkpoint reference to resume from"
    )


class TaskResolveRequest(BaseModel):
    """Payload for manually marking a reviewed task completed with verified output."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        ...,
        min_length=10,
        max_length=1000,
        description="Verification notes explaining out-of-band success",
    )
    expected_generation: int = Field(
        ..., ge=0, description="Mandatory generation fence to prevent concurrent lost updates"
    )
    result: dict[str, Any] = Field(
        default_factory=dict, description="Verified output dictionary attested by the operator"
    )


class TaskRejectRequest(BaseModel):
    """Payload for terminally failing a reviewed task."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        ...,
        min_length=10,
        max_length=1000,
        description="Rejection reason / failure description",
    )
    expected_generation: int = Field(
        ..., ge=0, description="Mandatory generation fence to prevent concurrent lost updates"
    )


class TaskReviewContextResponse(BaseModel):
    """Aggregated diagnostic view for operator decision making."""

    model_config = ConfigDict(from_attributes=True)

    task: TaskResponse
    assigned_worker_id: str | None = None
    worker_name: str | None = None
    worker_role: str | None = None
    worker_runtime: str | None = None
    profile_version_snapshot: int = 1
    worker_current_profile_version: int | None = None
    is_snapshot_outdated: bool = False
    execution_evidence: dict[str, Any] = Field(default_factory=dict)
    recent_events: list[EventResponse] = Field(default_factory=list)
    checkpoints_count: int = 0
    latest_checkpoint: TaskCheckpointResponse | None = None
    checkpoint_history: list[TaskCheckpointResponse] = Field(default_factory=list)


class NodeBindRequest(BaseModel):
    """Request payload to bind a worker to a node."""

    worker_id: str
    node_id: str
    reason: str | None = None


class NodeResponse(BaseModel):
    """Worker node binding representation."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    worker_id: str
    node_id: str
    bound_at: datetime
    unbound_at: datetime | None = None
    reason: str | None = None


class NodeDrainRequest(BaseModel):
    """Request payload to drain a node."""

    reason: str | None = Field(None, description="Reason for drain")


class EventResponse(BaseModel):
    """Task event representation."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    type: str
    actor_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
