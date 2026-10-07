# PersonaOS v0.1 — Operations

Local development and operational notes for the kernel.

## Local development

```bash
make up               # Start Postgres in the background
make migrate          # Apply Alembic migrations
make seed             # Load example workers from examples/
make test             # Run unit + integration + e2e tests
make lint             # Run ruff
make typecheck        # Run mypy
make format           # Auto-format
```

The development workflow assumes `uv` is installed. If you prefer raw pip:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Running the API

```bash
uv run personaos-api
# or
uv run uvicorn personaos.api.app:app --reload
```

- API: <http://localhost:8000>
- OpenAPI: <http://localhost:8000/docs>
- Health: `GET /health`

The API is the control plane. It does **not** execute tasks — that's the worker's job.

## Running a worker

```bash
uv run personaos-worker
# or
uv run python -m personaos.worker_main
```

The worker process:

1. Registers itself with the control plane (emits `node.joined`).
2. Polls for tasks matching its worker profile.
3. Runs the main loop from `VIKUNJA.md`:
   `pull → load worker → evaluate policy → execute via adapter → checkpoint → emit event`.

In v0.1, run one worker process per box. Run multiple worker processes on the
same box if you want concurrency (they'll coordinate via the queue).

## Database migrations

Alembic migrations live in `src/personaos/db/migrations/versions/`.

```bash
make migrate          # Apply all pending
make migrate-down     # Rollback the last migration
```

Schema changes must:

1. Update the corresponding `schemas/*.yaml` (source of truth, per ADR 0002).
2. Add an Alembic migration under `src/personaos/db/migrations/versions/`.
3. Add a test that exercises the migration both up and down.

## Logs

Structured JSON to stdout via `structlog`. Configurable via `LOG_LEVEL` and
`LOG_FORMAT` in `.env`.

Example log line:

```json
{"event": "task.claimed", "task_id": "deploy-2025-001", "worker_id": "devops-01", "node_id": "node-01", "lease_expires_at": "2026-10-07T05:30:00Z", "timestamp": "2026-10-07T05:25:00Z"}
```

## Observability (v0.1 minimal)

- **`GET /health`** — DB connectivity + last successful migration.
- **`GET /events?type=...&since=...&limit=...`** — Read the event log.
- Workers log structured JSON for every claim/execute/complete cycle.

Dashboards, metrics scraping, distributed tracing, and alerting are deferred to Phase 3.

## Troubleshooting

**Worker not claiming tasks?**

- Check `worker.status` is `active`, not `paused` or `quarantined`.
- Check `node.last_heartbeat_at` — older than lease TTL means the scheduler skipped you.
- Look at recent `policy.denied` events.

**Tasks stuck in `assigned`?**

- Likely the worker died mid-task. The lease TTL will expire; the scheduler
  will reclaim and requeue.

**Migrations failing?**

- Verify `DATABASE_URL` in `.env`.
- Verify Postgres is up: `docker compose ps`.
- Look for the failing migration in `src/personaos/db/migrations/versions/`.

**Worker crashed mid-execution?**

- Check the most recent `task.checkpointed` event for that task ID.
- The runner will resume from the checkpoint on the next claim.

## Security operations

**Quarantine a worker:**

```bash
curl -X POST http://localhost:8000/workers/devops-01/quarantine \
  -H 'Content-Type: application/json' \
  -d '{"reason": "manual review", "quarantined_by": "user:alice"}'
```

The worker stops claiming tasks within one poll interval. The `worker.quarantined`
event is appended to the event log.

**Inspect policy decisions:**

```bash
curl 'http://localhost:8000/events?type=policy.denied&limit=50'
```

**Approve a pending task:**

```bash
curl -X POST http://localhost:8000/tasks/deploy-2025-001/approve \
  -H 'Content-Type: application/json' \
  -d '{"approver": "user:alice"}'
```

These endpoints are v0.1 MVP — the full policy management UI is post-v0.1.

## Backup & restore

```bash
# Backup
docker compose exec postgres pg_dump -U personaos personaos > backup-$(date +%F).sql

# Restore
docker compose exec -T postgres psql -U personaos personaos < backup-2026-10-07.sql
```

In v0.1, the database is the source of truth. Events are append-only; if you
restore from a backup, you'll have a gap in the event log that's recoverable
but worth knowing.
