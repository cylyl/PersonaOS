# PersonaOS — Production Deployment Guide

This document describes the architectural requirements, configuration reference, and step-by-step procedures for deploying PersonaOS across single-machine, containerized, and distributed multi-node production environments.

---

## 1. Architectural Overview & Deployment Topologies

PersonaOS is built as a modular system with strict separation between the **State Store** (PostgreSQL), the **Control Plane** (`personaos-api`), and the **Distributed Workforce** (`personaos-worker` and `personaos-worker-daemon`).

```mermaid
flowchart TD
    subgraph Clients["Clients & Operators"]
        LB["Layer 7 Reverse Proxy / Load Balancer\n(Nginx / Traefik / Caddy)"]
        CLI["CLI / Web Dashboard (/dashboard)"]
    end

    subgraph ControlPlane["Active-Active Control Plane Cluster"]
        CP1["Control Plane API Node 1\n(personaos-api)"]
        CP2["Control Plane API Node 2\n(personaos-api)"]
    end

    subgraph StateStore["Authoritative State Layer"]
        PG[("PostgreSQL 16+\n(Active Leases, SKIP LOCKED Queue,\nAdvisory Locks, Events Log)")]
        MEM0[("Vector Storage\n(Mem0 API / Self-hosted pgvector)")]
    end

    subgraph WorkerFleet["Distributed Worker Fleet"]
        WN1["Worker Node 1\n(personaos-worker)"]
        WN2["Worker Node 2\n(Remote Daemon)"]
        WN3["GPU / Compute Node 3\n(Remote Daemon)"]
    end

    subgraph Runtimes["Agent Runtimes & Sandboxes"]
        OC["OpenClaw ACP Gateway\n(Port 18789 / stdio)"]
        SANDBOX["POSIX Process Sandboxes\n(pgid isolated)"]
    end

    CLI --> LB
    LB -->|HTTPS / REST| CP1
    LB -->|HTTPS / REST| CP2
    LB -->|WSS / PMAEP| CP1
    LB -->|WSS / PMAEP| CP2

    CP1 -->|SQLAlchemy + asyncpg| PG
    CP2 -->|SQLAlchemy + asyncpg| PG
    CP1 -.->|Memory Sync| MEM0
    CP2 -.->|Memory Sync| MEM0

    WN1 -->|Atomic Claims / Leases| PG
    WN2 -->|WebSocket PMAEP / mTLS| CP1
    WN3 -->|WebSocket PMAEP / mTLS| CP2

    WN1 --> OC
    WN2 --> SANDBOX
    WN3 --> SANDBOX
```

### Supported Topologies

1. **Single-Node Deployment (PoC / Staging / Edge):**
   - PostgreSQL, Control Plane API, and Worker run on the same virtual machine or bare-metal host using Docker Compose or systemd.
   - Ideal for evaluation, development, and standalone appliance deployments.
2. **Distributed Clustered Deployment (Production):**
   - **PostgreSQL 16+**: High-availability cluster (Patroni / AWS RDS / GCP Cloud SQL) with connection pooling (PgBouncer).
   - **Control Plane Cluster**: 2+ stateless `personaos-api` instances behind a load balancer with WebSocket support, utilizing PostgreSQL transactional advisory locks (`advisory_xact_lock`) for singleton supervisory loops.
   - **Distributed Worker Fleet**: Heterogeneous remote worker machines running `personaos-worker-daemon` connecting over PMAEP (Persistent WebSocket with HMAC token validation and real-time checkpoint streaming).
3. **Kubernetes / K3s GitOps Deployment (Homelab / Hybrid Cloud):**
   - Runs directly on multi-node K3s clusters (such as `cyl-dc` on PVE2) using Longhorn distributed persistent storage, Traefik ingress with WebSocket forwarding, Jenkins CI/CD automation, and ArgoCD GitOps reconciliation.


---

## 2. System Prerequisites

### Hardware & OS Requirements

| Component | Minimum Specifications | Recommended Production |
|---|---|---|
| **Control Plane Node** | 2 vCPUs, 4 GB RAM, 20 GB SSD | 4 vCPUs, 8 GB RAM, 50 GB NVMe |
| **Worker Host Node** | 2 vCPUs, 4 GB RAM, 20 GB SSD | 8+ vCPUs, 16+ GB RAM, GPU (if required) |
| **PostgreSQL Database** | 2 vCPUs, 4 GB RAM, 50 GB SSD | 4+ vCPUs, 16+ GB RAM, IOPS >= 3000 |

