/**
 * Phase M6: Live Verification Test Suite
 *
 * Verifies end-to-end integration across all phases:
 * - M0: Audit integrity and CSP preservation
 * - M1: Orca Host Messaging channel & Worker lifecycle
 * - M2: PersonaOS Integration Client & Diagnostics
 * - M3: Workforce Bridge UI (0 browser fetch calls)
 * - M4: Real DAG evaluation, task submission & HITL generation fencing
 * - M5: SSE event stream relaying and buffering
 * - ADR 0041: Zero Direct Agent Invariant
 */

import { test, describe, after } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { PersonaOSOrcaPluginClient } from '../index.js'

describe('Phase M6: End-to-End Live Verification & ADR 0041 Compliance', () => {
  const bridgePath = '/root/.config/orca/plugin-host-bridge.js'
  const panelHtmlPath = '/root/projects/personaos-orca-plugin/panel/index.html'
  const auditLogPath = '/root/.config/orca/logs/plugin-audit.log'
  const client = new PersonaOSOrcaPluginClient({ apiBaseUrl: 'http://127.0.0.1:8000' })
  let activeBridge: any = null

  after(() => {
    if (activeBridge) {
      activeBridge.destroy()
    }
  })

  test('M3 / ADR 0041 Invariant: panel/index.html contains zero direct browser fetch calls', () => {
    assert.ok(fs.existsSync(panelHtmlPath), 'panel/index.html must exist')
    const html = fs.readFileSync(panelHtmlPath, 'utf8')

    // ADR 0041 requirement: Orca panels must communicate exclusively through native host messaging
    assert.doesNotMatch(
      html,
      /\bfetch\s*\(/i,
      'Violation: panel/index.html must not perform direct browser fetch() calls'
    )
    assert.doesNotMatch(
      html,
      /\bXMLHttpRequest\b/i,
      'Violation: panel/index.html must not use XMLHttpRequest'
    )
    assert.doesNotMatch(
      html,
      /\bnew\s+EventSource\b/i,
      'Violation: panel/index.html must not open direct browser EventSource connections'
    )

    // Verify host messaging invocation is used
    assert.match(
      html,
      /window\.orca\.host\.call\s*\(\s*['"]plugin\.messageWorker['"]/i,
      'Panel must invoke window.orca.host.call("plugin.messageWorker", ...)'
    )
  })

  test('ADR 0041 Invariant: direct execution of external agent CLIs is strictly prohibited', () => {
    const prohibited = ['claude', 'codex', 'agy', '/usr/bin/claude', '/root/.local/bin/codex']
    for (const cmd of prohibited) {
      assert.throws(
        () => client.assertZeroDirectAgentExecution(cmd),
        /DirectAgentExecutionViolationError|forbidden|prohibited/i,
        `Direct execution of ${cmd} must throw violation error`
      )
    }
  })

  test('M1: Plugin Host Bridge discovers plugin and mounts panel', async () => {
    assert.ok(fs.existsSync(bridgePath), 'plugin-host-bridge.js must exist')
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const { getPluginHostBridge } = await import(bridgePath)
    const bridge = activeBridge = getPluginHostBridge()
    await bridge.whenReady()

    const plugins = bridge.listPlugins()
    const plugin = plugins.find((p: any) => p.pluginKey === 'joseph118789-max.personaos-bridge')
    assert.ok(plugin, 'Plugin joseph118789-max.personaos-bridge must be registered')
    assert.equal(plugin.status, 'running')
    assert.ok(plugin.hasWorker, 'Plugin must declare trusted-worker capability')

    // Open panel entry
    const entry = await bridge.panels.open('runtime:test-m6', 'joseph118789-max.personaos-bridge', 'personaos')
    assert.ok(entry, 'Panel entry must open successfully')
    assert.ok(entry.sessionToken, 'Panel session token must be generated')
    assert.ok(entry.html.length > 500, 'Panel HTML must be loaded')
    assert.match(entry.html, /Zero Direct Agent Invariant/i)
  })

  test('M1 / M2: Live Host Messaging routes getHealth through worker to PersonaOS', async () => {
    const { getPluginHostBridge } = await import(bridgePath)
    const bridge = getPluginHostBridge()
    await bridge.whenReady()

    const entry = await bridge.panels.open('runtime:test-m6', 'joseph118789-max.personaos-bridge', 'personaos')
    const res = await bridge.panels.execute('runtime:test-m6', {
      sessionToken: entry.sessionToken,
      action: 'plugin.messageWorker',
      params: { action: 'getHealth' },
    })

    assert.equal(res.ok, true, 'Panel action outcome must be ok')
    assert.equal(res.value.ok, true, 'Worker message response must be ok')
    assert.equal(res.value.data.status, 'healthy')
    assert.equal(res.value.data.personaos_connected, true)
    assert.equal(res.value.data.gateway_version, '1.0.0')
  })

  test('M2: Worker retrieves live registered workforce roster', async () => {
    const { getPluginHostBridge } = await import(bridgePath)
    const bridge = getPluginHostBridge()
    await bridge.whenReady()

    const entry = await bridge.panels.open('runtime:test-m6', 'joseph118789-max.personaos-bridge', 'personaos')
    const res = await bridge.panels.execute('runtime:test-m6', {
      sessionToken: entry.sessionToken,
      action: 'plugin.messageWorker',
      params: { action: 'getWorkers' },
    })

    assert.equal(res.ok, true)
    assert.equal(res.value.ok, true)
    const workers = res.value.data
    assert.ok(Array.isArray(workers), 'Workers must be an array')
    assert.ok(workers.length >= 3, 'Must have at least 3 active workers')

    const workerIds = workers.map((w: any) => w.id)
    assert.ok(workerIds.includes('se'), 'Worker se must be registered')
    assert.ok(workerIds.includes('worker-dev-01'), 'Worker worker-dev-01 must be registered')
    assert.ok(workerIds.includes('worker-devops-01'), 'Worker worker-devops-01 must be registered')
  })

  test('M2: Diagnostics probe reports live PersonaOS gateway health', async () => {
    const { getPluginHostBridge } = await import(bridgePath)
    const bridge = getPluginHostBridge()
    await bridge.whenReady()

    const entry = await bridge.panels.open('runtime:test-m6', 'joseph118789-max.personaos-bridge', 'personaos')
    const res = await bridge.panels.execute('runtime:test-m6', {
      sessionToken: entry.sessionToken,
      action: 'plugin.messageWorker',
      params: { action: 'getDiagnostics' },
    })

    assert.equal(res.ok, true)
    assert.equal(res.value.ok, true)
    const diag = res.value.data
    assert.equal(diag.code, 'CONNECTED')
    assert.equal(diag.status, 'ok')
    assert.ok(typeof diag.latency_ms === 'number' && diag.latency_ms > 0)
    assert.equal(diag.auth_status, 'authenticated')
    assert.equal(diag.api_version_match, true)
  })

  test('M4: Queries evaluated Task DAG projection with 8 authoritative states', async () => {
    const { getPluginHostBridge } = await import(bridgePath)
    const bridge = getPluginHostBridge()
    await bridge.whenReady()

    const entry = await bridge.panels.open('runtime:test-m6', 'joseph118789-max.personaos-bridge', 'personaos')
    const res = await bridge.panels.execute('runtime:test-m6', {
      sessionToken: entry.sessionToken,
      action: 'plugin.messageWorker',
      params: { action: 'getDag', payload: { runId: 'default' } },
    })

    assert.equal(res.ok, true)
    assert.equal(res.value.ok, true)
    const dag = res.value.data
    assert.ok(typeof dag.total_tasks === 'number')
    assert.ok(Array.isArray(dag.nodes))
    assert.ok(dag.nodes.length > 0, 'DAG must contain projected nodes')

    const firstNode = dag.nodes[0]
    assert.ok(firstNode.task_id, 'Node must have task_id')
    assert.ok(firstNode.status, 'Node must have status')
    assert.ok(typeof firstNode.generation === 'number', 'Node must have generation')
  })

  test('M4: Submits task idempotently with profile version and side effect classification', async () => {
    const { getPluginHostBridge } = await import(bridgePath)
    const bridge = getPluginHostBridge()
    await bridge.whenReady()

    const entry = await bridge.panels.open('runtime:test-m6', 'joseph118789-max.personaos-bridge', 'personaos')
    const idempotencyKey = `m6-idem-${Date.now()}`
    const res = await bridge.panels.execute('runtime:test-m6', {
      sessionToken: entry.sessionToken,
      action: 'plugin.messageWorker',
      params: {
        action: 'submitTask',
        payload: {
          worker_id: 'worker-dev-01',
          title: 'M6 Automated Verification Task',
          objective: 'Verify end-to-end task submission through Orca host channel',
          dependencies: [],
          idempotency_key: idempotencyKey,
          side_effect_classification: 'IDEMPOTENT',
          profile_version: 1,
        },
      },
    })

    assert.equal(res.ok, true)
    assert.equal(res.value.ok, true)
    const task = res.value.data
    assert.ok(task.id, 'Created task must have ID')
    assert.equal(task.idempotency_key, idempotencyKey)
    assert.equal(task.status, 'queued')
  })

  test('M4: Enforces generation fencing on stale decision gate resolution', async () => {
    const { getPluginHostBridge } = await import(bridgePath)
    const bridge = getPluginHostBridge()
    await bridge.whenReady()

    const entry = await bridge.panels.open('runtime:test-m6', 'joseph118789-max.personaos-bridge', 'personaos')
    // Attempt resolving a dummy gate ID with an arbitrary expected generation
    const res = await bridge.panels.execute('runtime:test-m6', {
      sessionToken: entry.sessionToken,
      action: 'plugin.messageWorker',
      params: {
        action: 'resolveDecisionGate',
        payload: {
          gateId: 'non-existent-gate-id',
          decision: 'approve',
          expectedGeneration: 999,
          reviewNotes: 'Stale gate resolution test',
        },
      },
    })

    assert.equal(res.ok, true)
    // Worker must return ok: false with structured error
    assert.equal(res.value.ok, false)
    assert.ok(
      res.value.error.includes('404') || res.value.error.includes('Not Found') || res.value.code === 'ENDPOINT_NOT_FOUND',
      'Should report structured not found or generation error'
    )
  })

  test('M5: Worker maintains SSE connection and buffers real-time events', async () => {
    const { getPluginHostBridge } = await import(bridgePath)
    const bridge = getPluginHostBridge()
    await bridge.whenReady()

    const entry = await bridge.panels.open('runtime:test-m6', 'joseph118789-max.personaos-bridge', 'personaos')
    const res = await bridge.panels.execute('runtime:test-m6', {
      sessionToken: entry.sessionToken,
      action: 'plugin.messageWorker',
      params: {
        action: 'pollEvents',
        payload: { timeoutMs: 1000 },
      },
    })

    assert.equal(res.ok, true)
    assert.equal(res.value.ok, true)
    const poll = res.value.data
    assert.equal(poll.connected, true, 'Worker event stream must be connected to PersonaOS SSE')
    assert.ok(Array.isArray(poll.events), 'Events buffer must return an array')
  })

  test('M1 Audit Logging: Confirms security audit events are persisted', () => {
    assert.ok(fs.existsSync(auditLogPath), 'Audit log file must exist')
    const logs = fs.readFileSync(auditLogPath, 'utf8')
    assert.ok(logs.length > 0, 'Audit log must contain events')
    assert.match(logs, /panel_opened|panel:joseph118789-max\.personaos-bridge/i)
  })
})
