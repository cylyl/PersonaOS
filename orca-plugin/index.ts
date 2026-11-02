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

import { randomUUID } from 'node:crypto'
export * from './contracts.js'
import {
  AgentExecutionSession,
  AggregatedParentResult,
  ChildTaskResult,
  ConnectionDiagnosticCode,
  DelegationProgress,
  GatewayDagNode,
  GatewayDagProjection,
  GatewayDecisionGate,
  GatewayDiagnosticsResponse,
  GatewayEventMessage,
  GatewayEventType,
  GatewayGateResolveResponse,
  GatewayHealthResponse,
  GatewayReadyWavesResponse,
  GatewayRunCreateRequest,
  GatewayRunSummary,
  GatewayWorkerSummary,
  PollEventsPayload,
  PollEventsResponse,
  TaskHierarchyNode,
  TaskLifecycleStatus,
  TaskStatusInfo,
  TaskSubmitOptions,
  TaskSubmitResponse,
  WorkerAction,
  WorkerMessageRequest,
  WorkerMessageResponse,
} from './contracts.js'

// ============================================================================
// Standardized Error Hierarchy
// ============================================================================

export class PersonaOSGatewayError extends Error {
  constructor(
    message: string,
    public readonly statusCode?: number,
    public readonly diagnosticCode: ConnectionDiagnosticCode = ConnectionDiagnosticCode.INVALID_RESPONSE,
    public readonly details?: unknown
  ) {
    super(message)
    this.name = 'PersonaOSGatewayError'
  }
}

export class AuthenticationError extends PersonaOSGatewayError {
  constructor(message = 'Authentication failed or token expired', statusCode = 401, details?: unknown) {
    super(message, statusCode, ConnectionDiagnosticCode.AUTHENTICATION_FAILED, details)
    this.name = 'AuthenticationError'
  }
}

export class NetworkError extends PersonaOSGatewayError {
  constructor(message: string, diagnosticCode = ConnectionDiagnosticCode.NETWORK_UNREACHABLE, details?: unknown) {
    super(message, undefined, diagnosticCode, details)
    this.name = 'NetworkError'
  }
}

export class StaleGenerationError extends PersonaOSGatewayError {
  constructor(
    message: string,
    public readonly expectedGeneration: number,
    public readonly currentGeneration?: number,
    details?: unknown
  ) {
    super(message, 409, ConnectionDiagnosticCode.INVALID_RESPONSE, details)
    this.name = 'StaleGenerationError'
  }
}

export class DirectAgentExecutionViolationError extends Error {
  constructor(public readonly command: string, public readonly agent: string) {
    super(
      `Direct execution of external agent CLI '${agent}' is forbidden (command: '${command}'). ` +
      `All agent execution must be scheduled through PersonaOS and supervised via PAB.`
    )
    this.name = 'DirectAgentExecutionViolationError'
  }
}

// ============================================================================
// Legacy Interfaces for Backward Compatibility
// ============================================================================

export interface PersonaOSWorker {
  id: string
  name: string
  role: string
  status: string
  current_profile_version: number
  capabilities: string[]
  permissions: string[]
}

export interface TaskReviewContext {
  task_id: string
  status: string
  review_notes?: string
  worker_id?: string
  generation?: number
}

export interface OrchestrationRun {
  run_id: string
  objective: string
  coordinator_handle?: string
  consumer_generation: number
  legacy?: boolean
  created_at?: string
  updated_at?: string
}

export interface OrchestrationTask {
  task_id: string
  spec: string
  task_title?: string
  display_name?: string
  status: 'pending' | 'ready' | 'claimed' | 'running' | 'review' | 'completed' | 'failed' | string
  deps: string[]
  parent_id?: string
  run_id?: string
  personaos_task_id?: string
}

export interface DecisionGate {
  gate_id: string
  task_id: string
  question: string
  options: string[]
  status: 'pending' | 'resolved'
  resolution?: string
  run_id?: string
}

// ============================================================================
// Phase 1: Client Options & Environment Resolution
// ============================================================================

export interface PersonaOSClientOptions {
  apiBaseUrl?: string
  token?: string
  tokenProvider?: () => string | undefined | Promise<string | undefined>
  timeoutMs?: number
  maxRetries?: number
  retryDelayMs?: number
  headers?: Record<string, string>
  fetchFn?: typeof fetch
}

export type ConnectionState = 'connecting' | 'connected' | 'reconnecting' | 'disconnected'

export interface EventStreamOptions {
  runId?: string
  heartbeatInterval?: number
  initialLastEventId?: string
  maxReconnectAttempts?: number
  reconnectDelayMs?: number
  maxReconnectDelayMs?: number
  backoffMultiplier?: number
  heartbeatTimeoutMs?: number
  onEvent?: (event: GatewayEventMessage) => void
  onTaskStatusChanged?: (payload: Record<string, unknown>, eventId: string) => void
  onReadyWaveAdvanced?: (payload: Record<string, unknown>, eventId: string) => void
  onGateCreated?: (payload: Record<string, unknown>, eventId: string) => void
  onGateResolved?: (payload: Record<string, unknown>, eventId: string) => void
  onHeartbeat?: (timestamp: string) => void
  onConnectionStateChange?: (state: ConnectionState, error?: Error) => void
  onError?: (error: Error) => void
}

export interface EventStreamSubscription {
  unsubscribe: () => void
  close: () => void
  isConnected: () => boolean
  getConnectionState: () => ConnectionState
  getLastEventId: () => string | null
}

// ============================================================================
// Server-Sent Events Lifecycle Stream Manager
// ============================================================================

class SSEStreamManager implements EventStreamSubscription {
  private abortController: AbortController | null = null
  private connected = false
  private connectionState: ConnectionState = 'disconnected'
  private lastEventId: string | null = null
  private reconnectAttempts = 0
  private closed = false
  private watchdogTimer: ReturnType<typeof setTimeout> | null = null
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private retryDelayMs = 3000
  private baseUrl: string
  private tokenGetter: () => Promise<string | undefined>
  private options: EventStreamOptions
  private fetchFn: typeof fetch

