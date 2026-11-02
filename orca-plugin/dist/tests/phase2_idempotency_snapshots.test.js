/**
 * Phase 2 Test Suite: Idempotency, Worker Profile Snapshots & Side Effects (Milestone 2 / Issue #2)
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { PersonaOSOrcaPluginClient, validateWorkerCapabilities, } from '../index.js';
describe('Phase 2: Task Submission with Idempotency & Profile Snapshots', () => {
    it('generates a stable UUIDv4 idempotency key when not explicitly supplied', async () => {
        let sentBody = null;
        let sentHeaders = {};
        const mockFetch = async (url, init) => {
            sentHeaders = (init?.headers || {});
            sentBody = JSON.parse(init?.body);
            return new Response(JSON.stringify({ id: 'task-id-123', status: 'queued', profile_version: 1 }), {
                status: 201,
                headers: { 'Content-Type': 'application/json' },
            });
        };
        const client = new PersonaOSOrcaPluginClient({
            apiBaseUrl: 'http://gateway.test',
            fetchFn: mockFetch,
        });
        const resp = await client.submitTask({
            workerId: 'worker-dev-01',
            title: 'Implement OAuth provider',
            objective: 'Add GitHub SSO',
        });
        assert.equal(resp.id, 'task-id-123');
        assert.equal(resp.status, 'queued');
        assert.ok(resp.idempotency_key, 'Should return generated idempotency key');
        assert.equal(sentBody.idempotency_key, resp.idempotency_key);
        assert.equal(sentHeaders['Idempotency-Key'], resp.idempotency_key);
        assert.match(resp.idempotency_key, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
    });
    it('preserves caller-provided idempotencyKey and correlationId across submission and retries', async () => {
        let sentBody = null;
        let sentHeaders = {};
        const mockFetch = async (url, init) => {
            sentHeaders = (init?.headers || {});
            sentBody = JSON.parse(init?.body);
            return new Response(JSON.stringify({ id: 'task-id-456', status: 'queued', profile_version: 2 }), {
                status: 200,
                headers: { 'Content-Type': 'application/json' },
            });
        };
        const client = new PersonaOSOrcaPluginClient({
            apiBaseUrl: 'http://gateway.test',
            fetchFn: mockFetch,
        });
        const customKey = 'idemp-custom-key-999';
        const customCorr = 'corr-trace-111';
        const resp = await client.submitTask({
            workerId: 'worker-dev-01',
            title: 'Database migration',
            objective: 'Run migration 0041',
            idempotencyKey: customKey,
            correlationId: customCorr,
        });
        assert.equal(resp.idempotency_key, customKey);
        assert.equal(resp.correlation_id, customCorr);
        assert.equal(sentBody.idempotency_key, customKey);
        assert.equal(sentBody.correlation_id, customCorr);
        assert.equal(sentHeaders['Idempotency-Key'], customKey);
        assert.equal(sentHeaders['X-Correlation-ID'], customCorr);
    });
    it('pins immutable worker profile snapshot and propagates snapshot fields to payload', async () => {
        let sentBody = null;
        const mockFetch = async (url, init) => {
            sentBody = JSON.parse(init?.body);
            return new Response(JSON.stringify({ id: 'task-snap-001', status: 'queued', profile_version: 3 }), {
                status: 201,
                headers: { 'Content-Type': 'application/json' },
            });
        };
        const client = new PersonaOSOrcaPluginClient({
            apiBaseUrl: 'http://gateway.test',
            fetchFn: mockFetch,
        });
        const resp = await client.submitTask({
            workerId: 'worker-dev-01',
            title: 'Build unit tests',
            objective: '100% coverage',
            profileSnapshot: {
                profileId: 'profile-dev-v3',
                profileVersion: 3,
                requiredSkills: ['typescript', 'postgres', 'testing'],
                profileSnapshotRef: 'snap-hash-abc123',
            },
        });
        assert.equal(resp.profile_version, 3);
        assert.equal(sentBody.profile_id, 'profile-dev-v3');
        assert.equal(sentBody.profile_version, 3);
        assert.deepEqual(sentBody.required_skills, ['typescript', 'postgres', 'testing']);
        assert.equal(sentBody.profile_snapshot_ref, 'snap-hash-abc123');
    });
    it('validates worker capabilities against required skills', () => {
        const worker = {
            id: 'worker-dev-01',
            name: 'worker-dev-01',
            role: 'developer',
            status: 'idle',
            current_profile_version: 1,
            capabilities: ['python', 'typescript', 'postgres', 'git'],
            permissions: ['repo.read', 'repo.write'],
        };
        const matchResult = validateWorkerCapabilities(worker, ['typescript', 'git']);
        assert.equal(matchResult.compatible, true);
        assert.equal(matchResult.missingSkills.length, 0);
        const missingResult = validateWorkerCapabilities(worker, ['typescript', 'kubernetes', 'rust']);
        assert.equal(missingResult.compatible, false);
        assert.deepEqual(missingResult.missingSkills, ['kubernetes', 'rust']);
    });
    it('transmits explicit side-effect classification and active worktree binding', async () => {
        let sentBody = null;
        const mockFetch = async (url, init) => {
            sentBody = JSON.parse(init?.body);
            return new Response(JSON.stringify({ id: 'task-side-01', status: 'queued' }), {
                status: 201,
                headers: { 'Content-Type': 'application/json' },
            });
        };
        const client = new PersonaOSOrcaPluginClient({
            apiBaseUrl: 'http://gateway.test',
            fetchFn: mockFetch,
        });
        await client.submitTask({
            workerId: 'worker-dev-01',
            title: 'Run schema migration',
            objective: 'Apply schema changes',
            sideEffectClassification: 'HAS_SIDE_EFFECTS',
            worktreeId: 'wt-branch-99',
            worktreePath: '/root/orca/workspaces/app',
            branch: 'feature/db-v2',
            dependencies: ['task-prev-01', 'task-prev-02'],
        });
        assert.equal(sentBody.side_effect_classification, 'HAS_SIDE_EFFECTS');
        assert.equal(sentBody.input.worktree.worktree_id, 'wt-branch-99');
        assert.equal(sentBody.input.worktree.path, '/root/orca/workspaces/app');
        assert.equal(sentBody.input.worktree.branch, 'feature/db-v2');
        assert.deepEqual(sentBody.dependencies, ['task-prev-01', 'task-prev-02']);
    });
});
//# sourceMappingURL=phase2_idempotency_snapshots.test.js.map