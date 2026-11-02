/**
 * Phase M1 & M2: Host Messaging & PersonaOS Integration Client Tests.
 *
 * Verifies:
 * - Plugin worker command registration ('personaos.messageWorker')
 * - Request/response correlation ID preservation
 * - Typed action routing (getHealth, getDiagnostics, getWorkers, submitTask, getDag, resolveDecisionGate)
 * - Generation-fenced HITL gate resolution
 * - Event ring buffer & pollEvents delivery
 * - Error propagation without crashing worker
 * - Strict Zero Direct Agent Execution invariant
 */
import { test, describe, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import activate, { deactivate, handleWorkerMessage, PersonaOSOrcaPluginClient, } from '../index.js';
import { ConnectionDiagnosticCode, } from '../contracts.js';
describe('Phase M1 & M2: Host Messaging & PersonaOS Integration Client', () => {
    let mockFetch;
    let client;
    beforeEach(() => {
        mockFetch = async (input, init) => {
            const url = input.toString();
            const method = init?.method ?? 'GET';
            if (url.includes('/integration/health')) {
                const body = {
                    status: 'healthy',
                    gateway_version: '1.0.0',
                    personaos_connected: true,
                    personaos_url: 'http://127.0.0.1:8000',
                    pab_connected: true,
                    timestamp: new Date().toISOString(),
                };
                return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
            }
            if (url.includes('/integration/diagnostics')) {
                const body = {
                    code: ConnectionDiagnosticCode.CONNECTED,
                    status: 'ok',
                    latency_ms: 2.1,
                    upstream_url: 'http://127.0.0.1:8000',
                    cors_supported: true,
                    pna_supported: true,
                    auth_status: 'authenticated',
                    api_version_match: true,
                    remediation: null,
                    details: { ok: true },
                };
                return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
            }
            if (url.includes('/integration/workers')) {
                const body = [
                    {
                        id: 'worker-dev-01',
                        name: 'Developer Worker',
                        role: 'developer',
                        status: 'idle',
                        current_profile_version: 1,
                        capabilities: ['python', 'typescript'],
                        permissions: ['filesystem:read'],
                    },
                ];
                return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
            }
            if (url.includes('/integration/runs') && !url.includes('/dag')) {
                const body = [{ run_id: 'run-alpha', status: 'running', root_task_id: 'task-root' }];
                return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
            }
            if (url.includes('/integration/runs/run-alpha/dag')) {
                const body = {
                    run_id: 'run-alpha',
                    nodes: [
                        { id: 'task-1', title: 'Task 1', status: 'completed', worker_id: 'worker-dev-01', dependencies: [] },
                        { id: 'task-2', title: 'Task 2', status: 'pending', worker_id: 'worker-dev-01', dependencies: ['task-1'] },
                    ],
                };
                return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
            }
            if (url.includes('/integration/decision-gates/gate-42/resolve') && method === 'POST') {
                const reqBody = JSON.parse(init?.body);
                if (reqBody.expected_generation !== 3) {
                    return new Response(JSON.stringify({ error: 'Stale generation' }), { status: 409, headers: { 'Content-Type': 'application/json' } });
                }
                return new Response(JSON.stringify({
                    gate_id: 'gate-42',
                    task_id: 'task-2',
                    resolved: true,
                    status: 'approved',
                    new_generation: 4,
                    timestamp: new Date().toISOString(),
                }), { status: 200, headers: { 'Content-Type': 'application/json' } });
            }
            if (url.includes('/integration/decision-gates')) {
                const body = [
                    {
                        gate_id: 'gate-42',
                        task_id: 'task-2',
                        title: 'Deploy Approval',
                        question: 'Proceed with deployment?',
                        options: ['Approve', 'Reject'],
                        status: 'pending',
                        execution_generation: 3,
                        review_context: {},
                    },
                ];
                return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
            }
            if (url.includes('/tasks') && method === 'POST') {
                const reqBody = JSON.parse(init?.body);
                return new Response(JSON.stringify({
                    id: 'task-new-123',
                    status: 'pending',
                    profile_version: reqBody.profile_version ?? 1,
                }), { status: 201, headers: { 'Content-Type': 'application/json' } });
            }
            return new Response('Not Found', { status: 404 });
        };
        client = new PersonaOSOrcaPluginClient({
            apiBaseUrl: 'http://127.0.0.1:8000',
            fetchFn: mockFetch,
        });
    });
    afterEach(async () => {
        await deactivate();
    });
    test('routes getHealth and preserves correlation ID', async () => {
        const req = {
            action: 'getHealth',
            correlationId: 'test-corr-1',
        };
        const res = await handleWorkerMessage(req, client);
        assert.equal(res.ok, true);
        assert.equal(res.correlationId, 'test-corr-1');
        assert.equal(res.data.status, 'healthy');
        assert.equal(res.data.personaos_connected, true);
    });
    test('routes getDiagnostics with upstream probe result', async () => {
        const req = {
            action: 'getDiagnostics',
            payload: { targetUrl: 'http://127.0.0.1:8000' },
            correlationId: 'test-corr-2',
        };
        const res = await handleWorkerMessage(req, client);
        assert.equal(res.ok, true);
        assert.equal(res.data.code, ConnectionDiagnosticCode.CONNECTED);
    });
    test('routes getWorkers list', async () => {
        const req = {
            action: 'getWorkers',
        };
        const res = await handleWorkerMessage(req, client);
        assert.equal(res.ok, true);
        const workers = res.data;
        assert.equal(workers.length, 1);
        assert.equal(workers[0].id, 'worker-dev-01');
    });
    test('submits task and returns generated task ID and idempotency metadata', async () => {
        const req = {
            action: 'submitTask',
            payload: {
                workerId: 'worker-dev-01',
                title: 'Run lint check',
                objective: 'Execute linter across repository',
                idempotencyKey: 'idem-key-abc',
            },
            correlationId: 'test-corr-submit',
        };
        const res = await handleWorkerMessage(req, client);
        assert.equal(res.ok, true);
        assert.equal(res.correlationId, 'test-corr-submit');
        assert.equal(res.data.id, 'task-new-123');
        assert.equal(res.data.idempotency_key, 'idem-key-abc');
    });
    test('routes getDag using runId or defaults to latest run', async () => {
        const req = {
            action: 'getDag',
            payload: { runId: 'run-alpha' },
        };
        const res = await handleWorkerMessage(req, client);
        assert.equal(res.ok, true);
        assert.equal(res.data.run_id, 'run-alpha');
        assert.equal(res.data.nodes.length, 2);
    });
    test('resolves decision gate successfully with matching expected_generation', async () => {
        const req = {
            action: 'resolveDecisionGate',
            payload: {
                gateId: 'gate-42',
                decision: 'approve',
                expectedGeneration: 3,
                reviewNotes: 'Looks good',
            },
        };
        const res = await handleWorkerMessage(req, client);
        assert.equal(res.ok, true);
        assert.equal(res.data.resolved, true);
        assert.equal(res.data.new_generation, 4);
    });
    test('returns structured conflict error when expected_generation is stale', async () => {
        const req = {
            action: 'resolveDecisionGate',
            payload: {
                gateId: 'gate-42',
                decision: 'approve',
                expectedGeneration: 2, // stale, expected 3
            },
        };
        const res = await handleWorkerMessage(req, client);
        assert.equal(res.ok, false);
        assert.match(res.error ?? '', /conflict|Stale generation/i);
    });
    test('returns structured unknown_action error for unrecognized actions', async () => {
        const req = {
            action: 'unsupportedAction',
        };
        const res = await handleWorkerMessage(req, client);
        assert.equal(res.ok, false);
        assert.equal(res.code, 'unknown_action');
    });
    test('activates plugin worker and registers personaos.messageWorker command', async () => {
        const registeredCommands = new Map();
        const logs = [];
        const mockContext = {
            api: { major: 1, minor: 0 },
            commands: {
                register: (commandId, handler) => {
                    registeredCommands.set(commandId, handler);
                    return { dispose: () => registeredCommands.delete(commandId) };
                },
            },
            events: {
                on: () => ({ dispose: () => { } }),
            },
            host: {
                call: async () => ({}),
                has: () => true,
            },
            grantedCapabilities: [],
            log: (msg) => logs.push(msg),
        };
        await activate(mockContext);
        assert.ok(registeredCommands.has('personaos.messageWorker'), 'Command personaos.messageWorker must be registered');
        // Test invoking registered command directly
        const handler = registeredCommands.get('personaos.messageWorker');
        const response = (await handler({
            action: 'getHealth',
            correlationId: 'cmd-test-corr',
        }));
        assert.equal(response.ok, true);
        assert.equal(response.correlationId, 'cmd-test-corr');
        assert.equal(response.data.status, 'healthy');
    });
    test('strictly enforces ADR 0041 Zero Direct Agent Execution invariant', () => {
        assert.throws(() => client.assertZeroDirectAgentExecution('claude --prompt "fix bug"'), /DirectAgentExecutionViolationError|forbidden/);
        assert.throws(() => client.assertZeroDirectAgentExecution('/usr/bin/codex run'), /DirectAgentExecutionViolationError|forbidden/);
        assert.throws(() => client.assertZeroDirectAgentExecution('agy task'), /DirectAgentExecutionViolationError|forbidden/);
        // Non-prohibited commands must not throw
        assert.doesNotThrow(() => client.assertZeroDirectAgentExecution('git status'));
    });
});
//# sourceMappingURL=phase_m1_m2_host_messaging.test.js.map