  constructor(
    baseUrl: string,
    tokenGetter: () => Promise<string | undefined>,
    options: EventStreamOptions,
    fetchFn: typeof fetch = fetch
  ) {
    this.baseUrl = baseUrl.replace(/\/+$/, '')
    this.tokenGetter = tokenGetter
    this.options = options
    this.fetchFn = fetchFn
    if (options.initialLastEventId) {
      this.lastEventId = options.initialLastEventId
    }
    this.retryDelayMs = options.reconnectDelayMs ?? 1000
  }

  start() {
    this.closed = false
    this.connect()
  }

  unsubscribe() {
    this.close()
  }

  close() {
    this.closed = true
    this.clearWatchdog()
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    if (this.abortController) {
      this.abortController.abort()
      this.abortController = null
    }
    this.connected = false
    this.setConnectionState('disconnected')
  }

  isConnected(): boolean {
    return this.connected
  }

  getConnectionState(): ConnectionState {
    return this.connectionState
  }

  getLastEventId(): string | null {
    return this.lastEventId
  }

  private setConnectionState(state: ConnectionState, error?: Error) {
    this.connectionState = state
    this.connected = state === 'connected'
    this.options.onConnectionStateChange?.(state, error)
  }

  private resetWatchdog() {
    this.clearWatchdog()
    const timeoutMs = this.options.heartbeatTimeoutMs ?? 30000
    this.watchdogTimer = setTimeout(() => {
      if (!this.closed && this.connected) {
        if (this.abortController) {
          this.abortController.abort()
        }
        this.handleDisconnect(new Error(`Watchdog timeout: no SSE frame received within ${timeoutMs}ms`))
      }
    }, timeoutMs)
  }

  private clearWatchdog() {
    if (this.watchdogTimer) {
      clearTimeout(this.watchdogTimer)
      this.watchdogTimer = null
    }
  }

  private async connect() {
    if (this.closed) return

    const isReconnect = this.reconnectAttempts > 0
    this.setConnectionState(isReconnect ? 'reconnecting' : 'connecting')

    this.abortController = new AbortController()
    const interval = this.options.heartbeatInterval ?? 15.0
    const queryParts: string[] = [`heartbeat_interval=${interval}`]
    if (this.options.runId) {
      queryParts.push(`run_id=${encodeURIComponent(this.options.runId)}`)
    }
    if (this.lastEventId) {
      queryParts.push(`last_event_id=${encodeURIComponent(this.lastEventId)}`)
    }

    const url = `${this.baseUrl}/integration/events?${queryParts.join('&')}`
    const headers: Record<string, string> = {
      Accept: 'text/event-stream',
      'Cache-Control': 'no-cache',
    }
    const token = await this.tokenGetter().catch(() => undefined)
    if (token) {
      headers['Authorization'] = `Bearer ${token}`
    }
    if (this.lastEventId) {
      headers['Last-Event-ID'] = this.lastEventId
    }

    try {
      const response = await this.fetchFn(url, {
        method: 'GET',
        headers,
        signal: this.abortController.signal,
      })

      if (!response.ok) {
        const errText = await response.text().catch(() => '')
        throw new Error(`SSE endpoint returned status ${response.status}: ${errText}`)
      }

      if (!response.body) {
        throw new Error('Response body is null, readable stream required for SSE')
      }

      this.reconnectAttempts = 0
      this.setConnectionState('connected')
      this.resetWatchdog()

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let currentEventId: string | null = null
      let currentEventType = 'message'
      let currentDataLines: string[] = []

      while (!this.closed) {
        const { value, done } = await reader.read()
        if (done) break

        this.resetWatchdog()
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split(/\r\n|\r|\n/)
        buffer = lines.pop() ?? ''

        for (const line of lines) {
          if (line === '') {
            if (currentDataLines.length > 0 || currentEventType === 'heartbeat') {
              const dataStr = currentDataLines.join('\n')
              this.dispatchEvent(currentEventId, currentEventType, dataStr)
            }
            currentEventId = null
            currentEventType = 'message'
            currentDataLines = []
            continue
          }

          if (line.startsWith(':')) continue

          if (line.startsWith('id:')) {
            currentEventId = line.substring(3).trim()
            if (currentEventId) {
              this.lastEventId = currentEventId
            }
          } else if (line.startsWith('event:')) {
            currentEventType = line.substring(6).trim()
          } else if (line.startsWith('data:')) {
            currentDataLines.push(line.substring(5).trim())
          } else if (line.startsWith('retry:')) {
            const parsedRetry = parseInt(line.substring(6).trim(), 10)
            if (!isNaN(parsedRetry) && parsedRetry > 0) {
              this.retryDelayMs = parsedRetry
            }
          }
        }
      }

      if (!this.closed) {
        this.handleDisconnect(new Error('SSE stream closed by server'))
      }
    } catch (err: any) {
      if (this.closed) return
      if (err.name === 'AbortError') return
      this.handleDisconnect(err instanceof Error ? err : new Error(String(err)))
    }
  }

  private dispatchEvent(eventId: string | null, eventType: string, rawData: string) {
    let payload: Record<string, unknown> = {}
    if (rawData) {
      try {
        const parsed = JSON.parse(rawData)
        if (typeof parsed === 'object' && parsed !== null) {
          if ('payload' in parsed && typeof parsed.payload === 'object' && parsed.payload !== null) {
            payload = parsed.payload as Record<string, unknown>
            if ('event_id' in parsed && typeof parsed.event_id === 'string') {
              eventId = parsed.event_id
              this.lastEventId = eventId
            }
            if ('event_type' in parsed && typeof parsed.event_type === 'string') {
              eventType = parsed.event_type
            }
          } else {
            payload = parsed as Record<string, unknown>
          }
        } else {
          payload = { value: parsed }
        }
      } catch {
        payload = { raw: rawData }
      }
    }

    const eventMessage: GatewayEventMessage = {
      event_id: eventId ?? `evt-${Date.now()}`,
      event_type: eventType,
      payload,
      timestamp: new Date().toISOString(),
    }

    if (eventType === 'heartbeat') {
      const hbTime = (payload.timestamp as string) || eventMessage.timestamp
      this.options.onHeartbeat?.(hbTime)
    } else if (eventType === 'task_status_changed') {
      this.options.onTaskStatusChanged?.(payload, eventMessage.event_id)
    } else if (eventType === 'ready_wave_advanced') {
      this.options.onReadyWaveAdvanced?.(payload, eventMessage.event_id)
    } else if (eventType === 'gate_created') {
      this.options.onGateCreated?.(payload, eventMessage.event_id)
    } else if (eventType === 'gate_resolved') {
      this.options.onGateResolved?.(payload, eventMessage.event_id)
    }

    this.options.onEvent?.(eventMessage)
  }

