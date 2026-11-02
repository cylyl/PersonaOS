/**
 * PersonaOS ADE Plugin for Orca Desktop & Mobile.
 *
 * Implements the Orca visual interface and client integration to PersonaOS:
 * - Phase 1: Gateway/BFF connectivity, authentication, and structured diagnostics
 * - Phase 2: Idempotent task submission and immutable worker profile snapshots
 * - Phase 3: Parent/child task hierarchy and topological DAG/ready-waves projection
 * - Phase 4: PAB agent execution session visibility and lifecycle supervision
 * - Phase 5: Delegation progress, result aggregation, and generation-fenced HITL gates
 *
 * Non-negotiable principles:
 * - PersonaOS is the authoritative owner of task lifecycle, generation fencing, and review state.
 * - Strict prohibition on direct external agent CLI execution from Orca.
 */
export * from './contracts.js';
import { AgentExecutionSession, AggregatedParentResult, ConnectionDiagnosticCode, DelegationProgress, GatewayDagNode, GatewayDagProjection, GatewayDecisionGate, GatewayDiagnosticsResponse, GatewayEventMessage, GatewayEventType, GatewayGateResolveResponse, GatewayHealthResponse, GatewayReadyWavesResponse, GatewayRunCreateRequest, GatewayRunSummary, GatewayWorkerSummary, TaskHierarchyNode, TaskStatusInfo, TaskSubmitOptions, TaskSubmitResponse, WorkerMessageRequest, WorkerMessageResponse } from './contracts.js';
export declare class PersonaOSGatewayError extends Error {
    readonly statusCode?: number | undefined;
    readonly diagnosticCode: ConnectionDiagnosticCode;
    readonly details?: unknown;
    constructor(message: string, statusCode?: number | undefined, diagnosticCode?: ConnectionDiagnosticCode, details?: unknown);
}
export declare class AuthenticationError extends PersonaOSGatewayError {
    constructor(message?: string, statusCode?: number, details?: unknown);
}
export declare class NetworkError extends PersonaOSGatewayError {
    constructor(message: string, diagnosticCode?: ConnectionDiagnosticCode, details?: unknown);
}
export declare class StaleGenerationError extends PersonaOSGatewayError {
    readonly expectedGeneration: number;
    readonly currentGeneration?: number | undefined;
    constructor(message: string, expectedGeneration: number, currentGeneration?: number | undefined, details?: unknown);
}
export declare class DirectAgentExecutionViolationError extends Error {
    readonly command: string;
    readonly agent: string;
    constructor(command: string, agent: string);
}
export interface PersonaOSWorker {
    id: string;
    name: string;
    role: string;
    status: string;
    current_profile_version: number;
    capabilities: string[];
    permissions: string[];
}
export interface TaskReviewContext {
    task_id: string;
    status: string;
    review_notes?: string;
    worker_id?: string;
    generation?: number;
}
export interface OrchestrationRun {
    run_id: string;
    objective: string;
    coordinator_handle?: string;
    consumer_generation: number;
    legacy?: boolean;
    created_at?: string;
    updated_at?: string;
}
export interface OrchestrationTask {
    task_id: string;
    spec: string;
    task_title?: string;
    display_name?: string;
    status: 'pending' | 'ready' | 'claimed' | 'running' | 'review' | 'completed' | 'failed' | string;
    deps: string[];
    parent_id?: string;
    run_id?: string;
    personaos_task_id?: string;
}
export interface DecisionGate {
    gate_id: string;
    task_id: string;
    question: string;
    options: string[];
    status: 'pending' | 'resolved';
    resolution?: string;
    run_id?: string;
}
export interface PersonaOSClientOptions {
    apiBaseUrl?: string;
    token?: string;
    tokenProvider?: () => string | undefined | Promise<string | undefined>;
    timeoutMs?: number;
    maxRetries?: number;
    retryDelayMs?: number;
    headers?: Record<string, string>;
    fetchFn?: typeof fetch;
}
export type ConnectionState = 'connecting' | 'connected' | 'reconnecting' | 'disconnected';
export interface EventStreamOptions {
    runId?: string;
    heartbeatInterval?: number;
    initialLastEventId?: string;
    maxReconnectAttempts?: number;
    reconnectDelayMs?: number;
    maxReconnectDelayMs?: number;
    backoffMultiplier?: number;
    heartbeatTimeoutMs?: number;
    onEvent?: (event: GatewayEventMessage) => void;
    onTaskStatusChanged?: (payload: Record<string, unknown>, eventId: string) => void;
    onReadyWaveAdvanced?: (payload: Record<string, unknown>, eventId: string) => void;
    onGateCreated?: (payload: Record<string, unknown>, eventId: string) => void;
    onGateResolved?: (payload: Record<string, unknown>, eventId: string) => void;
    onHeartbeat?: (timestamp: string) => void;
    onConnectionStateChange?: (state: ConnectionState, error?: Error) => void;
    onError?: (error: Error) => void;
}
export interface EventStreamSubscription {
    unsubscribe: () => void;
    close: () => void;
    isConnected: () => boolean;
    getConnectionState: () => ConnectionState;
    getLastEventId: () => string | null;
}
/**
 * Validate that a target worker satisfies required skills before task dispatch.
 */
export declare function validateWorkerCapabilities(worker: PersonaOSWorker | GatewayWorkerSummary, requiredSkills: string[]): {
    compatible: boolean;
    missingSkills: string[];
};
/**
 * Map raw PersonaOS task status to canonical display metadata preserving all 8 lifecycle states.
 */