- **Operating System:** Ubuntu 22.04 LTS / 24.04 LTS, Debian 12, or RHEL 9 (Linux kernel >= 6.5 recommended).
- **Python Runtime:** Python 3.12+ (when running bare-metal or systemd).
- **Container Engine:** Docker Engine >= 24.0 and Docker Compose v2 (when running containerized).
- **Network Ports:**
  - `8000/tcp`: Control Plane HTTP REST API and WebSocket Gateway (`/api/v1/cluster/ws`).
  - `5432/tcp`: PostgreSQL relational state store.
  - `18789/tcp`: Local OpenClaw ACP Gateway (loopback/internal only).

---

## 3. Configuration Reference

PersonaOS is configured via environment variables or a `.env` file loaded at application startup via Pydantic Settings.

### Database Settings
| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://personaos:personaos@localhost:5432/personaos` | Asynchronous PostgreSQL connection string. Must use `postgresql+asyncpg://`. |
| `DATABASE_POOL_SIZE` | `5` | Base connection pool size per process. |
| `DATABASE_MAX_OVERFLOW` | `10` | Maximum overflow connections allowed above pool size. |

### Control Plane API Settings
| Variable | Default | Description |
|---|---|---|
| `API_HOST` | `0.0.0.0` | Bind IP for the control plane. |
| `API_PORT` | `8000` | Bind port for the control plane. |
| `OPERATOR_TOKEN` | `None` | Optional Bearer authentication token for admin endpoints. In trusted VPC environments, leave unset. |
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `LOG_FORMAT` | `json` | Log format: `json` for production structured logging, `console` for local readability. |

### Cluster Fencing & Node Supervision
| Variable | Default | Description |
|---|---|---|
| `ENABLE_NODE_SUPERVISION` | `true` | Enables background dead-node eviction and task quarantine loops. |
| `NODE_SUPERVISION_INTERVAL_SECONDS` | `10.0` | Interval between cluster supervision sweeps. |
| `NODE_HEARTBEAT_TIMEOUT_SECONDS` | `60.0` | Heartbeat inactivity threshold before a node is marked dead. |
| `TASK_LEASE_TTL_SECONDS` | `300` | Lease duration for claimed tasks before expiration. |

### Memory & Vector Subsystem
| Variable | Default | Description |
|---|---|---|
| `MEMORY_ADAPTER` | `in_memory` | Memory backend: `in_memory` (local volatile) or `mem0` (persistent vector store). |
| `MEM0_API_KEY` | `None` | API key for Mem0 cloud or self-hosted endpoint. |
| `MEM0_BASE_URL` | `https://api.mem0.ai/v1` | Base URL for Mem0 vector storage service. |
| `MEMORY_MIN_CONFIDENCE` | `0.0` | Minimum confidence score threshold [0.0 - 1.0] for memory context injection. |

### Security & Worker Execution
| Variable | Default | Description |
|---|---|---|
| `POLICIES_DIR` | `None` | Path to directory containing `policy.yaml` files. |
| `PROFILES_DIR` | `None` | Path to directory containing worker profile YAML manifests. |
| `PERSONAOS_RUN_LIVE_OPENCLAW_TESTS` | `0` | Set to `1` only when a live OpenClaw gateway is running on port 18789. |

---

## 4. Method 1: Containerized Production Deployment (Docker Compose)

### 4.1 Production Dockerfile

Create `Dockerfile` in the root of the repository:

```dockerfile
# Multi-stage production build for PersonaOS
FROM python:3.12-slim-bookworm AS builder

WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install build tools
RUN pip install --no-cache-dir --upgrade pip hatchling uv

# Copy project definition and source
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Build wheel package
RUN uv pip build --wheel --out-dir /build/dist

# Final minimal runtime image
FROM python:3.12-slim-bookworm AS runtime

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Create unprivileged system user
RUN groupadd -g 10001 personaos && \
    useradd -u 10001 -g personaos -m -s /bin/bash personaos

# Copy and install built wheel
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -rf /tmp/*.whl

# Copy migrations and default configurations
COPY src/personaos/db/migrations/ /app/migrations/
COPY alembic.ini /app/alembic.ini
COPY policies/ /app/policies/
COPY profiles/ /app/profiles/

RUN chown -R personaos:personaos /app
USER personaos

EXPOSE 8000
CMD ["personaos-api"]
```