  private handleDisconnect(error: Error) {
    this.clearWatchdog()
    this.connected = false

    const maxAttempts = this.options.maxReconnectAttempts ?? Infinity
    if (this.reconnectAttempts >= maxAttempts) {
      this.setConnectionState('disconnected', error)
      this.options.onError?.(new Error(`Max reconnection attempts (${maxAttempts}) exceeded: ${error.message}`))
      return
    }

    this.setConnectionState('reconnecting', error)
    this.options.onError?.(error)

    const baseDelay = this.options.reconnectDelayMs ?? this.retryDelayMs
    const multiplier = this.options.backoffMultiplier ?? 1.5
    const maxDelay = this.options.maxReconnectDelayMs ?? 30000

    const expDelay = Math.min(baseDelay * Math.pow(multiplier, this.reconnectAttempts), maxDelay)
    const jitter = 0.8 + Math.random() * 0.4
    const delay = Math.round(expDelay * jitter)

    this.reconnectAttempts++
    this.reconnectTimer = setTimeout(() => {
      this.connect()
    }, delay)
  }
}

// ============================================================================
// Phase 2 & 3 Helpers: Skill Validation & State Mapping
// ============================================================================

/**
 * Validate that a target worker satisfies required skills before task dispatch.
 */
export function validateWorkerCapabilities(
  worker: PersonaOSWorker | GatewayWorkerSummary,
  requiredSkills: string[]
): { compatible: boolean; missingSkills: string[] } {
  const workerSkills = new Set((worker.capabilities || []).map((s) => s.toLowerCase()))
  const missingSkills = requiredSkills.filter((s) => !workerSkills.has(s.toLowerCase()))
  return {
    compatible: missingSkills.length === 0,
    missingSkills,
  }
}

/**
 * Map raw PersonaOS task status to canonical display metadata preserving all 8 lifecycle states.
 */
export function getTaskStatusInfo(status: string): TaskStatusInfo {
  const normalized = (status || '').toLowerCase().trim() as TaskLifecycleStatus
  switch (normalized) {
    case 'queued':
      return {
        rawStatus: 'queued',
        displayLabel: 'Scheduled',
        color: '#94a3b8',
        isTerminal: false,
        isActive: false,
        isBlocked: false,
        requiresReview: false,
      }
    case 'assigned':
      return {
        rawStatus: 'assigned',
        displayLabel: 'Claimed',
        color: '#38bdf8',
        isTerminal: false,
        isActive: true,
        isBlocked: false,
        requiresReview: false,
      }
    case 'in_progress':
      return {
        rawStatus: 'in_progress',
        displayLabel: 'Running',
        color: '#60a5fa',
        isTerminal: false,
        isActive: true,
        isBlocked: false,
        requiresReview: false,
      }
    case 'blocked':
      return {
        rawStatus: 'blocked',
        displayLabel: 'Blocked',
        color: '#f97316',
        isTerminal: false,
        isActive: false,
        isBlocked: true,
        requiresReview: false,
      }
    case 'review':
      return {
        rawStatus: 'review',
        displayLabel: 'Waiting for review',
        color: '#facc15',
        isTerminal: false,
        isActive: false,
        isBlocked: true,
        requiresReview: true,
      }
    case 'completed':
      return {
        rawStatus: 'completed',
        displayLabel: 'Completed',
        color: '#4ade80',
        isTerminal: true,
        isActive: false,
        isBlocked: false,
        requiresReview: false,
      }
    case 'failed':
      return {
        rawStatus: 'failed',
        displayLabel: 'Failed',
        color: '#f87171',
        isTerminal: true,
        isActive: false,
        isBlocked: false,
        requiresReview: false,
      }
    case 'canceled':
      return {
        rawStatus: 'canceled',
        displayLabel: 'Cancelled',
        color: '#64748b',
        isTerminal: true,
        isActive: false,
        isBlocked: false,
        requiresReview: false,
      }
    default:
      return {
        rawStatus: 'queued',
        displayLabel: status || 'Unknown',
        color: '#94a3b8',
        isTerminal: false,
        isActive: false,
        isBlocked: false,
        requiresReview: false,
      }
  }
}

/**
 * Reconstruct hierarchical parent/child task tree from a DAG projection.
 */
export function buildTaskHierarchy(
  dag: GatewayDagProjection,
  rootTaskId?: string
): TaskHierarchyNode[] {
  const nodeMap = new Map<string, TaskHierarchyNode>()
  const childMap = new Map<string, string[]>()

  for (const n of dag.nodes) {
    const statusInfo = getTaskStatusInfo(n.status)
    nodeMap.set(n.task_id, {
      task_id: n.task_id,
      parent_task_id: n.parent_task_id ?? null,
      title: n.title,
      worker_id: n.worker_id ?? null,
      status: statusInfo.rawStatus,
      generation: n.generation ?? 1,
      dependencies: [...(n.dependencies || [])],
      children: [],
      depth: 0,
    })

    if (n.parent_task_id) {
      const existing = childMap.get(n.parent_task_id) ?? []
      existing.push(n.task_id)
      childMap.set(n.parent_task_id, existing)
    }
  }

  // Populate children and calculate depths
  function attachChildren(node: TaskHierarchyNode, depth: number) {
    node.depth = depth
    const childIds = childMap.get(node.task_id) ?? []
    for (const cid of childIds) {
      const childNode = nodeMap.get(cid)
      if (childNode) {
        node.children.push(childNode)
        attachChildren(childNode, depth + 1)
      }
    }
  }

  const roots: TaskHierarchyNode[] = []
  if (rootTaskId && nodeMap.has(rootTaskId)) {
    const root = nodeMap.get(rootTaskId)!
    attachChildren(root, 0)
    roots.push(root)
  } else {
    for (const node of nodeMap.values()) {
      if (!node.parent_task_id || !nodeMap.has(node.parent_task_id)) {
        attachChildren(node, 0)
        roots.push(node)
      }
    }
  }

  return roots
}

