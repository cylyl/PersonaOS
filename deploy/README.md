# PersonaOS v0.1 — Single-Machine Deployment

This directory holds the production-ish docker-compose for v0.1. In v0.1, the
control plane API and the worker run on the **same machine** as Postgres.
Cluster scheduling, failover, and capability-aware routing are deferred to
post-v0.1 (see `VIKUNJA.md`).

## Prerequisites

- Docker + Docker Compose
- A built `personaos:0.1.0` image (or use the development workflow in the
  repo root)

## Steps

1. Set the Postgres password in an environment file or your shell:

   ```bash
   export POSTGRES_PASSWORD="$(openssl rand -hex 24)"
   ```

2. Build the image once:

   ```bash
   docker build -t personaos:0.1.0 ..
   ```

3. Start the stack:

   ```bash
   docker compose -f deploy/docker-compose.yml up -d
   ```

4. Verify health:

   ```bash
   curl http://localhost:8000/health
   ```

## What runs where

| Service | Process | Port | Notes |
|---|---|---|---|
| `postgres` | Postgres 16 | 5432 | Authoritative state. |
| `api` | `personaos-api` (FastAPI via uvicorn) | 8000 | Control plane. |
| `worker` | `personaos-worker` (worker_main.py) | — | Worker loop. Same box as API. |

## Operational notes

- **Backups.** `pg_dump` the `personaos` database regularly. State is durable;
  events are append-only and can be rebuilt from the audit log if needed.
- **Logs.** All services log to stdout in structured JSON. Pipe to your log
  aggregator of choice.
- **Upgrades.** v0.1 → v0.2 migrations are Alembic. Run `make migrate` (or
  the equivalent inside the `api` container) before swapping the image.
- **Quarantine.** Workers can be quarantined via the API. The scheduler will
  skip them within one poll interval.

## What this is NOT

- Not a clustered deployment. Single API + single worker + single Postgres.
- Not high-availability. Postgres is a single node. No read replicas.
- Not multi-tenant. One organization per deployment in v0.1.

Those are post-v0.1 concerns (see `docs/adr/` and the README *Cluster Support*
section for the long-term direction).