### 4.2 Production Docker Compose Stack

Deploy via `deploy/docker-compose.yml`:

```yaml
version: "3.8"

services:
  postgres:
    image: postgres:16-alpine
    container_name: personaos-postgres
    restart: unless-stopped
    environment:
      POSTGRES_DB: personaos
      POSTGRES_USER: personaos
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD in .env}
    ports:
      - "127.0.0.1:5432:5432"
    volumes:
      - personaos_pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U personaos -d personaos"]
      interval: 10s
      timeout: 3s
      retries: 5
    deploy:
      resources:
        limits:
          cpus: "4.0"
          memory: 4096M

  api:
    build:
      context: ..
      dockerfile: Dockerfile
    image: personaos:latest
    container_name: personaos-api
    restart: unless-stopped
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      DATABASE_URL: postgresql+asyncpg://personaos:${POSTGRES_PASSWORD}@postgres:5432/personaos
      API_HOST: 0.0.0.0
      API_PORT: 8000
      LOG_LEVEL: INFO
      LOG_FORMAT: json
      ENABLE_NODE_SUPERVISION: "true"
      MEMORY_ADAPTER: ${MEMORY_ADAPTER:-in_memory}
      MEM0_API_KEY: ${MEM0_API_KEY:-}
    ports:
      - "8000:8000"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 15s
      timeout: 5s
      retries: 3
    command: ["sh", "-c", "alembic upgrade head && personaos-api"]

  worker:
    image: personaos:latest
    container_name: personaos-worker
    restart: unless-stopped
    depends_on:
      api:
        condition: service_healthy
    environment:
      DATABASE_URL: postgresql+asyncpg://personaos:${POSTGRES_PASSWORD}@postgres:5432/personaos
      PERSONAOS_WORKER_ID: "worker-prod-01"
      LOG_LEVEL: INFO
      LOG_FORMAT: json
      WORKER_POLL_INTERVAL_SECONDS: "2.0"
      TASK_LEASE_TTL_SECONDS: "300"
    command: ["personaos-worker", "--worker-id", "worker-prod-01"]

volumes:
  personaos_pgdata:
```

### 4.3 Running the Stack

```bash
# 1. Generate secrets
echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)" > .env

# 2. Build and launch
docker compose -f deploy/docker-compose.yml up -d --build

# 3. Check health status
docker compose -f deploy/docker-compose.yml ps
curl -s http://localhost:8000/health | jq
```

---

## 5. Method 2: Systemd Linux Service Deployment (Bare Metal / VM)

Running PersonaOS directly under `systemd` ensures native OS performance, cgroup resource limits, and deterministic process lifetime management.

### 5.1 System User & Virtual Environment Setup

```bash
# Create system user and directory tree
sudo useradd -r -s /bin/false -d /opt/personaos personaos
sudo mkdir -p /opt/personaos /etc/personaos /var/log/personaos
sudo chown -R personaos:personaos /opt/personaos /etc/personaos /var/log/personaos

# Install virtualenv using uv or python
sudo -u personaos python3.12 -m venv /opt/personaos/venv
sudo -u personaos /opt/personaos/venv/bin/pip install --upgrade pip
sudo -u personaos /opt/personaos/venv/bin/pip install /path/to/personaos-*.whl
```

### 5.2 Environment Configuration File (`/etc/personaos/personaos.env`)

```ini
DATABASE_URL=postgresql+asyncpg://personaos:SecurePassword123@10.0.1.10:5432/personaos
API_HOST=0.0.0.0
API_PORT=8000
LOG_LEVEL=INFO
LOG_FORMAT=json
ENABLE_NODE_SUPERVISION=true
NODE_SUPERVISION_INTERVAL_SECONDS=10.0
NODE_HEARTBEAT_TIMEOUT_SECONDS=60.0
TASK_LEASE_TTL_SECONDS=300
MEMORY_ADAPTER=in_memory
POLICIES_DIR=/etc/personaos/policies
PROFILES_DIR=/etc/personaos/profiles
```