// ============================================================================
// Phase 4 & 5 Helpers: Fencing & Aggregation
// ============================================================================

/**
 * Verify whether an incoming execution event is stale relative to current generation.
 */
export function isExecutionEventStale(
  eventGeneration: number,
  currentTaskGeneration: number
): boolean {
  return eventGeneration < currentTaskGeneration
}

/**
 * Calculate multi-agent delegation progress across DAG nodes.
 */
export function calculateDelegationProgress(
  nodes: GatewayDagNode[],
  parentTaskId?: string
): DelegationProgress {
  const filtered = parentTaskId
    ? nodes.filter((n) => n.parent_task_id === parentTaskId || n.task_id === parentTaskId)
    : nodes

  const byStatus: Record<TaskLifecycleStatus, number> = {
    queued: 0,
    assigned: 0,
    in_progress: 0,
    blocked: 0,
    review: 0,
    completed: 0,
    failed: 0,
    canceled: 0,
  }

  const workers = new Set<string>()
  for (const n of filtered) {
    const statusInfo = getTaskStatusInfo(n.status)
    byStatus[statusInfo.rawStatus] = (byStatus[statusInfo.rawStatus] || 0) + 1
    if (n.worker_id) workers.add(n.worker_id)
  }

  const total = filtered.length
  const completed = byStatus.completed || 0
  const estimated_progress_pct = total > 0 ? Math.round((completed / total) * 100) : 0

  return {
    parent_task_id: parentTaskId ?? 'root',
    total_subtasks: total,
    by_status: byStatus,
    estimated_progress_pct,
    is_blocked: byStatus.blocked > 0,
    has_failures: byStatus.failed > 0,
    requires_review: byStatus.review > 0,
    active_workers: Array.from(workers),
    active_providers: [],
  }
}

/**
 * Aggregate child task results into a consolidated parent task result view.
 */
export function aggregateChildTaskResults(
  nodes: GatewayDagNode[],
  parentTaskId?: string
): AggregatedParentResult {
  const children = parentTaskId ? nodes.filter((n) => n.parent_task_id === parentTaskId) : nodes
  const results: ChildTaskResult[] = []

  let allSucceeded = children.length > 0
  for (const c of children) {
    const statusInfo = getTaskStatusInfo(c.status)
    if (statusInfo.rawStatus !== 'completed') {
      allSucceeded = false
    }
    results.push({
      task_id: c.task_id,
      title: c.title,
      worker_id: c.worker_id ?? null,
      status: statusInfo.rawStatus,
      generation: c.generation,
      completed_at: statusInfo.isTerminal ? new Date().toISOString() : null,
    })
  }

  const parentStatus: TaskLifecycleStatus = children.length === 0
    ? 'queued'
    : allSucceeded
      ? 'completed'
      : results.some((r) => r.status === 'failed')
        ? 'failed'
        : results.some((r) => r.status === 'review')
          ? 'review'
          : results.some((r) => r.status === 'in_progress' || r.status === 'assigned')
            ? 'in_progress'
            : 'queued'

  return {
    parent_task_id: parentTaskId ?? 'root',
    status: parentStatus,
    children_results: results,
    all_succeeded: allSucceeded,
    completed_at: allSucceeded ? new Date().toISOString() : null,
  }
}

// ============================================================================
// Main Client: PersonaOSOrcaPluginClient
// ============================================================================

export class PersonaOSOrcaPluginClient {
  private apiBaseUrl: string
  private token?: string
  private tokenProvider?: () => string | undefined | Promise<string | undefined>
  private timeoutMs: number
  private maxRetries: number
  private retryDelayMs: number
  private defaultHeaders: Record<string, string>
  private fetchFn: typeof fetch

  constructor(
    optionsOrUrl: string | PersonaOSClientOptions = 'http://localhost:8000',
    legacyToken?: string
  ) {
    if (typeof optionsOrUrl === 'string') {
      this.apiBaseUrl = this.normalizeUrl(optionsOrUrl)
      this.token = legacyToken
      this.timeoutMs = 15000
      this.maxRetries = 2
      this.retryDelayMs = 1000
      this.defaultHeaders = {}
      this.fetchFn = fetch
    } else {
      const opts = optionsOrUrl || {}
      this.apiBaseUrl = this.resolveBaseUrl(opts.apiBaseUrl)
      this.token = opts.token
      this.tokenProvider = opts.tokenProvider
      this.timeoutMs = opts.timeoutMs ?? 15000
      this.maxRetries = opts.maxRetries ?? 2
      this.retryDelayMs = opts.retryDelayMs ?? 1000
      this.defaultHeaders = opts.headers ?? {}
      this.fetchFn = opts.fetchFn ?? fetch
    }
  }

  private normalizeUrl(url: string): string {
    return (url || '').trim().replace(/\/+$/, '')
  }

  private resolveBaseUrl(explicitUrl?: string): string {
    if (explicitUrl && explicitUrl.trim()) {
      return this.normalizeUrl(explicitUrl)
    }
    if (typeof process !== 'undefined' && process.env && process.env.PERSONAOS_GATEWAY_URL) {
      return this.normalizeUrl(process.env.PERSONAOS_GATEWAY_URL)
    }
    if (typeof window !== 'undefined' && window.location && window.location.origin) {
      if (window.location.origin.startsWith('http')) {
        return this.normalizeUrl(window.location.origin)
      }
    }
    return 'http://localhost:8000'
  }

  getBaseUrl(): string {
    return this.apiBaseUrl
  }

  setBaseUrl(url: string): void {
    this.apiBaseUrl = this.normalizeUrl(url)
  }

  setToken(token?: string): void {
    this.token = token
  }

  setTokenProvider(provider: () => string | undefined | Promise<string | undefined>): void {
    this.tokenProvider = provider
  }

