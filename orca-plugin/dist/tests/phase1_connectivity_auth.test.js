/**
 * Phase 1 Test Suite: Gateway/BFF Connectivity, Authentication & Diagnostics (Milestone 1 / Issue #1)
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { PersonaOSOrcaPluginClient, AuthenticationError, NetworkError, ConnectionDiagnosticCode, } from '../index.js';
describe('Phase 1: Gateway/BFF Connectivity & Authentication', () => {
    it('resolves base URL correctly with trailing slash stripping', () => {
        const client1 = new PersonaOSOrcaPluginClient('http://192.168.1.239:8000///');
        assert.equal(client1.getBaseUrl(), 'http://192.168.1.239:8000');
        const client2 = new PersonaOSOrcaPluginClient({ apiBaseUrl: 'http://gateway.internal:9000/' });
        assert.equal(client2.getBaseUrl(), 'http://gateway.internal:9000');
        const client3 = new PersonaOSOrcaPluginClient();
        assert.equal(client3.getBaseUrl(), 'http://localhost:8000');
    });
    it('updates base URL and token dynamically', () => {
        const client = new PersonaOSOrcaPluginClient();
        client.setBaseUrl('http://new-gateway:8080/');
        assert.equal(client.getBaseUrl(), 'http://new-gateway:8080');
        client.setToken('test-token-123');
    });
    it('propagates Authorization Bearer header from static token', async () => {
        let capturedHeaders = {};
        const mockFetch = async (url, init) => {
            capturedHeaders = (init?.headers || {});
            return new Response(JSON.stringify({ status: 'healthy', gateway_version: '1.0.0', personaos_connected: true, personaos_url: 'http://0.0.0.0:8000', pab_connected: true, timestamp: new Date().toISOString() }), {
                status: 200,
                headers: { 'Content-Type': 'application/json' },
            });
        };
        const client = new PersonaOSOrcaPluginClient({
            apiBaseUrl: 'http://gateway.test',
            token: 'jwt-token-xyz',
            fetchFn: mockFetch,
        });
        const health = await client.getHealth();
        assert.equal(health.status, 'healthy');
        assert.equal(capturedHeaders['Authorization'], 'Bearer jwt-token-xyz');
    });
    it('resolves Authorization header from async tokenProvider for SSO/Keycloak refresh', async () => {
        let capturedHeaders = {};
        let callCount = 0;
        const mockFetch = async (url, init) => {
            capturedHeaders = (init?.headers || {});
            return new Response(JSON.stringify({ status: 'healthy', gateway_version: '1.0.0', personaos_connected: true, personaos_url: 'http://0.0.0.0:8000', pab_connected: true, timestamp: new Date().toISOString() }), {
                status: 200,
                headers: { 'Content-Type': 'application/json' },
            });
        };
        const client = new PersonaOSOrcaPluginClient({
            apiBaseUrl: 'http://gateway.test',
            tokenProvider: async () => {
                callCount++;
                return `dynamic-token-${callCount}`;
            },
            fetchFn: mockFetch,
        });
        await client.getHealth();
        assert.equal(callCount, 1);
        assert.equal(capturedHeaders['Authorization'], 'Bearer dynamic-token-1');
        await client.getHealth();
        assert.equal(callCount, 2);
        assert.equal(capturedHeaders['Authorization'], 'Bearer dynamic-token-2');
    });
    it('throws AuthenticationError on HTTP 401 and 403', async () => {
        const mockFetch401 = async () => {
            return new Response(JSON.stringify({ detail: 'Token expired or invalid signature' }), {
                status: 401,
                statusText: 'Unauthorized',
                headers: { 'Content-Type': 'application/json' },
            });
        };
        const client = new PersonaOSOrcaPluginClient({
            apiBaseUrl: 'http://gateway.test',
            fetchFn: mockFetch401,
        });
        await assert.rejects(async () => client.getHealth(), (err) => {
            assert.ok(err instanceof AuthenticationError);
            assert.equal(err.statusCode, 401);
            assert.equal(err.diagnosticCode, ConnectionDiagnosticCode.AUTHENTICATION_FAILED);
            assert.match(err.message, /Token expired/);
            return true;
        });
    });
    it('retries read-only GET requests on transient network failures', async () => {
        let attempts = 0;
        const mockFetch = async () => {
            attempts++;
            if (attempts < 2) {
                throw new TypeError('Failed to fetch (network drop)');
            }
            return new Response(JSON.stringify({ status: 'healthy', gateway_version: '1.0.0', personaos_connected: true, personaos_url: 'http://0.0.0.0:8000', pab_connected: true, timestamp: new Date().toISOString() }), {
                status: 200,
                headers: { 'Content-Type': 'application/json' },
            });
        };
        const client = new PersonaOSOrcaPluginClient({
            apiBaseUrl: 'http://gateway.test',
            maxRetries: 2,
            retryDelayMs: 10,
            fetchFn: mockFetch,
        });
        const health = await client.getHealth();
        assert.equal(health.status, 'healthy');
        assert.equal(attempts, 2);
    });
    it('never retries write requests (POST) on network error', async () => {
        let attempts = 0;
        const mockFetch = async () => {
            attempts++;
            throw new TypeError('Network timeout on submission');
        };
        const client = new PersonaOSOrcaPluginClient({
            apiBaseUrl: 'http://gateway.test',
            maxRetries: 3,
            fetchFn: mockFetch,
        });
        await assert.rejects(async () => client.submitTask({ workerId: 'w-1', title: 'Task', objective: 'Do work' }), (err) => {
            assert.ok(err instanceof NetworkError);
            return true;
        });
        assert.equal(attempts, 1);
    });
    it('checkConnection returns structured report even when network fails', async () => {
        const mockFetch = async () => {
            throw new Error('Connection refused (port closed)');
        };
        const client = new PersonaOSOrcaPluginClient({
            apiBaseUrl: 'http://unreachable-host:9999',
            fetchFn: mockFetch,
        });
        const diag = await client.checkConnection();
        assert.equal(diag.code, ConnectionDiagnosticCode.NETWORK_UNREACHABLE);
        assert.equal(diag.status, 'error');
        assert.equal(diag.cors_supported, false);
        assert.match(diag.remediation || '', /Verify PersonaOS Integration Gateway is reachable/);
    });
    it('getDiagnostics passes query params and maps upstream diagnostics', async () => {
        const mockFetch = async (url) => {
            assert.ok(String(url).includes('target_url=http%3A%2F%2Ftarget.node%3A8000'));
            return new Response(JSON.stringify({
                code: ConnectionDiagnosticCode.CONNECTED,
                status: 'ok',
                latency_ms: 2.5,
                upstream_url: 'http://target.node:8000',
                cors_supported: true,
                pna_supported: true,
                auth_status: 'authenticated',
                api_version_match: true,
                details: { database: 'ok' },
            }), {
                status: 200,
                headers: { 'Content-Type': 'application/json' },
            });
        };
        const client = new PersonaOSOrcaPluginClient({
            apiBaseUrl: 'http://gateway.test',
            fetchFn: mockFetch,
        });
        const report = await client.getDiagnostics('http://target.node:8000');
        assert.equal(report.code, ConnectionDiagnosticCode.CONNECTED);
        assert.equal(report.status, 'ok');
        assert.equal(report.latency_ms, 2.5);
    });
});
//# sourceMappingURL=phase1_connectivity_auth.test.js.map