### 5.3 Control Plane Systemd Unit (`/etc/systemd/system/personaos-api.service`)

```ini
[Unit]
Description=PersonaOS Control Plane API
After=network.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=personaos
Group=personaos
WorkingDirectory=/opt/personaos
EnvironmentFile=/etc/personaos/personaos.env
ExecStart=/opt/personaos/venv/bin/personaos-api
Restart=always
RestartSec=5s

# Security Hardening
ProtectSystem=strict
ProtectHome=true
NoNewPrivileges=true
PrivateTmp=true
ProtectKernelTunables=true
ProtectControlGroups=true
ReadWritePaths=/var/log/personaos

# Resource Limits
LimitNOFILE=65535
MemoryMax=4G
CPUQuota=400%

[Install]
WantedBy=multi-user.target
```

### 5.4 Worker Systemd Unit (`/etc/systemd/system/personaos-worker@.service`)

Allows scaling multiple workers via template instances (e.g. `personaos-worker@1`, `personaos-worker@2`):

```ini
[Unit]
Description=PersonaOS Worker Instance %i
After=network.target personaos-api.service

[Service]
Type=simple
User=personaos
Group=personaos
WorkingDirectory=/opt/personaos
EnvironmentFile=/etc/personaos/personaos.env
ExecStart=/opt/personaos/venv/bin/personaos-worker --worker-id worker-prod-%i --node-id node-%H-%i
Restart=always
RestartSec=5s

# Process Sandbox Hardening
KillMode=mixed
TimeoutStopSec=60s
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Enable and start services:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now personaos-api
sudo systemctl enable --now personaos-worker@1
sudo systemctl enable --now personaos-worker@2
```

---

## 6. Method 3: Distributed Multi-Node Cluster Setup (v0.2 PMAEP)

In a distributed v0.2 setup, execution nodes run across separate servers communicating with the control plane over WebSocket (PMAEP).

### 6.1 Nginx Ingress Configuration (SSL Termination & WebSocket Support)

Deploy Nginx on the edge router or load balancer:

```nginx
upstream personaos_control_plane {
    least_conn;
    server 10.0.1.21:8000 max_fails=3 fail_timeout=10s;
    server 10.0.1.22:8000 max_fails=3 fail_timeout=10s;
}

server {
    listen 443 ssl http2;
    server_name personaos.internal.domain;

    ssl_certificate /etc/ssl/certs/personaos.crt;
    ssl_certificate_key /etc/ssl/private/personaos.key;
    ssl_protocols TLSv1.3;

    client_max_body_size 50M;

    # Standard REST API and Operator Dashboard
    location / {
        proxy_pass http://personaos_control_plane;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Cross-Machine Agent Execution Protocol (PMAEP) WebSocket Gateway
    location /api/v1/cluster/ws {
        proxy_pass http://personaos_control_plane;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        # Keep connection open for long-lived daemon streams
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }
}
```

### 6.2 Running Remote Worker Daemon on Execution Hosts

On each remote worker machine, run the autonomous daemon:

```python
import asyncio
from personaos.worker_daemon.config import DaemonConfig
from personaos.worker_daemon.daemon import WorkerDaemon
from personaos.db.session import make_session_factory, make_engine

async def run_remote_daemon():
    engine = make_engine("postgresql+asyncpg://personaos:pass@db.internal:5432/personaos")
    session_factory = make_session_factory(engine)

    config = DaemonConfig(
        worker_id="worker-compute-gpu-01",
        heartbeat_interval=5.0,
        task_lease_ttl=30.0,
        extra_capabilities={"accelerator": "nvidia-a100", "cuda_version": "12.4"},
    )

    daemon = WorkerDaemon(config=config, session_factory=session_factory)
    await daemon.start()

    # Wait for OS termination signal
    try:
        while True:
            await asyncio.sleep(1)
    except (asyncio.CancelledError, KeyboardInterrupt):
        await daemon.drain(reason="runner_shutdown")

if __name__ == "__main__":
    asyncio.run(run_remote_daemon())
```

---

## 7. Method 4: K3s Cluster Deployment with Infra CI/CD & ArgoCD on PVE2 (`cyl-dc`)

