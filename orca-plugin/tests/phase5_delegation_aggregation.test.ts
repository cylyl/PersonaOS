/**
 * Phase 5 Test Suite: Delegation Progress, Result Aggregation & Decision Gates (Milestone 5 / Issue #5)
 */

import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import {
  PersonaOSOrcaPluginClient,
  calculateDelegationProgress,
  aggregateChildTaskResults,
  StaleGenerationError,
  GatewayDagNode,
} from '../index.js'

describe('Phase 5: Delegation Progress, Result Aggregation & Decision Gates', () => {
  it('calculates multi-agent delegation progress accurately with explicit formula', () => {
    const nodes: GatewayDagNode[] = [
      {
        task_id: 'sub-01',
        title: 'Backend API endpoint',
        status: 'completed',
        dependencies: [],
        wave: 1,
        generation: 1,
        worker_id: 'worker-dev-01',
      },
      {
        task_id: 'sub-02',
        title: 'Frontend UI view',
        status: 'in_progress',
        dependencies: ['sub-01'],
        wave: 2,
        generation: 1,
        worker_id: 'worker-dev-02',
      },
      {
        task_id: 'sub-03',
        title: 'Integration test suite',
        status: 'blocked',
        dependencies: ['sub-02'],
        wave: 3,
        generation: 1,
        worker_id: 'worker-qa-01',
      },
      {
        task_id: 'sub-04',
        title: 'Security audit gate',
        status: 'review',
        dependencies: ['sub-01'],
        wave: 2,
        generation: 2,
        worker_id: 'worker-sec-01',
      },
    ]

    const progress = calculateDelegationProgress(nodes)
    assert.equal(progress.total_subtasks, 4)
    assert.equal(progress.by_status.completed, 1)
    assert.equal(progress.by_status.in_progress, 1)
    assert.equal(progress.by_status.blocked, 1)
    assert.equal(progress.by_status.review, 1)
    assert.equal(progress.is_blocked, true)
    assert.equal(progress.has_failures, false)
    assert.equal(progress.requires_review, true)
    // Formula: 1 completed out of 4 total = 25%
    assert.equal(progress.estimated_progress_pct, 25)
    assert.deepEqual(progress.active_workers.sort(), ['worker-dev-01', 'worker-dev-02', 'worker-qa-01', 'worker-sec-01'].sort())
  })

  it('aggregates child task results into consolidated parent result', () => {
    const nodes: GatewayDagNode[] = [
      {
        task_id: 'sub-done-1',
        title: 'Schema migration',
        status: 'completed',
        dependencies: [],
        wave: 1,
        generation: 1,
        worker_id: 'worker-devops-01',
        parent_task_id: 'parent-01',
      },
      {
        task_id: 'sub-done-2',
        title: 'Model definition',
        status: 'completed',
        dependencies: [],
        wave: 1,
        generation: 1,
        worker_id: 'worker-dev-01',
        parent_task_id: 'parent-01',
      },
    ]

    const result = aggregateChildTaskResults(nodes, 'parent-01')
    assert.equal(result.parent_task_id, 'parent-01')
    assert.equal(result.all_succeeded, true)
    assert.equal(result.status, 'completed')
    assert.equal(result.children_results.length, 2)
    assert.ok(result.completed_at)
  })

  it('resolves decision gate successfully with matching generation', async () => {
    let sentBody: any = null

    const mockFetch: typeof fetch = async (url, init) => {
      assert.ok(String(url).includes('/integration/decision-gates/gate-hitl-01/resolve'))
      sentBody = JSON.parse(init?.body as string)
      return new Response(JSON.stringify({
        gate_id: 'gate-hitl-01',
        task_id: 'task-01',
        resolved: true,
        status: 'approved',
        new_generation: 4,
        timestamp: new Date().toISOString(),
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }

    const client = new PersonaOSOrcaPluginClient({
      apiBaseUrl: 'http://gateway.test',
      fetchFn: mockFetch,
    })

    const res = await client.resolveDecisionGate(
      'gate-hitl-01',
      'approve',
      3,
      'Approved after verifying security scan'
    )

    assert.equal(res.resolved, true)
    assert.equal(res.status, 'approved')
    assert.equal(res.new_generation, 4)
    assert.equal(sentBody.decision, 'approve')
    assert.equal(sentBody.expected_generation, 3)
    assert.equal(sentBody.review_notes, 'Approved after verifying security scan')
  })

  it('throws StaleGenerationError on 409 conflict when generation is outdated', async () => {
    const mockFetch: typeof fetch = async () => {
      return new Response(JSON.stringify({
        detail: 'Task generation has advanced (expected 2, found 3)',
        expected_generation: 2,
        current_generation: 3,
      }), {
        status: 409,
        statusText: 'Conflict',
        headers: { 'Content-Type': 'application/json' },
      })
    }

    const client = new PersonaOSOrcaPluginClient({
      apiBaseUrl: 'http://gateway.test',
      fetchFn: mockFetch,
    })

    await assert.rejects(
      async () => client.resolveDecisionGate('gate-hitl-02', 'approve', 2, 'Stale approval attempt'),
      (err: any) => {
        assert.ok(err instanceof StaleGenerationError)
        assert.equal(err.statusCode, 409)
        assert.equal(err.expectedGeneration, 2)
        assert.equal(err.currentGeneration, 3)
        assert.match(err.message, /generation has advanced/)
        return true
      }
    )
  })
})
