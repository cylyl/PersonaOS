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
export enum ConnectionDiagnosticCode {
  CONNECTED = 'CONNECTED',
  NETWORK_UNREACHABLE = 'NETWORK_UNREACHABLE',
  CORS_BLOCKED = 'CORS_BLOCKED',
  AUTHENTICATION_FAILED = 'AUTHENTICATION_FAILED',
  ENDPOINT_NOT_FOUND = 'ENDPOINT_NOT_FOUND',
  API_VERSION_MISMATCH = 'API_VERSION_MISMATCH',
  INVALID_RESPONSE = 'INVALID_RESPONSE',
  SERVICE_UNAVAILABLE = 'SERVICE_UNAVAILABLE',
}

/**
 * Health check response from Integration Gateway (/integration/health).
 */
export interface GatewayHealthResponse {
  status: 'healthy' | 'degraded' | 'down' | string
  gateway_version: string
  personaos_connected: boolean
  personaos_url: string
  pab_connected: boolean
  timestamp: string
}

/**
 * Comprehensive diagnostic probe report for Orca ADE (/integration/diagnostics).
 */
export interface GatewayDiagnosticsResponse {
  code: ConnectionDiagnosticCode
  status: 'ok' | 'degraded' | 'error' | string
  latency_ms: number
  upstream_url: string
  cors_supported: boolean
  pna_supported: boolean
  auth_status: 'authenticated' | 'unauthenticated' | 'invalid' | string
  api_version_match: boolean
  remediation?: string | null
  details?: Record<string, unknown>
}

// ============================================================================
// Phase 2: Task Submission, Idempotency & Profile Snapshot Contracts
// ============================================================================

export type SideEffectClassification = 'UNKNOWN' | 'IDEMPOTENT' | 'HAS_SIDE_EFFECTS'

/**
 * Immutable worker profile snapshot pinned at task submission time.
 */
export interface WorkerProfileSnapshot {
  profileId: string
  profileVersion: number
  requiredSkills: string[]
  profileSnapshotRef?: string
}

/**
 * Options for idempotent task submission to PersonaOS.
 */
export interface TaskSubmitOptions {
  workerId: string
  title: string
  objective: string
  worktreePath?: string
  worktreeId?: string
  branch?: string
  dependencies?: string[]
  runId?: string
  inputData?: Record<string, unknown>
  idempotencyKey?: string
  correlationId?: string
  sideEffectClassification?: SideEffectClassification
  profileSnapshot?: WorkerProfileSnapshot
}

export interface TaskSubmitResponse {
  id: string
  status: string
  idempotency_key?: string
  correlation_id?: string
  profile_version?: number
  created_at?: string
}

// ============================================================================
// Phase 3: DAG, Ready Waves & Task Hierarchy Contracts
// ============================================================================

/**
 * All 8 distinct PersonaOS authoritative lifecycle states.
 */
export type TaskLifecycleStatus =
  | 'queued'
  | 'assigned'
  | 'in_progress'
  | 'blocked'
  | 'review'
  | 'completed'
  | 'failed'
  | 'canceled'

export interface TaskStatusInfo {
  rawStatus: TaskLifecycleStatus
  displayLabel: string
  color: string
  isTerminal: boolean
  isActive: boolean
  isBlocked: boolean
  requiresReview: boolean
}

/**
 * Node in the parent/child task hierarchy.
 */
export interface TaskHierarchyNode {
  task_id: string
  parent_task_id?: string | null
  title: string
  worker_id?: string | null
  status: TaskLifecycleStatus
  generation: number
  dependencies: string[]
  children: TaskHierarchyNode[]
  depth: number
}

/**
 * Persistent worker summary for Orca ADE workforce roster (/integration/workers).
 */
export interface GatewayWorkerSummary {
  id: string
  name: string
  role: string
  status: 'idle' | 'running' | 'quarantined' | 'paused' | string
  current_profile_version: number
  capabilities: string[]
  permissions: string[]
  updated_at?: string | null
}

export interface GatewayRunCreateRequest {
  run_id: string
  objective: string
  coordinator_handle?: string | null
  metadata?: Record<string, unknown>
}

export interface GatewayRunSummary {
  run_id: string
  objective: string
  coordinator_handle?: string | null
  status: 'active' | 'completed' | 'failed' | string
  consumer_generation: number
  legacy?: boolean
  created_at?: string | null
  updated_at?: string | null
}

export interface GatewayDagNode {
  task_id: string
  title: string
  worker_id?: string | null
  status: TaskLifecycleStatus | string
  dependencies: string[]
  wave: number
  generation: number
  parent_task_id?: string | null
}

export interface GatewayDagEdge {
  from: string
  to: string
}