PersonaOS integrates seamlessly with existing Kubernetes infrastructure such as the dual-site K3s cluster managed in `cyl-dc`. In this architecture:
- **Site 1 (PVE1):** Hosts cluster management and primary nodes (`k3s01`, `k3s02`).
- **Site 2 (PVE2):** Hosts dedicated compute and execution nodes (`k3s03`, `k3s04`), labeled `site: site2`.
- **Target Node Pinning:** All PersonaOS workloads (control plane, worker daemon, and database) use `nodeSelector: site: site2` to guarantee execution strictly on the PVE2 hypervisor.
- **Storage Subsystem:** Backed by Longhorn distributed block storage (`storageClassName: longhorn`).
- **Local Insecure Registry:** `192.168.1.239:5000/personaos` provides high-speed, local container image distribution.
- **GitOps & CI/CD:** Continuous delivery is driven by Jenkins (`Jenkinsfile`) and reconciled declaratively by ArgoCD (`argoproj.io/v1alpha1 Application`).

```mermaid
flowchart TD
    subgraph GitRepo["Git Repository & Jenkins CI"]
        GIT["Git Commit\n(joseph118789-max/yabby)"]
        JENK["Jenkins Pipeline\n(port 8084)"]
        REG[("Local Registry\n192.168.1.239:5000")]
        GIT -->|Webhook / Poll| JENK
        JENK -->|docker build| JENK
        JENK -->|docker push| REG
    end

    subgraph Cluster["K3s Cluster (cyl-dc)"]
        subgraph PVE1["Site 1 / PVE1 (Cluster Control)"]
            K1["k3s01 (Master)"]
            ARGO["ArgoCD Controller"]
        end

        subgraph PVE2["Site 2 / PVE2 (PersonaOS Compute: site=site2)"]
            subgraph K3s03["Node: k3s03"]
                CP1["personaos-api Pod 1\n(port 8000)"]
                W1["personaos-worker Pod 1"]
                PG["PostgreSQL 16 StatefulSet"]
            end
            subgraph K3s04["Node: k3s04"]
                CP2["personaos-api Pod 2\n(port 8000)"]
                W2["personaos-worker Pod 2"]
            end
            LH[("Longhorn Distributed Storage\n(PVC: personaos-db-data)")]
        end

        TRF["Traefik Ingress Controller\n(HTTP & WebSocket Upgrade)"]
    end

    JENK -->|kubectl apply / rollout restart| K1
    ARGO -->|Sync Manifests| PVE2
    PG --> LH
    TRF -->|/api/v1/cluster/ws (WebSocket)| CP1
    TRF -->|/api/v1/cluster/ws (WebSocket)| CP2
    TRF -->|/dashboard & REST| CP1
    TRF -->|/dashboard & REST| CP2
    CP1 --> PG
    CP2 --> PG
    W1 --> PG
    W2 --> PG
```

### 7.1 Kubernetes Manifest Architecture (`deploy/k8s/`)

The Kubernetes manifests are organized for modular management via Kustomize:

| Manifest | Kind | Purpose |
|---|---|---|
| `00-namespace.yaml` | `Namespace` | Creates `personaos` namespace labeled `site: site2`. |
| `01-secret.yaml` | `Secret` | Stores database credentials and HMAC `CLUSTER_SECRET_KEY`. |
| `02-configmap.yaml` | `ConfigMap` | Runtime configuration (`API_PORT`, supervision, memory settings). |
| `03-postgres-pvc.yaml` | `PersistentVolumeClaim` | 20Gi volume using `storageClassName: longhorn`. |
| `04-postgres.yaml` | `StatefulSet` + `Service` | PostgreSQL 16 pinned to `site: site2` with healthcheck probes. |
| `05-control-plane.yaml` | `Deployment` + `Service` | 2-replica `personaos-api` with Alembic migration `initContainer` and rolling updates. |
| `06-worker.yaml` | `Deployment` | 2-replica `personaos-worker` using Downward API for pod identity. |
| `07-ingress.yaml` | `Ingress` | Traefik ingress routing `/`, `/dashboard`, `/health`, and `/api/v1/cluster/ws`. |
| `08-network-policy.yaml` | `NetworkPolicy` | Restricts database access strictly to pods in `personaos` namespace. |
| `10-argocd-application.yaml` | `Application` (ArgoCD) | Declarative GitOps synchronization with auto-prune and self-healing. |
| `kustomization.yaml` | `Kustomization` | Bundles resources and applies standard `app.kubernetes.io` metadata. |

