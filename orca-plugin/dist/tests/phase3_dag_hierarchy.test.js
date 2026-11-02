/**
 * Phase 3 Test Suite: Parent/Child Task Hierarchy & DAG/Ready Waves (Milestone 3 / Issue #3)
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { PersonaOSOrcaPluginClient, getTaskStatusInfo, buildTaskHierarchy, } from '../index.js';
describe('Phase 3: Parent/Child Tasks & DAG Projections', () => {
    it('preserves all 8 distinct PersonaOS lifecycle states without state collapse', () => {
        const sQueued = getTaskStatusInfo('queued');
        assert.equal(sQueued.rawStatus, 'queued');
        assert.equal(sQueued.displayLabel, 'Scheduled');
        assert.equal(sQueued.isTerminal, false);
        const sAssigned = getTaskStatusInfo('assigned');
        assert.equal(sAssigned.rawStatus, 'assigned');
        assert.equal(sAssigned.displayLabel, 'Claimed');
        assert.notEqual(sAssigned.displayLabel, 'Running', 'assigned must not be collapsed into Running');
        const sInProgress = getTaskStatusInfo('in_progress');
        assert.equal(sInProgress.rawStatus, 'in_progress');
        assert.equal(sInProgress.displayLabel, 'Running');
        const sBlocked = getTaskStatusInfo('blocked');
        assert.equal(sBlocked.rawStatus, 'blocked');
        assert.equal(sBlocked.displayLabel, 'Blocked');
        assert.equal(sBlocked.isBlocked, true);
        assert.notEqual(sBlocked.displayLabel, 'Failed', 'blocked must not be collapsed into Failed');
        const sReview = getTaskStatusInfo('review');
        assert.equal(sReview.rawStatus, 'review');
        assert.equal(sReview.displayLabel, 'Waiting for review');
        assert.equal(sReview.requiresReview, true);
        assert.notEqual(sReview.displayLabel, 'Completed', 'review must not be collapsed into Completed');
        const sCompleted = getTaskStatusInfo('completed');
        assert.equal(sCompleted.rawStatus, 'completed');
        assert.equal(sCompleted.displayLabel, 'Completed');
        assert.equal(sCompleted.isTerminal, true);
        const sFailed = getTaskStatusInfo('failed');
        assert.equal(sFailed.rawStatus, 'failed');
        assert.equal(sFailed.displayLabel, 'Failed');
        assert.equal(sFailed.isTerminal, true);
        const sCanceled = getTaskStatusInfo('canceled');
        assert.equal(sCanceled.rawStatus, 'canceled');
        assert.equal(sCanceled.displayLabel, 'Cancelled');
        assert.equal(sCanceled.isTerminal, true);
    });
    it('builds parent/child task hierarchy accurately from a DAG projection', () => {
        const dag = {
            run_id: 'run-hier-01',
            total_tasks: 4,
            completed_tasks: 1,
            ready_tasks: 1,
            edges: [
                { from: 'task-parent', to: 'task-child-1' },
                { from: 'task-parent', to: 'task-child-2' },
                { from: 'task-child-1', to: 'task-grandchild-1' },
            ],
            nodes: [
                {
                    task_id: 'task-parent',
                    title: 'Parent Orchestrator Task',
                    status: 'in_progress',
                    dependencies: [],
                    wave: 1,
                    generation: 2,
                    parent_task_id: null,
                    worker_id: 'coord-01',
                },
                {
                    task_id: 'task-child-1',
                    title: 'Child Service Implementation',
                    status: 'completed',
                    dependencies: ['task-parent'],
                    wave: 2,
                    generation: 1,
                    parent_task_id: 'task-parent',
                    worker_id: 'worker-dev-01',
                },
                {
                    task_id: 'task-child-2',
                    title: 'Child Database Setup',
                    status: 'in_progress',
                    dependencies: ['task-parent'],
                    wave: 2,
                    generation: 1,
                    parent_task_id: 'task-parent',
                    worker_id: 'worker-devops-01',
                },
                {
                    task_id: 'task-grandchild-1',
                    title: 'Grandchild Verification',
                    status: 'queued',
                    dependencies: ['task-child-1'],
                    wave: 3,
                    generation: 1,
                    parent_task_id: 'task-child-1',
                    worker_id: 'worker-qa-01',
                },
            ],
        };
        const tree = buildTaskHierarchy(dag);
        assert.equal(tree.length, 1, 'Should find 1 root node');
        const root = tree[0];
        assert.equal(root.task_id, 'task-parent');
        assert.equal(root.depth, 0);
        assert.equal(root.children.length, 2);
        const child1 = root.children.find((c) => c.task_id === 'task-child-1');
        assert.ok(child1);
        assert.equal(child1.depth, 1);
        assert.equal(child1.children.length, 1);
        const grandchild = child1.children[0];
        assert.equal(grandchild.task_id, 'task-grandchild-1');
        assert.equal(grandchild.depth, 2);
    });
    it('queries run DAG projection and ready waves from Gateway', async () => {
        const mockFetch = async (url) => {
            const urlStr = String(url);
            if (urlStr.includes('/dag')) {
                return new Response(JSON.stringify({
                    run_id: 'run-waves-01',
                    total_tasks: 3,
                    completed_tasks: 1,
                    ready_tasks: 1,
                    edges: [{ from: 't1', to: 't2' }, { from: 't2', to: 't3' }],
                    nodes: [
                        { task_id: 't1', title: 'Task 1', status: 'completed', dependencies: [], wave: 1, generation: 1 },
                        { task_id: 't2', title: 'Task 2', status: 'assigned', dependencies: ['t1'], wave: 2, generation: 1 },
                        { task_id: 't3', title: 'Task 3', status: 'blocked', dependencies: ['t2'], wave: 3, generation: 1 },
                    ],
                }), {
                    status: 200,
                    headers: { 'Content-Type': 'application/json' },
                });
            }
            if (urlStr.includes('/ready-waves')) {
                return new Response(JSON.stringify({
                    run_id: 'run-waves-01',
                    current_wave: 2,
                    waves: [
                        {
                            wave_number: 2,
                            ready_tasks: [
                                { task_id: 't2', title: 'Task 2', worker_id: 'worker-dev-01', priority: 'high', generation: 1 },
                            ],
                        },
                    ],
                }), {
                    status: 200,
                    headers: { 'Content-Type': 'application/json' },
                });
            }
            throw new Error(`Unexpected URL: ${urlStr}`);
        };
        const client = new PersonaOSOrcaPluginClient({
            apiBaseUrl: 'http://gateway.test',
            fetchFn: mockFetch,
        });
        const dag = await client.getRunDag('run-waves-01');
        assert.equal(dag.run_id, 'run-waves-01');
        assert.equal(dag.nodes.length, 3);
        const waves = await client.getRunReadyWaves('run-waves-01');
        assert.equal(waves.current_wave, 2);
        assert.equal(waves.waves[0].ready_tasks[0].task_id, 't2');
    });
});
//# sourceMappingURL=phase3_dag_hierarchy.test.js.map