  private async resolveToken(): Promise<string | undefined> {
    if (this.tokenProvider) {
      try {
        const resolved = await this.tokenProvider()
        if (resolved) return resolved
      } catch {
        // Fall back to static token
      }
    }
    return this.token
  }

  private async getHeaders(extraHeaders: Record<string, string> = {}, skipAuth = false): Promise<Record<string, string>> {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      ...this.defaultHeaders,
      ...extraHeaders,
    }
    if (!skipAuth) {
      const token = await this.resolveToken()
      if (token) {
        headers['Authorization'] = `Bearer ${token}`
      }
    }
    return headers
  }

  /**
   * Resilient request executor with timeouts, safe retries on read-only requests,
   * zero retries on write requests, and structured error mapping.
   */
  private async request<T>(
    path: string,
    options: {
      method?: 'GET' | 'POST' | 'PUT' | 'DELETE' | 'PATCH'
      body?: unknown
      headers?: Record<string, string>
      timeoutMs?: number
      skipAuth?: boolean
    } = {}
  ): Promise<T> {
    const method = options.method || 'GET'
    const isReadOnly = method === 'GET'
    const maxAttempts = isReadOnly ? Math.max(1, this.maxRetries + 1) : 1
    const timeout = options.timeoutMs ?? this.timeoutMs
    const url = `${this.apiBaseUrl}${path.startsWith('/') ? path : `/${path}`}`

    let lastError: Error | null = null

    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
      const controller = new AbortController()
      const timer = setTimeout(() => controller.abort(), timeout)

      try {
        const headers = await this.getHeaders(options.headers, options.skipAuth)
        const fetchOptions: RequestInit = {
          method,
          headers,
          signal: controller.signal,
        }
        if (options.body !== undefined) {
          fetchOptions.body = typeof options.body === 'string' ? options.body : JSON.stringify(options.body)
        }

        const res = await this.fetchFn(url, fetchOptions)
        clearTimeout(timer)

        if (res.ok) {
          const contentType = res.headers.get('content-type') || ''
          if (contentType.includes('application/json')) {
            return (await res.json()) as T
          }
          const text = await res.text()
          try {
            return JSON.parse(text) as T
          } catch {
            return text as unknown as T
          }
        }

        // Map HTTP error codes
        const errText = await res.text().catch(() => '')
        let errJson: any = null
        try {
          errJson = JSON.parse(errText)
        } catch {
          errJson = { detail: errText }
        }

        const rawDetail = errJson?.detail
        const detailMessage = typeof rawDetail === 'object' && rawDetail !== null
          ? JSON.stringify(rawDetail)
          : rawDetail

        if (res.status === 401 || res.status === 403) {
          throw new AuthenticationError(
            detailMessage || `Authentication failed (${res.status}): ${res.statusText}`,
            res.status,
            errJson
          )
        }

        if (res.status === 409) {
          const expected = errJson?.expected_generation ?? 0
          const current = errJson?.current_generation ?? expected + 1
          throw new StaleGenerationError(
            detailMessage || `Conflict (409): Optimistic generation mismatch`,
            expected,
            current,
            errJson
          )
        }

        throw new PersonaOSGatewayError(
          detailMessage || `Gateway error (${res.status}): ${res.statusText}`,
          res.status,
          res.status === 404
            ? ConnectionDiagnosticCode.ENDPOINT_NOT_FOUND
            : res.status >= 500
              ? ConnectionDiagnosticCode.SERVICE_UNAVAILABLE
              : ConnectionDiagnosticCode.INVALID_RESPONSE,
          errJson
        )
      } catch (err: any) {
        clearTimeout(timer)
        if (err instanceof PersonaOSGatewayError) {
          throw err
        }

        const isAbort = err.name === 'AbortError'
        const msg = isAbort
          ? `Request timeout after ${timeout}ms: ${method} ${path}`
          : `Network failure connecting to ${url}: ${err.message}`
        lastError = new NetworkError(msg, ConnectionDiagnosticCode.NETWORK_UNREACHABLE, err)

        if (attempt < maxAttempts) {
          await new Promise((r) => setTimeout(r, this.retryDelayMs * attempt))
          continue
        }
      }
    }

    throw lastError || new NetworkError(`Request failed: ${method} ${path}`)
  }

  // ==========================================================================
  // Phase 1: Gateway Connectivity, Health & Structured Diagnostics
  // ==========================================================================

  async getHealth(): Promise<GatewayHealthResponse> {
    return this.request<GatewayHealthResponse>('/integration/health')
  }

  async getDiagnostics(targetUrl?: string): Promise<GatewayDiagnosticsResponse> {
    const query = targetUrl ? `?target_url=${encodeURIComponent(targetUrl)}` : ''
    return this.request<GatewayDiagnosticsResponse>(`/integration/diagnostics${query}`)
  }

  async checkConnection(): Promise<GatewayDiagnosticsResponse> {
    try {
      return await this.getDiagnostics(this.apiBaseUrl)
    } catch (err: any) {
      return {
        code: ConnectionDiagnosticCode.NETWORK_UNREACHABLE,
        status: 'error',
        latency_ms: 0,
        upstream_url: this.apiBaseUrl,
        cors_supported: false,
        pna_supported: false,
        auth_status: 'unauthenticated',
        api_version_match: false,
        remediation: `Verify PersonaOS Integration Gateway is reachable at ${this.apiBaseUrl}. Details: ${err.message}`,
        details: { error: err.message },
      }
    }
  }

  // ==========================================================================
  // Phase 2: Workforce Roster & Idempotent Task Submission
  // ==========================================================================

  async listWorkers(): Promise<PersonaOSWorker[]> {
    try {
      const workers = await this.request<GatewayWorkerSummary[]>('/integration/workers')
      return workers.map((w) => ({
        id: w.id,
        name: w.name,
        role: w.role,
        status: w.status,
        current_profile_version: w.current_profile_version,
        capabilities: w.capabilities,
        permissions: w.permissions,
      }))
    } catch {
      return this.request<PersonaOSWorker[]>('/workers')
    }
  }

  async submitTask(options: TaskSubmitOptions): Promise<TaskSubmitResponse> {
    const rawOpts = options as any
    const idempotencyKey = options.idempotencyKey || rawOpts.idempotency_key || randomUUID()
    const correlationId = options.correlationId || rawOpts.correlation_id || `corr-${Date.now()}`
    const workerId = options.workerId || rawOpts.worker_id

    const headers: Record<string, string> = {
      'Idempotency-Key': idempotencyKey,
      'X-Correlation-ID': correlationId,
    }

    const rawSideEffect = String(options.sideEffectClassification || rawOpts.side_effect_classification || 'UNKNOWN').toUpperCase()
    let sideEffect = 'UNKNOWN'
    if (rawSideEffect === 'IDEMPOTENT' || rawSideEffect === 'READ_ONLY') {
      sideEffect = 'IDEMPOTENT'
    } else if (rawSideEffect === 'HAS_SIDE_EFFECTS' || rawSideEffect === 'MUTATIVE' || rawSideEffect === 'WRITE') {
      sideEffect = 'HAS_SIDE_EFFECTS'
    }

    const body: Record<string, unknown> = {
      worker_id: workerId,
      title: options.title,
      objective: options.objective,
      dependencies: options.dependencies || [],
      idempotency_key: idempotencyKey,
      correlation_id: correlationId,
      side_effect_classification: sideEffect,
      profile_version: options.profileSnapshot?.profileVersion ?? rawOpts.profile_version ?? 1,
      metadata: options.runId ? { run_id: options.runId } : undefined,
      input: {
        ...(options.inputData || {}),
        worktree: options.worktreeId
          ? {
              worktree_id: options.worktreeId,
              path: options.worktreePath,
              branch: options.branch,
            }
          : undefined,
      },
    }

    if (options.profileSnapshot) {
      body.profile_id = options.profileSnapshot.profileId
      body.profile_version = options.profileSnapshot.profileVersion
      body.required_skills = options.profileSnapshot.requiredSkills
      if (options.profileSnapshot.profileSnapshotRef) {
        body.profile_snapshot_ref = options.profileSnapshot.profileSnapshotRef
      }
    }

    const res = await this.request<{ id: string; status: string; profile_version?: number }>(
      '/tasks',
      {
        method: 'POST',
        headers,
        body,
      }
    )

    return {
      id: res.id,
      status: res.status,
      idempotency_key: idempotencyKey,
      correlation_id: correlationId,
      profile_version: res.profile_version ?? options.profileSnapshot?.profileVersion,
      created_at: new Date().toISOString(),
    }
  }

  // ==========================================================================
  // Phase 3: DAG, Ready Waves & Task Hierarchy Projections
  // ==========================================================================

  async listRuns(): Promise<OrchestrationRun[]> {
    return this.request<OrchestrationRun[]>('/integration/runs')
  }

  async createRun(run: GatewayRunCreateRequest): Promise<GatewayRunSummary> {
    return this.request<GatewayRunSummary>('/integration/runs', {
      method: 'POST',
      body: run,
    })
  }

  async getRunDag(runId: string): Promise<GatewayDagProjection> {
    return this.request<GatewayDagProjection>(`/integration/runs/${encodeURIComponent(runId)}/dag`)
  }

  async getRunReadyWaves(runId: string): Promise<GatewayReadyWavesResponse> {
    return this.request<GatewayReadyWavesResponse>(
      `/integration/runs/${encodeURIComponent(runId)}/ready-waves`
    )
  }

  async getTaskHierarchy(runId: string, rootTaskId?: string): Promise<TaskHierarchyNode[]> {
    const dag = await this.getRunDag(runId)
    return buildTaskHierarchy(dag, rootTaskId)
  }

  // ==========================================================================
  // Phase 4: PAB Agent Execution & Session Visibility
  // ==========================================================================

  async getTaskExecutionSession(taskId: string): Promise<AgentExecutionSession | null> {
    try {
      return await this.request<AgentExecutionSession>(`/integration/tasks/${encodeURIComponent(taskId)}/session`)
    } catch (err: any) {
      if (err.statusCode === 404) return null
      throw err
    }
  }

  async listAgentSessions(filter: { taskId?: string; provider?: string } = {}): Promise<AgentExecutionSession[]> {
    const queryParts: string[] = []
    if (filter.taskId) queryParts.push(`task_id=${encodeURIComponent(filter.taskId)}`)
    if (filter.provider) queryParts.push(`provider=${encodeURIComponent(filter.provider)}`)
    const query = queryParts.length > 0 ? `?${queryParts.join('&')}` : ''
    try {
      return await this.request<AgentExecutionSession[]>(`/integration/sessions${query}`)
    } catch {
      return []
    }
  }

  async cancelTaskExecution(
    taskId: string,
    expectedGeneration: number,
    reason = 'Cancelled by operator in Orca ADE'
  ): Promise<{ ok: boolean }> {
    return this.request<{ ok: boolean }>(`/integration/tasks/${encodeURIComponent(taskId)}/cancel`, {
      method: 'POST',
      body: {
        task_id: taskId,
        expected_generation: expectedGeneration,
        reason,
      },
    })
  }

  // ==========================================================================
  // Phase 5: Delegation Progress, Result Aggregation & Decision Gates
  // ==========================================================================

  async getDelegationProgress(runId: string, parentTaskId?: string): Promise<DelegationProgress> {
    try {
      const query = parentTaskId ? `?parent_task_id=${encodeURIComponent(parentTaskId)}` : ''
      return await this.request<DelegationProgress>(`/integration/runs/${encodeURIComponent(runId)}/delegation${query}`)
    } catch {
      const dag = await this.getRunDag(runId)
      return calculateDelegationProgress(dag.nodes, parentTaskId)
    }
  }

  async getAggregatedResults(runId: string, parentTaskId?: string): Promise<AggregatedParentResult> {
    try {
      const query = parentTaskId ? `?parent_task_id=${encodeURIComponent(parentTaskId)}` : ''
      return await this.request<AggregatedParentResult>(`/integration/runs/${encodeURIComponent(runId)}/results${query}`)
    } catch {
      const dag = await this.getRunDag(runId)
      return aggregateChildTaskResults(dag.nodes, parentTaskId)
    }
  }

  async listDecisionGates(runId?: string): Promise<GatewayDecisionGate[]> {
    const query = runId ? `?run_id=${encodeURIComponent(runId)}` : ''
    return this.request<GatewayDecisionGate[]>(`/integration/decision-gates${query}`)
  }

  async getDecisionGate(gateId: string): Promise<GatewayDecisionGate> {
    return this.request<GatewayDecisionGate>(`/integration/decision-gates/${encodeURIComponent(gateId)}`)
  }

  async resolveDecisionGate(
    gateId: string,
    decision: 'approve' | 'reject',
    expectedGeneration: number,
    reviewNotes?: string,
    selectedOption?: string
  ): Promise<GatewayGateResolveResponse> {
    return this.request<GatewayGateResolveResponse>(
      `/integration/decision-gates/${encodeURIComponent(gateId)}/resolve`,
      {
        method: 'POST',
        body: {
          decision,
          expected_generation: expectedGeneration,
          review_notes: reviewNotes,
          selected_option: selectedOption,
        },
      }
    )
  }

  // Legacy review queue methods
  async getTaskReviewContext(taskId: string): Promise<TaskReviewContext> {
    return this.request<TaskReviewContext>(`/tasks/${taskId}/review-context`)
  }

  async approveTask(taskId: string, notes?: string, expectedGeneration?: number): Promise<{ ok: boolean }> {
    return this.request<{ ok: boolean }>(`/tasks/${taskId}/approve`, {
      method: 'POST',
      body: { review_notes: notes, expected_generation: expectedGeneration },
    })
  }

  async rejectTask(taskId: string, reason: string, expectedGeneration?: number): Promise<{ ok: boolean }> {
    return this.request<{ ok: boolean }>(`/tasks/${taskId}/reject`, {
      method: 'POST',
      body: { reason, expected_generation: expectedGeneration },
    })
  }

  async resolveDecisionGateThroughReview(
    taskId: string,
    expectedGeneration: number,
    operatorId: string,
    action: 'approve' | 'reject',
    notesOrReason: string
  ): Promise<{ ok: boolean }> {
    const body = action === 'approve'
      ? { expected_generation: expectedGeneration, operator_id: operatorId, review_notes: notesOrReason }
      : { expected_generation: expectedGeneration, operator_id: operatorId, reason: notesOrReason }

    return this.request<{ ok: boolean }>(`/tasks/${taskId}/${action}`, {
      method: 'POST',
      body,
    })
  }

  // ==========================================================================
  // Real-Time Event Bus & Security Enforcement
  // ==========================================================================

  subscribeLifecycleEvents(options: EventStreamOptions = {}): EventStreamSubscription {
    const manager = new SSEStreamManager(
      this.apiBaseUrl,
      () => this.resolveToken(),
      options,
      this.fetchFn
    )
    manager.start()
    return manager
  }

  async publishEvent(
    eventType: GatewayEventType,
    payload: Record<string, unknown> = {},
    eventId?: string
  ): Promise<GatewayEventMessage> {
    return this.request<GatewayEventMessage>('/integration/events/publish', {
      method: 'POST',
      body: {
        event_id: eventId ?? `evt-${Date.now()}`,
        event_type: eventType,
        payload,
        timestamp: new Date().toISOString(),
      },
    })
  }

  /**
   * Guard verifying that no external agent binary is directly executed by the UI.
   * Invariant (ADR 0038 / ADR 0040): UI and ADE layers must NEVER spawn raw agent binaries.
   */
  assertZeroDirectAgentExecution(targetCommand: string): void {
    const prohibited = [
      'openclaw',
      'codex',
      'claude',
      'agy',
      'mcode',
      'dsh',
      'pi',
      'gemini',
      'cursor',
      'opencode',
      'opencode2',
    ]
    const firstWord = targetCommand.trim().split(/\s+/)[0].toLowerCase()
    for (const agent of prohibited) {
      if (firstWord === agent || firstWord.endsWith(`/${agent}`)) {
        throw new DirectAgentExecutionViolationError(targetCommand, agent)
      }
    }
  }
}