### 7.2 Infra CI/CD Pipeline (`Jenkinsfile`)

The declarative Jenkins pipeline automates the full build, publish, and rollout lifecycle:

```groovy
pipeline {
    agent any
    environment {
        REGISTRY = '192.168.1.239:5000'
        IMAGE_NAME = 'personaos'
        IMAGE_FULL = "${REGISTRY}/${IMAGE_NAME}"
        K3S_MASTER = '192.168.1.150'
    }
    stages {
        stage('Compute Image Tag') {
            steps {
                script {
                    env.GIT_COMMIT_SHORT = sh(script: "git rev-parse --short HEAD", returnStdout: true).trim()
                    env.IMAGE_TAG = "${env.BUILD_NUMBER}-${env.GIT_COMMIT_SHORT}"
                }
            }
        }
        stage('Build Docker Image') {
            steps {
                sh "docker build -t ${IMAGE_FULL}:${IMAGE_TAG} -t ${IMAGE_FULL}:latest -f Dockerfile ."
            }
        }
        stage('Push Docker Image to Local Registry') {
            steps {
                sh "docker push ${IMAGE_FULL}:${IMAGE_TAG} && docker push ${IMAGE_FULL}:latest"
            }
        }
        stage('Deploy to K3s Cluster (Site 2 / PVE2)') {
            steps {
                sh """
                    ssh -o StrictHostKeyChecking=no user@${K3S_MASTER} "\
                        sudo kubectl apply -f - << 'EOF'
$(cat deploy/k8s/00-namespace.yaml)
---
$(cat deploy/k8s/01-secret.yaml)
---
$(cat deploy/k8s/02-configmap.yaml)
---
$(cat deploy/k8s/03-postgres-pvc.yaml)
---
$(cat deploy/k8s/04-postgres.yaml)
---
$(cat deploy/k8s/05-control-plane.yaml)
---
$(cat deploy/k8s/06-worker.yaml)
---
$(cat deploy/k8s/07-ingress.yaml)
---
$(cat deploy/k8s/08-network-policy.yaml)
EOF
                    "
                    ssh -o StrictHostKeyChecking=no user@${K3S_MASTER} "\
                        sudo kubectl rollout restart -n personaos deployment/personaos-api && \
                        sudo kubectl rollout restart -n personaos deployment/personaos-worker"
                """
            }
        }
        stage('Trigger ArgoCD Sync') {
            steps {
                sh """
                    ssh -o StrictHostKeyChecking=no user@${K3S_MASTER} "\
                        sudo kubectl patch application -n argocd personaos --type merge \
                        -p '{\\"metadata\\":{\\"annotations\\":{\\"argocd.argoproj.io/refresh\\":\\"hard\\"}}}' 2>/dev/null || true"
                """
            }
        }
        stage('Verify Deployment Health') {
            steps {
                sh """
                    ssh -o StrictHostKeyChecking=no user@${K3S_MASTER} "\
                        sudo kubectl rollout status -n personaos deployment/personaos-api --timeout=120s"
                """
            }
        }
    }
}
```

### 7.3 Manual Deployment & Verification Commands

To deploy or verify manually against the K3s cluster:

```bash
# 1. Validate manifests via client-side dry run
kubectl apply -k deploy/k8s --dry-run=client

# 2. Deploy all resources to the cluster
kubectl apply -k deploy/k8s

# 3. Register the ArgoCD Application (optional if using GitOps sync)
kubectl apply -f deploy/k8s/10-argocd-application.yaml

# 4. Verify pod placement on Site 2 (PVE2 nodes k3s03 / k3s04)
kubectl get pods -n personaos -o wide

# 5. Check API rollout status and health endpoint
kubectl rollout status -n personaos deployment/personaos-api
curl -s http://192.168.1.150/health | jq
```

---

## 8. Database Migrations & Zero-Downtime Upgrades

All database changes in PersonaOS are governed by Alembic. Historical migrations must remain lossless and backward-compatible.

### 8.1 Applying Migrations

