/**
 * PersonaOS–Orca Integration Gateway Type Contracts (ADR 0041).
 *
 * Defines the normalized, type-safe API contract for the PersonaOS Integration Gateway
 * (/integration/*) and Orca ADE integration layer.
 *
 * Invariants:
 * - Direct browser fetch() from Orca ADE to external PersonaOS backend ports is prohibited.
 * - All communication flows through the Gateway (Option B/C BFF).
 * - Zero Shadow State: The Gateway maintains no independent queue or scheduler.
 * - Generation Fencing: Gate resolution must supply expected_generation.
 * - Zero Direct Agent Execution: Orca UI and ADE never execute raw agent CLIs.
 */
// ============================================================================
// Phase 1: Gateway Diagnostics & Connectivity Contracts
// ============================================================================
/**
 * Structured diagnostic classification codes for ADE <-> PersonaOS connectivity.
 */
export var ConnectionDiagnosticCode;
(function (ConnectionDiagnosticCode) {
    ConnectionDiagnosticCode["CONNECTED"] = "CONNECTED";
    ConnectionDiagnosticCode["NETWORK_UNREACHABLE"] = "NETWORK_UNREACHABLE";
    ConnectionDiagnosticCode["CORS_BLOCKED"] = "CORS_BLOCKED";
    ConnectionDiagnosticCode["AUTHENTICATION_FAILED"] = "AUTHENTICATION_FAILED";
    ConnectionDiagnosticCode["ENDPOINT_NOT_FOUND"] = "ENDPOINT_NOT_FOUND";
    ConnectionDiagnosticCode["API_VERSION_MISMATCH"] = "API_VERSION_MISMATCH";
    ConnectionDiagnosticCode["INVALID_RESPONSE"] = "INVALID_RESPONSE";
    ConnectionDiagnosticCode["SERVICE_UNAVAILABLE"] = "SERVICE_UNAVAILABLE";
})(ConnectionDiagnosticCode || (ConnectionDiagnosticCode = {}));
//# sourceMappingURL=contracts.js.map