// ============================================================================
// Phase M1 & M2: Orca Plugin Worker Runtime Entrypoint
// ============================================================================

export interface PluginContext {
  api: { major: number; minor: number }
  commands: {
    register: (commandId: string, handler: (args: unknown, context?: { signal?: AbortSignal }) => Promise<unknown>) => { dispose: () => void }
  }
  events: {
    on: (event: string, handler: (payload: unknown) => Promise<unknown> | void) => { dispose: () => void }
  }
  host: {
    call: (method: string, params?: unknown, options?: { signal?: AbortSignal }) => Promise<unknown>
    has: (method: string) => boolean
  }
  grantedCapabilities: readonly string[]
  log: (message: string) => void
}

let activeClient: PersonaOSOrcaPluginClient | null = null
let eventStreamSubscription: EventStreamSubscription | null = null
const eventRingBuffer: GatewayEventMessage[] = []
const MAX_RING_BUFFER_SIZE = 1000
const waitingEventPollers: Array<{
  since?: string | number
  resolve: (response: PollEventsResponse) => void
  timer: NodeJS.Timeout
}> = []

function pushEventToBuffer(event: GatewayEventMessage) {
  eventRingBuffer.push(event)
  if (eventRingBuffer.length > MAX_RING_BUFFER_SIZE) {
    eventRingBuffer.shift()
  }
  while (waitingEventPollers.length > 0) {
    const poller = waitingEventPollers.shift()
    if (poller) {
      clearTimeout(poller.timer)
      const newEvents = getEventsSince(poller.since)
      poller.resolve({
        events: newEvents,
        lastEventId: eventRingBuffer[eventRingBuffer.length - 1]?.event_id,
        connected: eventStreamSubscription?.isConnected() ?? false,
      })
    }
  }
}

