"""PersonaOS–Orca Integration Gateway Schemas.

Pydantic v2 schemas defining the normalized, type-safe API contract for the
PersonaOS Integration Gateway (/integration/*) and Orca ADE integration layer.

Governing ADRs:
- ADR 0038: Three-Tier Unified Agent Architecture
- ADR 0040: Orca Orchestration as Workspace Coordination Layer
- ADR 0041: PersonaOS–Orca Integration Gateway Architecture
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ConnectionDiagnosticCode(StrEnum):
    """Structured diagnostic classification codes for ADE <-> PersonaOS connectivity."""

    CONNECTED = "CONNECTED"
    NETWORK_UNREACHABLE = "NETWORK_UNREACHABLE"
    CORS_BLOCKED = "CORS_BLOCKED"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    ENDPOINT_NOT_FOUND = "ENDPOINT_NOT_FOUND"
    API_VERSION_MISMATCH = "API_VERSION_MISMATCH"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"


class GatewayHealthResponse(BaseModel):
    """Health check response from Integration Gateway."""

    model_config = ConfigDict(extra="ignore")

    status: str = Field(..., description="Overall health: healthy | degraded | down")
    gateway_version: str = Field("1.0.0", description="Gateway implementation version")
    personaos_connected: bool = Field(
        ..., description="Whether upstream PersonaOS control plane is reachable"
    )
    personaos_url: str = Field(..., description="Configured PersonaOS upstream base URL")
    pab_connected: bool = Field(
        ..., description="Whether PAB execution supervisor is reachable"
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC), description="Health check timestamp"
    )


class GatewayDiagnosticsResponse(BaseModel):
    """Comprehensive diagnostic probe report for Orca ADE."""

    model_config = ConfigDict(extra="ignore")

    code: ConnectionDiagnosticCode = Field(
        ..., description="Diagnostic error classification code"
    )
    status: str = Field(..., description="Short status descriptor: ok | degraded | error")
    latency_ms: float = Field(..., ge=0.0, description="Roundtrip latency in milliseconds")
    upstream_url: str = Field(..., description="Target upstream URL probed")
    cors_supported: bool = Field(
        True, description="Whether CORS headers are correctly negotiated"
    )
    pna_supported: bool = Field(
        True, description="Whether Private Network Access headers are present"
    )
    auth_status: str = Field(
        "authenticated",
        description="Authentication state: authenticated | unauthenticated | invalid",
    )
    api_version_match: bool = Field(
        True, description="Whether API version is compatible"
    )
    remediation: str | None = Field(
        None, description="Actionable troubleshooting instructions if unhealthy"
    )
    details: dict[str, Any] = Field(
        default_factory=dict, description="Diagnostic telemetry and probe details"
    )


class GatewayWorkerSummary(BaseModel):
    """Persistent worker summary for Orca ADE workforce roster."""

    model_config = ConfigDict(from_attributes=True, extra="ignore")

    id: str = Field(..., description="Unique worker identifier")
    name: str = Field(..., description="Human-readable worker name")
    role: str = Field(..., description="Worker role/specialization")
    status: str = Field(
        ..., description="Operational status: idle | running | quarantined | paused"
    )
    current_profile_version: int = Field(
        ..., ge=1, description="Active worker profile version"
    )
    capabilities: list[str] = Field(
        default_factory=list, description="Worker skills and capabilities"
    )
    permissions: list[str] = Field(
        default_factory=list, description="Worker permissions"
    )
    updated_at: datetime | None = Field(None, description="Last update timestamp")


class GatewayRunCreateRequest(BaseModel):
    """Request payload to create an external coordination run."""

    model_config = ConfigDict(extra="ignore")

    run_id: str = Field(..., min_length=1, description="External run identifier")
    objective: str = Field(..., min_length=1, description="Overall run objective")
    coordinator_handle: str | None = Field(
        None, description="Assigned coordinator worker or handle"
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Arbitrary coordination metadata"
    )


class GatewayRunSummary(BaseModel):
    """External coordination run summary."""

    model_config = ConfigDict(extra="ignore")

    run_id: str = Field(..., description="External run identifier")
    objective: str = Field(..., description="Run objective")
    coordinator_handle: str | None = Field(
        None, description="Assigned coordinator worker or handle"
    )
    status: str = Field("active", description="Run status: active | completed | failed")
    consumer_generation: int = Field(
        0, ge=0, description="Coordination consumer generation"
    )
    legacy: bool = Field(False, description="Whether run was migrated from legacy schema")
    created_at: datetime | None = Field(None, description="Creation timestamp")
    updated_at: datetime | None = Field(None, description="Last update timestamp")


class GatewayDagNode(BaseModel):
    """Projected task node in the orchestration DAG."""

    model_config = ConfigDict(extra="ignore")

    task_id: str = Field(..., description="Task identifier")
    title: str = Field(..., description="Task title")
    worker_id: str | None = Field(None, description="Assigned worker identifier")
    status: str = Field(
        ...,
        description=(
            "Task status: pending | ready | claimed | running | review | completed | failed"
        ),
    )
    dependencies: list[str] = Field(
        default_factory=list, description="List of dependency task IDs"
    )
    wave: int = Field(1, ge=1, description="Calculated ready wave execution tier")
    generation: int = Field(1, ge=1, description="Execution generation fence counter")


class GatewayDagEdge(BaseModel):
    """Directed dependency edge in the task DAG."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    from_node: str = Field(
        ...,
        alias="from",
        serialization_alias="from",
        description="Source task ID (dependency)",
    )
    to_node: str = Field(
        ...,
        alias="to",
        serialization_alias="to",
        description="Target task ID (dependent)",
    )


