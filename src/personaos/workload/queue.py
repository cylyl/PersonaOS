"""Durable task queue — Postgres-backed.

Why Postgres instead of Redis/RabbitMQ for v0.1:
  - One less piece of infra to operate
  - Transactions span state + events + queue (per ADR 0006)
  - The same SELECT ... FOR UPDATE SKIP LOCKED pattern is atomic

TODO(v0.1): implement the queue primitives:
  - enqueue(task)        — single INSERT
  - claim(worker, node)  — atomic SELECT + UPDATE in one transaction
  - complete(task_id)
  - fail(task_id, reason)
  - cancel(task_id, reason)
  - list_by_status(status, limit=100)

The claim is the hot path — measure it.
"""