function getEventsSince(since?: string | number): GatewayEventMessage[] {
  if (!since) {
    return eventRingBuffer.slice(-20)
  }
  const sinceStr = String(since)
  const idx = eventRingBuffer.findIndex((e) => e.event_id === sinceStr)
  if (idx === -1) {
    return eventRingBuffer.slice(-50)
  }
  return eventRingBuffer.slice(idx + 1)
}

export async function handleWorkerMessage(
  request: WorkerMessageRequest,
  client: PersonaOSOrcaPluginClient
): Promise<WorkerMessageResponse> {
  const correlationId = request.correlationId || `req-${Date.now()}`
  try {
    switch (request.action) {
      case 'getHealth': {
        const data = await client.getHealth()
        return { ok: true, data, correlationId }
      }
      case 'getDiagnostics': {
        const payload = request.payload as { targetUrl?: string } | undefined
        const data = await client.getDiagnostics(payload?.targetUrl)
        return { ok: true, data, correlationId }
      }
      case 'getWorkers': {
        const data = await client.listWorkers()
        return { ok: true, data, correlationId }
      }
      case 'submitTask': {
        const p = request.payload as TaskSubmitOptions
        const data = await client.submitTask(p)
        return { ok: true, data, correlationId }
      }
      case 'getDag': {
        const p = request.payload as { runId?: string } | undefined
        let runId = p?.runId
        if (!runId) {
          const runs = await client.listRuns()
          runId = runs[0]?.run_id || 'run-default'
        }
        const data = await client.getRunDag(runId)
        return { ok: true, data, correlationId }
      }
      case 'getReadyWaves': {
        const p = request.payload as { runId?: string } | undefined
        let runId = p?.runId
        if (!runId) {
          const runs = await client.listRuns()
          runId = runs[0]?.run_id || 'run-default'
        }
        const data = await client.getRunReadyWaves(runId)
        return { ok: true, data, correlationId }
      }
      case 'getDecisionGates': {
        const p = request.payload as { runId?: string } | undefined
        const data = await client.listDecisionGates(p?.runId)
        return { ok: true, data, correlationId }
      }
      case 'resolveDecisionGate': {
        const p = request.payload as {
          gateId: string
          decision: 'approve' | 'reject'
          expectedGeneration: number
          reviewNotes?: string
          selectedOption?: string
        }
        const data = await client.resolveDecisionGate(
          p.gateId,
          p.decision,
          p.expectedGeneration,
          p.reviewNotes,
          p.selectedOption
        )
        return { ok: true, data, correlationId }
      }
      case 'getExecutionSession': {
        const p = request.payload as { taskId: string }
        const data = await client.getTaskExecutionSession(p.taskId)
        return { ok: true, data, correlationId }
      }
      case 'publishEvent': {
        const p = request.payload as { eventType: GatewayEventType; payload: Record<string, unknown>; eventId?: string }
        const data = await client.publishEvent(p.eventType, p.payload, p.eventId)
        return { ok: true, data, correlationId }
      }
      case 'pollEvents': {
        const p = (request.payload || {}) as PollEventsPayload
        const newEvents = getEventsSince(p.since)
        if (newEvents.length > 0) {
          return {
            ok: true,
            data: {
              events: newEvents,
              lastEventId: eventRingBuffer[eventRingBuffer.length - 1]?.event_id,
              connected: eventStreamSubscription?.isConnected() ?? false,
            },
            correlationId,
          }
        }
        const timeoutMs = Math.min(Math.max(1000, p.timeoutMs || 15000), 30000)
        return new Promise((resolve) => {
          const timer = setTimeout(() => {
            const idx = waitingEventPollers.findIndex((w) => w.timer === timer)
            if (idx !== -1) waitingEventPollers.splice(idx, 1)
            resolve({
              ok: true,
              data: {
                events: [],
                lastEventId: eventRingBuffer[eventRingBuffer.length - 1]?.event_id,
                connected: eventStreamSubscription?.isConnected() ?? false,
              },
              correlationId,
            })
          }, timeoutMs)
          waitingEventPollers.push({
            since: p.since,
            resolve: (res) => resolve({ ok: true, data: res, correlationId }),
            timer,
          })
        })
      }
      default:
        return {
          ok: false,
          error: `Unknown worker action: ${(request as any).action}`,
          code: 'unknown_action',
          correlationId,
        }
    }
  } catch (err) {
    const errorMsg = err instanceof Error ? err.message : String(err)
    const code = (err as any).diagnosticCode || 'action_failed'
    return {
      ok: false,
      error: errorMsg,
      code,
      correlationId,
    }
  }
}