Before launching or upgrading control plane containers, execute Alembic migrations:

```bash
# Apply all pending migrations up to head
alembic upgrade head

# Inspect migration history
alembic current
alembic history --verbose
```

### 8.2 Zero-Downtime Rolling Update Strategy

1. **Pre-Deployment:** Run non-destructive database migrations (e.g. adding nullable columns or new tables like `control_plane_nodes`). Existing running control plane instances will ignore unknown columns.
2. **Rolling Replace API Nodes:** Incrementally update `personaos-api` instances behind the load balancer one node at a time. The health check (`GET /health`) guarantees readiness before traffic is routed.
3. **Drain Worker Nodes:** Signal workers to drain active tasks:
   ```bash
   curl -X POST http://personaos.internal/api/v1/cluster/nodes/node-host-01/drain \
     -H 'Content-Type: application/json' \
     -d '{"reason": "upgrade_maintenance"}'
   ```
4. **Restart Worker Nodes:** After active tasks complete, swap worker binaries or container images.

---

## 9. Backup & Disaster Recovery

### 9.1 PostgreSQL Backup Procedure

State in PersonaOS is authoritative in PostgreSQL; `task_events` is an immutable append-only ledger that preserves full execution provenance.

```bash
# Automated Daily Backup Script
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/var/backups/personaos"
mkdir -p "$BACKUP_DIR"

# Perform compressed custom-format pg_dump
PGPASSWORD="$POSTGRES_PASSWORD" pg_dump \
  -h 127.0.0.1 \
  -U personaos \
  -F c \
  -b -v \
  -f "$BACKUP_DIR/personaos_$DATE.dump" \
  personaos

# Retain backups for 30 days
find "$BACKUP_DIR" -type f -name "*.dump" -mtime +30 -delete
```

### 9.2 Disaster Recovery & Database Restoration

```bash
# Restore to a clean database
PGPASSWORD="$POSTGRES_PASSWORD" pg_restore \
  -h 127.0.0.1 \
  -U personaos \
  -d personaos \
  -v \
  -c "$BACKUP_DIR/personaos_20261029.dump"

# Re-verify schema consistency
alembic current
```

---

## 10. Observability, Health Checks & Day-2 Operations

### 10.1 Health Check Endpoints

| Endpoint | Method | Purpose | Response |
|---|---|---|---|
| `/health` | `GET` | Readiness & Liveness probe | `{"status": "ok", "database": "connected"}` |
| `/api/v1/cluster/nodes` | `GET` | Cluster membership status | List of active, draining, or dead nodes |
| `/dashboard` | `GET` | Single-page operator console | Interactive HTML UI |
| `/api/v1/telemetry/quality` | `GET` | Task quality scores | Aggregated benchmark quality |
| `/api/v1/telemetry/cost` | `GET` | Token expenditure & pricing | Cost per model and task type |

### 10.2 Operator Incident Response Playbook

#### Incident 1: Worker Process Crashes Mid-Execution
- **Behavior:** The task lease (`lock_expires_at`) lapses after `TASK_LEASE_TTL_SECONDS`.
- **Automatic Recovery:** `NodeSupervisor.evict_dead_nodes` detects the expired lease.
  - If step is marked `IDEMPOTENT`, lock expires immediately and a surviving worker reclaims the task at its last stored checkpoint (`tasks.checkpoint_ref`).
  - If step is `UNKNOWN` or `HAS_SIDE_EFFECTS`, task transitions to `status = 'review'` with reason `unhandled_runtime_error` to protect against duplicate external mutations.

#### Incident 2: Quarantining a Misbehaving Worker
```bash
curl -X POST http://localhost:8000/workers/worker-prod-01/quarantine \
  -H 'Content-Type: application/json' \
  -d '{"reason": "suspected credential leakage", "quarantined_by": "secops-alice"}'
```
The scheduler immediately bypasses this worker on its next atomic poll cycle.

#### Incident 3: Resolving Tasks in Review Queue
```bash
# View tasks pending manual approval/resolution
curl -s 'http://localhost:8000/tasks?status=review' | jq

# Approve and resume
curl -X POST http://localhost:8000/tasks/task-uuid-here/approve \
  -H 'Content-Type: application/json' \
  -d '{"approver": "secops-alice"}'
```
