# PersonaOS — Deployment

This directory contains the Docker Compose stack for running PersonaOS.

> [!NOTE]
> For the comprehensive, production-grade guide covering multi-node active-active control planes, systemd Linux services, reverse proxy configuration with WebSocket (PMAEP), disaster recovery, and security hardening, see **[`docs/deployment.md`](../docs/deployment.md)**.

## Quickstart (Single-Machine Compose)

In this baseline topology, the PostgreSQL database, the Control Plane API (`personaos-api`), and the worker process (`personaos-worker`) run together on a single host.

### Prerequisites

- Docker Engine >= 24.0
- Docker Compose v2+

### Running the Stack

1. **Configure Secrets:**
   ```bash
   export POSTGRES_PASSWORD="$(openssl rand -hex 24)"
   ```

2. **Build the Production Image:**
   ```bash
   docker build -t personaos:0.1.0 .
   ```

3. **Start the Stack:**
   ```bash
   docker compose -f deploy/docker-compose.yml up -d
   ```

4. **Verify Health:**
   ```bash
   curl http://localhost:8000/health
   ```

## Architecture & Ports

| Service | Container | Internal Port | Host Port | Purpose |
|---|---|---|---|---|
| `postgres` | `personaos-postgres` | `5432` | `5432` (bind to localhost) | Relational state, atomic locks, event audit log |
| `api` | `personaos-api` | `8000` | `8000` | Control plane REST API, PMAEP WebSocket gateway, Dashboard |
| `worker` | `personaos-worker` | — | — | Main task execution loop and local node supervision |

## Advanced & Multi-Node Topologies

For advanced setups introduced in v0.2:
- **Active-Active Control Plane Clusters:** See [`docs/specs/multi-node-control-plane-distribution.md`](../docs/specs/multi-node-control-plane-distribution.md).
- **Remote Worker Daemons & Jittered Heartbeats:** See [`docs/specs/remote-worker-daemon-heartbeat-distribution.md`](../docs/specs/remote-worker-daemon-heartbeat-distribution.md).
- **Cross-Machine Agent Execution Protocol (PMAEP):** See [`docs/specs/cross-machine-agent-execution-protocol.md`](../docs/specs/cross-machine-agent-execution-protocol.md).
- **Proxmox VE 2 (PVE2) IaC Deployment:** See [`deploy/iac/README.md`](iac/README.md).
- **Production Operations & Hardening:** See [`docs/deployment.md`](../docs/deployment.md).