export default async function activate(context: PluginContext): Promise<void> {
  const backendUrl = process.env.PERSONAOS_BACKEND_URL || 'http://127.0.0.1:8000'
  context.log(`[PersonaOS Worker] Activating PersonaOS workforce bridge worker connecting to ${backendUrl}...`)

  activeClient = new PersonaOSOrcaPluginClient({
    apiBaseUrl: backendUrl,
    token: process.env.PERSONAOS_API_TOKEN,
  })

  try {
    eventStreamSubscription = activeClient.subscribeLifecycleEvents({
      onEvent: (event) => {
        pushEventToBuffer(event)
      },
      onError: (err) => {
        context.log(`[PersonaOS Worker] EventStream error: ${err.message}`)
      },
      onConnectionStateChange: (state) => {
        context.log(`[PersonaOS Worker] Event stream state: ${state}`)
      },
    })
  } catch (err) {
    context.log(`[PersonaOS Worker] Initial SSE subscription warning: ${String(err)}`)
  }

  context.commands.register('personaos.messageWorker', async (args: unknown) => {
    if (!activeClient) {
      throw new Error('PersonaOS client is not initialized in worker')
    }
    const request = args as WorkerMessageRequest
    return await handleWorkerMessage(request, activeClient)
  })

  context.log(`[PersonaOS Worker] Worker activated and command 'personaos.messageWorker' registered.`)
}

export async function deactivate(): Promise<void> {
  if (eventStreamSubscription) {
    eventStreamSubscription.close()
    eventStreamSubscription = null
  }
  while (waitingEventPollers.length > 0) {
    const poller = waitingEventPollers.shift()
    if (poller) {
      clearTimeout(poller.timer)
      poller.resolve({ events: [], connected: false })
    }
  }
  activeClient = null
}