export declare function getTaskStatusInfo(status: string): TaskStatusInfo;
/**
 * Reconstruct hierarchical parent/child task tree from a DAG projection.
 */
export declare function buildTaskHierarchy(dag: GatewayDagProjection, rootTaskId?: string): TaskHierarchyNode[];
/**
 * Verify whether an incoming execution event is stale relative to current generation.
 */
export declare function isExecutionEventStale(eventGeneration: number, currentTaskGeneration: number): boolean;
/**
 * Calculate multi-agent delegation progress across DAG nodes.
 */
export declare function calculateDelegationProgress(nodes: GatewayDagNode[], parentTaskId?: string): DelegationProgress;
/**
 * Aggregate child task results into a consolidated parent task result view.
 */
export declare function aggregateChildTaskResults(nodes: GatewayDagNode[], parentTaskId?: string): AggregatedParentResult;
export declare class PersonaOSOrcaPluginClient {
    private apiBaseUrl;
    private token?;
    private tokenProvider?;
    private timeoutMs;
    private maxRetries;
    private retryDelayMs;
    private defaultHeaders;
    private fetchFn;
    constructor(optionsOrUrl?: string | PersonaOSClientOptions, legacyToken?: string);
    private normalizeUrl;
    private resolveBaseUrl;
    getBaseUrl(): string;
    setBaseUrl(url: string): void;
    setToken(token?: string): void;
    setTokenProvider(provider: () => string | undefined | Promise<string | undefined>): void;
    private resolveToken;
    private getHeaders;
    /**
     * Resilient request executor with timeouts, safe retries on read-only requests,
     * zero retries on write requests, and structured error mapping.
     */
    private request;
    getHealth(): Promise<GatewayHealthResponse>;
    getDiagnostics(targetUrl?: string): Promise<GatewayDiagnosticsResponse>;
    checkConnection(): Promise<GatewayDiagnosticsResponse>;
    listWorkers(): Promise<PersonaOSWorker[]>;
    submitTask(options: TaskSubmitOptions): Promise<TaskSubmitResponse>;
    listRuns(): Promise<OrchestrationRun[]>;
    createRun(run: GatewayRunCreateRequest): Promise<GatewayRunSummary>;
    getRunDag(runId: string): Promise<GatewayDagProjection>;
    getRunReadyWaves(runId: string): Promise<GatewayReadyWavesResponse>;
    getTaskHierarchy(runId: string, rootTaskId?: string): Promise<TaskHierarchyNode[]>;
    getTaskExecutionSession(taskId: string): Promise<AgentExecutionSession | null>;
    listAgentSessions(filter?: {
        taskId?: string;
        provider?: string;
    }): Promise<AgentExecutionSession[]>;
    cancelTaskExecution(taskId: string, expectedGeneration: number, reason?: string): Promise<{
        ok: boolean;
    }>;
    getDelegationProgress(runId: string, parentTaskId?: string): Promise<DelegationProgress>;
    getAggregatedResults(runId: string, parentTaskId?: string): Promise<AggregatedParentResult>;
    listDecisionGates(runId?: string): Promise<GatewayDecisionGate[]>;
    getDecisionGate(gateId: string): Promise<GatewayDecisionGate>;
    resolveDecisionGate(gateId: string, decision: 'approve' | 'reject', expectedGeneration: number, reviewNotes?: string, selectedOption?: string): Promise<GatewayGateResolveResponse>;
    getTaskReviewContext(taskId: string): Promise<TaskReviewContext>;
    approveTask(taskId: string, notes?: string, expectedGeneration?: number): Promise<{
        ok: boolean;
    }>;
    rejectTask(taskId: string, reason: string, expectedGeneration?: number): Promise<{
        ok: boolean;
    }>;
    resolveDecisionGateThroughReview(taskId: string, expectedGeneration: number, operatorId: string, action: 'approve' | 'reject', notesOrReason: string): Promise<{
        ok: boolean;
    }>;
    subscribeLifecycleEvents(options?: EventStreamOptions): EventStreamSubscription;
    publishEvent(eventType: GatewayEventType, payload?: Record<string, unknown>, eventId?: string): Promise<GatewayEventMessage>;
    /**
     * Guard verifying that no external agent binary is directly executed by the UI.
     * Invariant (ADR 0038 / ADR 0040): UI and ADE layers must NEVER spawn raw agent binaries.
     */
    assertZeroDirectAgentExecution(targetCommand: string): void;
}
export interface PluginContext {
    api: {
        major: number;
        minor: number;
    };
    commands: {
        register: (commandId: string, handler: (args: unknown, context?: {
            signal?: AbortSignal;
        }) => Promise<unknown>) => {
            dispose: () => void;
        };
    };
    events: {
        on: (event: string, handler: (payload: unknown) => Promise<unknown> | void) => {
            dispose: () => void;
        };
    };
    host: {
        call: (method: string, params?: unknown, options?: {
            signal?: AbortSignal;
        }) => Promise<unknown>;
        has: (method: string) => boolean;
    };
    grantedCapabilities: readonly string[];
    log: (message: string) => void;
}
export declare function handleWorkerMessage(request: WorkerMessageRequest, client: PersonaOSOrcaPluginClient): Promise<WorkerMessageResponse>;
export default function activate(context: PluginContext): Promise<void>;
export declare function deactivate(): Promise<void>;
//# sourceMappingURL=index.d.ts.map