export interface GatewayDagProjection {
  run_id: string
  nodes: GatewayDagNode[]
  edges: GatewayDagEdge[]
  total_tasks: number
  completed_tasks: number
  ready_tasks: number
}

export interface GatewayReadyTask {
  task_id: string
  title: string
  worker_id?: string | null
  priority: 'low' | 'medium' | 'high' | 'urgent' | string
  generation: number
}

export interface GatewayReadyWave {
  wave_number: number
  ready_tasks: GatewayReadyTask[]
}

export interface GatewayReadyWavesResponse {
  run_id: string
  current_wave: number
  waves: GatewayReadyWave[]
}

// ============================================================================
// Phase 4: PAB Agent Execution & Session Contracts
// ============================================================================

export type AgentRuntimeProvider =
  | 'openclaw'
  | 'agy'
  | 'codex'
  | 'claude'
  | 'pi'
  | 'dsh'
  | string

export interface AgentExecutionSession {
  session_id: string
  execution_id: string
  task_id: string
  provider: AgentRuntimeProvider
  agent_id: string
  agent_profile?: string
  worktree_id?: string
  worktree_path?: string
  status: 'starting' | 'active' | 'suspended' | 'idle' | 'completed' | 'failed' | 'cancelled'
  started_at: string
  completed_at?: string | null
  last_heartbeat?: string | null
  metrics?: {
    input_tokens: number
    output_tokens: number
    total_tokens: number
    cost_usd?: number
    latency_ms?: number
  }
  exit_code?: number | null
  error_message?: string | null
  exhaustion_reason?: string | null
  generation: number
}

export interface TaskCancelRequest {
  task_id: string
  expected_generation: number
  reason?: string
}

// ============================================================================
// Phase 5: Delegation Progress & Result Aggregation Contracts
// ============================================================================

export interface DelegationProgress {
  parent_task_id: string
  total_subtasks: number
  by_status: Record<TaskLifecycleStatus, number>
  estimated_progress_pct: number
  is_blocked: boolean
  has_failures: boolean
  requires_review: boolean
  active_workers: string[]
  active_providers: string[]
}

export interface ChildTaskResult {
  task_id: string
  title: string
  worker_id?: string | null
  agent_provider?: string | null
  status: TaskLifecycleStatus
  output_summary?: string | null
  artifacts?: Array<{ name: string; path?: string; uri?: string }>
  metrics?: { total_tokens: number; latency_ms: number }
  error_details?: string | null
  generation: number
  completed_at?: string | null
}

export interface AggregatedParentResult {
  parent_task_id: string
  run_id?: string
  status: TaskLifecycleStatus
  children_results: ChildTaskResult[]
  consolidated_summary?: string
  all_succeeded: boolean
  completed_at?: string | null
}

export interface GatewayDecisionGate {
  gate_id: string
  task_id: string
  run_id?: string | null
  title: string
  question: string
  options: string[]
  status: 'pending' | 'approved' | 'rejected' | string
  execution_generation: number
  review_context: Record<string, unknown>
  created_at?: string | null
}

export interface GatewayGateResolveRequest {
  decision: 'approve' | 'reject'
  expected_generation: number
  review_notes?: string | null
  selected_option?: string | null
}

export interface GatewayGateResolveResponse {
  gate_id: string
  task_id: string
  resolved: boolean
  status: 'approved' | 'rejected' | string
  new_generation: number
  timestamp: string
}

// ============================================================================
// Real-Time SSE Lifecycle Event Contracts
// ============================================================================

export type GatewayEventType =
  | 'task_status_changed'
  | 'ready_wave_advanced'
  | 'gate_created'
  | 'gate_resolved'
  | 'heartbeat'
  | 'execution_session_updated'
  | string

export interface GatewayEventMessage {
  event_id: string
  event_type: GatewayEventType
  payload: Record<string, unknown>
  timestamp: string
}

// ============================================================================
// Phase M1: Orca Host Messaging Contracts (ADR 0041)
// ============================================================================

export type WorkerAction =
  | 'getHealth'
  | 'getDiagnostics'
  | 'getWorkers'
  | 'submitTask'
  | 'getDag'
  | 'getReadyWaves'
  | 'getDecisionGates'
  | 'resolveDecisionGate'
  | 'getExecutionSession'
  | 'pollEvents'
  | 'publishEvent'

export interface WorkerMessageRequest<T = unknown> {
  action: WorkerAction
  payload?: T
  correlationId?: string
}

export interface WorkerMessageResponse<T = unknown> {
  ok: boolean
  data?: T
  error?: string
  code?: string
  correlationId?: string
}

export interface PollEventsPayload {
  since?: string | number
  timeoutMs?: number
}

export interface PollEventsResponse {
  events: GatewayEventMessage[]
  lastEventId?: string
  connected: boolean
}

