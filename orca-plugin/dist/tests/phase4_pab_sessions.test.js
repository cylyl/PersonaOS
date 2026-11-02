/**
 * Phase 4 Test Suite: PAB Agent Execution & Session Visibility (Milestone 4 / Issue #4)
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { PersonaOSOrcaPluginClient, isExecutionEventStale, DirectAgentExecutionViolationError, } from '../index.js';
describe('Phase 4: PAB Agent Execution & Session Visibility', () => {
    it('retrieves active PAB execution session mapped to a PersonaOS task', async () => {
        const mockSession = {
            session_id: 'pab-sess-888',
            execution_id: 'exec-999',
            task_id: 'task-auth-01',
            provider: 'agy',
            agent_id: 'agent-dev-01',
            agent_profile: 'software-engineer',
            worktree_id: 'wt-01',
            worktree_path: '/root/orca/workspaces/app',
            status: 'active',
            started_at: '2026-11-01T14:00:00Z',
            last_heartbeat: '2026-11-01T14:05:00Z',
            metrics: {
                input_tokens: 1200,
                output_tokens: 450,
                total_tokens: 1650,
                cost_usd: 0.012,
                latency_ms: 3200,
            },
            generation: 2,
        };
        const mockFetch = async (url) => {
            assert.ok(String(url).includes('/integration/tasks/task-auth-01/session'));
            return new Response(JSON.stringify(mockSession), {
                status: 200,
                headers: { 'Content-Type': 'application/json' },
            });
        };
        const client = new PersonaOSOrcaPluginClient({
            apiBaseUrl: 'http://gateway.test',
            fetchFn: mockFetch,
        });
        const session = await client.getTaskExecutionSession('task-auth-01');
        assert.ok(session);
        assert.equal(session.session_id, 'pab-sess-888');
        assert.equal(session.provider, 'agy');
        assert.equal(session.metrics?.total_tokens, 1650);
        assert.equal(session.generation, 2);
    });
    it('returns null when task execution session is not found (404)', async () => {
        const mockFetch = async () => {
            return new Response(JSON.stringify({ detail: 'No active session' }), {
                status: 404,
                headers: { 'Content-Type': 'application/json' },
            });
        };
        const client = new PersonaOSOrcaPluginClient({
            apiBaseUrl: 'http://gateway.test',
            fetchFn: mockFetch,
        });
        const session = await client.getTaskExecutionSession('task-non-existent');
        assert.equal(session, null);
    });
    it('cancels task execution enforcing expected generation', async () => {
        let sentBody = null;
        const mockFetch = async (url, init) => {
            sentBody = JSON.parse(init?.body);
            return new Response(JSON.stringify({ ok: true }), {
                status: 200,
                headers: { 'Content-Type': 'application/json' },
            });
        };
        const client = new PersonaOSOrcaPluginClient({
            apiBaseUrl: 'http://gateway.test',
            fetchFn: mockFetch,
        });
        const res = await client.cancelTaskExecution('task-to-cancel', 3, 'Timeout exceeded');
        assert.equal(res.ok, true);
        assert.equal(sentBody.task_id, 'task-to-cancel');
        assert.equal(sentBody.expected_generation, 3);
        assert.equal(sentBody.reason, 'Timeout exceeded');
    });
    it('correctly detects stale execution events with isExecutionEventStale', () => {
        // Event generation 1 against current task generation 2 is STALE
        assert.equal(isExecutionEventStale(1, 2), true);
        // Event generation 2 against current task generation 2 is CURRENT
        assert.equal(isExecutionEventStale(2, 2), false);
        // Event generation 3 against current task generation 2 is VALID
        assert.equal(isExecutionEventStale(3, 2), false);
    });
    it('strictly prohibits direct execution of external agent CLI binaries from Orca ADE', () => {
        const client = new PersonaOSOrcaPluginClient();
        const prohibitedCommands = [
            'claude --prompt "write tests"',
            'codex run task-1',
            'agy -m "fix issue"',
            'openclaw session attach',
            '/usr/local/bin/claude',
            'gemini query',
            'cursor --agent',
        ];
        for (const cmd of prohibitedCommands) {
            assert.throws(() => client.assertZeroDirectAgentExecution(cmd), (err) => {
                assert.ok(err instanceof DirectAgentExecutionViolationError);
                return true;
            }, `Expected command '${cmd}' to be rejected by assertZeroDirectAgentExecution`);
        }
        // Sanctioned internal tools or non-agent commands should pass
        assert.doesNotThrow(() => client.assertZeroDirectAgentExecution('git status'));
        assert.doesNotThrow(() => client.assertZeroDirectAgentExecution('pytest tests/'));
        assert.doesNotThrow(() => client.assertZeroDirectAgentExecution('npm test'));
    });
});
//# sourceMappingURL=phase4_pab_sessions.test.js.map