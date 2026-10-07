"""Workload Engine — durable queue, scheduler, leases.

v0.1 scope:
  - Postgres-backed durable queue (SKIP LOCKED)
  - Scheduler: FIFO + priority + skill-match in SQL (per ADR: no AI scheduler yet)
  - Leases: TTL-based locks for safe retry and resume
"""
