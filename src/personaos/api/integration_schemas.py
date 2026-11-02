"""PersonaOS API Integration Schemas.

Re-exports Integration Gateway contracts for use in FastAPI route definitions
and dependency injection.
"""

from personaos.integrations.common.gateway_schemas import (
    ConnectionDiagnosticCode,
    GatewayDagEdge,
    GatewayDagNode,
    GatewayDagProjection,
    GatewayDecisionGate,
    GatewayDiagnosticsResponse,
    GatewayEventMessage,
    GatewayGateResolveRequest,
    GatewayGateResolveResponse,
    GatewayHealthResponse,
    GatewayReadyTask,
    GatewayReadyWave,
    GatewayReadyWavesResponse,
    GatewayRunCreateRequest,
    GatewayRunSummary,
    GatewayWorkerSummary,
)

__all__ = [
    "ConnectionDiagnosticCode",
    "GatewayDagEdge",
    "GatewayDagNode",
    "GatewayDagProjection",
    "GatewayDecisionGate",
    "GatewayDiagnosticsResponse",
    "GatewayEventMessage",
    "GatewayGateResolveRequest",
    "GatewayGateResolveResponse",
    "GatewayHealthResponse",
    "GatewayReadyTask",
    "GatewayReadyWave",
    "GatewayReadyWavesResponse",
    "GatewayRunCreateRequest",
    "GatewayRunSummary",
    "GatewayWorkerSummary",
]