class GatewayDagProjection(BaseModel):
    """Pre-evaluated DAG projection returned to Orca ADE."""

    model_config = ConfigDict(extra="ignore")

    run_id: str = Field(..., description="Coordination run identifier")
    nodes: list[GatewayDagNode] = Field(
        default_factory=list, description="Projected task DAG nodes"
    )
    edges: list[GatewayDagEdge] = Field(
        default_factory=list, description="Directed dependency edges"
    )
    total_tasks: int = Field(0, ge=0, description="Total tasks in DAG")
    completed_tasks: int = Field(0, ge=0, description="Completed tasks count")
    ready_tasks: int = Field(0, ge=0, description="Ready tasks count")


class GatewayReadyTask(BaseModel):
    """Task ready for execution within a ready wave."""

    model_config = ConfigDict(extra="ignore")

    task_id: str = Field(..., description="Task identifier")
    title: str = Field(..., description="Task title")
    worker_id: str | None = Field(None, description="Assigned worker identifier")
    priority: str = Field(
        "medium", description="Priority level: low | medium | high | urgent"
    )
    generation: int = Field(1, ge=1, description="Current execution generation")


class GatewayReadyWave(BaseModel):
    """Tier of ready tasks whose dependencies are fully resolved."""

    model_config = ConfigDict(extra="ignore")

    wave_number: int = Field(..., ge=1, description="Ready wave sequence number")
    ready_tasks: list[GatewayReadyTask] = Field(
        default_factory=list, description="Tasks ready in this wave"
    )


class GatewayReadyWavesResponse(BaseModel):
    """Evaluated ready waves for an orchestration run."""

    model_config = ConfigDict(extra="ignore")

    run_id: str = Field(..., description="Coordination run identifier")
    current_wave: int = Field(1, ge=1, description="Currently executing wave number")
    waves: list[GatewayReadyWave] = Field(
        default_factory=list, description="All waves with ready tasks"
    )


class GatewayDecisionGate(BaseModel):
    """Interactive decision gate (HITL) mapped from a task in review status."""

    model_config = ConfigDict(extra="ignore")

    gate_id: str = Field(..., description="Unique decision gate identifier")
    task_id: str = Field(..., description="Associated PersonaOS task ID")
    run_id: str | None = Field(None, description="Associated coordination run ID")
    title: str = Field(..., description="Gate title")
    question: str = Field(
        ..., description="Question or decision required from human reviewer"
    )
    options: list[str] = Field(
        default_factory=list, description="Available decision choices"
    )
    status: str = Field(
        "pending", description="Gate status: pending | approved | rejected"
    )
    execution_generation: int = Field(
        1, ge=1, description="Task generation fence token"
    )
    review_context: dict[str, Any] = Field(
        default_factory=dict, description="Diagnostic findings and context"
    )
    created_at: datetime | None = Field(None, description="Gate creation timestamp")


class GatewayGateResolveRequest(BaseModel):
    """Request payload to resolve an interactive decision gate."""

    model_config = ConfigDict(extra="ignore")

    decision: Literal["approve", "reject"] = Field(
        ..., description="Human decision: approve or reject"
    )
    expected_generation: int = Field(..., ge=1, description="Fencing generation token")
    review_notes: str | None = Field(None, description="Review feedback or rationale")
    selected_option: str | None = Field(None, description="Selected option choice")


class GatewayGateResolveResponse(BaseModel):
    """Response returned upon resolving a decision gate."""

    model_config = ConfigDict(extra="ignore")

    gate_id: str = Field(..., description="Decision gate identifier")
    task_id: str = Field(..., description="Task identifier")
    resolved: bool = Field(..., description="Whether gate resolution succeeded")
    status: str = Field(
        ..., description="New task review status: approved | rejected"
    )
    new_generation: int = Field(..., ge=1, description="Advanced generation counter")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC), description="Resolution timestamp"
    )


class GatewayEventMessage(BaseModel):
    """Server-Sent Event message payload."""

    model_config = ConfigDict(extra="ignore")

    event_id: str = Field(..., description="Unique event identifier")
    event_type: str = Field(
        ...,
        description=(
            "Event type: task_status_changed | ready_wave_advanced | "
            "gate_created | gate_resolved | heartbeat"
        ),
    )
    payload: dict[str, Any] = Field(
        default_factory=dict, description="Event payload data"
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC), description="Event timestamp"
    )
