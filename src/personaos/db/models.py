"""SQLAlchemy ORM models — six v0.1 tables per ADR 0008.

Per the v0.1 design (Liang, 2026-10-07) + ADR 0008: SIX core tables only.

  workers           — thin identity record
  tasks             — unit of work
  worker_skills     — (worker_id, skill) join table
  worker_nodes      — current/historical binding (worker ↔ node)
  task_events       — events related to tasks (CHECK-constrained to 5 task event types)
  task_checkpoints  — durable state snapshots for resumable tasks

Deferred from v0.1 (kept for v0.2+ planning):
  - policies, leases, pending_approvals, worker/node events, unified events, pgvector

Per ADR 0002: generated from schemas/*.yaml (codegen deferred to v0.2).
This is the hand-written v0.1 starting point; matches `dd2118d` exactly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all v0.1 ORM models."""


class Worker(Base):
    """Thin worker identity record.

    Per ADR 0003: Worker is identity. Survives model + runtime changes.
    Per ADR 0008: thin — skills live in worker_skills; permissions/capabilities
    are JSONB lists; memory_id is null in v0.1.
    """

    __tablename__ = "workers"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'idle'"))
    runtime: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'openclaw'"))
    max_concurrent_tasks: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    capabilities: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    permissions: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    personality: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    memory_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_profile_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('idle', 'busy', 'paused', 'quarantined', 'retired')",
            name="workers_status_check",
        ),
        Index("ix_workers_status", "status"),
        Index("ix_workers_role", "role"),
    )


class WorkerSkill(Base):
    """(worker_id, skill) join table.

    Scheduler queries this for skill matching.
    """

    __tablename__ = "worker_skills"

    worker_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("workers.id", ondelete="CASCADE"),
        primary_key=True,
    )
    skill: Mapped[str] = mapped_column(Text, primary_key=True)

    __table_args__ = (Index("ix_worker_skills_skill", "skill"),)


class WorkerNode(Base):
    """Current / historical binding of a worker to a node.

    Per ADR 0003 + ADR 0008: replaces the leases table for v0.1.
    unbound_at IS NULL means the binding is current.

    Caveat (per ADR 0008 Future evolution): this is NOT a true lease.
    When failover is implemented, schema will evolve to track attempt,
    lease_expires_at, etc.
    """

    __tablename__ = "worker_nodes"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    worker_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workers.id", ondelete="CASCADE"), nullable=False
    )
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    bound_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    unbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_worker_nodes_worker", "worker_id"),
        Index("ix_worker_nodes_node", "node_id"),
        Index(
            "ix_worker_nodes_current",
            "worker_id",
            "node_id",
            postgresql_where=text("unbound_at IS NULL"),
        ),
    )


class Task(Base):
    """Unit of work assigned to a worker.

    Per ADR 0008: tasks is one of the six v0.1 tables. 8 lifecycle states.
    Priority is a string enum (low/medium/high/critical) — scheduler maps to int.

    FK notes:
      - assigned_worker → workers.id (SET NULL on worker delete)
      - assigned_node   → text (no nodes table in v0.1; opaque runner ID)
      - checkpoint_ref  → task_checkpoints.ref (added in migration step 5
        via op.create_foreign_key to avoid circular dep)
      - simulated_from  → tasks.id (SET NULL on source delete; added in
        migration 0003 for task simulation / replay per ADR 0011)
    """

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'queued'"))
    priority: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'medium'"))
    type: Mapped[str | None] = mapped_column(Text, nullable=True)
    required_skills: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    required_capabilities: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    assigned_worker: Mapped[str | None] = mapped_column(
        Text, ForeignKey("workers.id", ondelete="SET NULL"), nullable=True
    )
    assigned_node: Mapped[str | None] = mapped_column(Text, nullable=True)
    dependencies: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # Per ADR 0011 (Step 6): nullable self-FK to the source task. NULL on
    # production tasks; set to source.id on simulation tasks. ON DELETE SET
    # NULL preserves simulations when the source is deleted (audit invariant).
    simulated_from: Mapped[str | None] = mapped_column(
        Text, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    checkpoint_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    side_effect_classification: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'UNKNOWN'")
    )
    exhaustion_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'assigned', 'in_progress', 'blocked', "
            "'review', 'completed', 'failed', 'canceled')",
            name="tasks_status_check",
        ),
        CheckConstraint(
            "priority IN ('low', 'medium', 'high', 'critical')",
            name="tasks_priority_check",
        ),
        CheckConstraint(
            "side_effect_classification IN ('UNKNOWN', 'IDEMPOTENT', 'HAS_SIDE_EFFECTS')",
            name="tasks_side_effect_classification_check",
        ),
        Index("ix_tasks_status", "status"),
        Index("ix_tasks_assigned_worker", "assigned_worker"),
        Index("ix_tasks_assigned_node", "assigned_node"),
        Index("ix_tasks_priority_created", "priority", "created_at"),
        Index("ix_tasks_simulated_from", "simulated_from"),
        Index(
            "ix_tasks_recovery_scan",
            "lock_expires_at",
            postgresql_where=text("status = 'in_progress'"),
        ),
        Index(
            "ix_tasks_review_queue",
            "priority",
            "created_at",
            postgresql_where=text("status = 'review'"),
        ),
    )


class TaskCheckpoint(Base):
    """Durable state snapshot for a task. Resume point on restart / failover.

    ref is the opaque UUID stored in tasks.checkpoint_ref.
    """

    __tablename__ = "task_checkpoints"

    ref: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    last_step: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_task_checkpoints_task", "task_id", "created_at"),)


class TaskEvent(Base):
    """Append-only event log for task lifecycle.

    Per ADR 0006: events are append-only. State tables remain source of truth.

    v0.1 emits only the 5 task event types:
      task.created, task.claimed, task.checkpointed, task.completed, task.failed
    (Other 12 from ADR 0006 deferred to v0.2.)
    """

    __tablename__ = "task_events"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    task_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "type IN ('task.created', 'task.claimed', 'task.checkpointed', "
            "'task.completed', 'task.failed', 'task.requeued')",
            name="task_events_type_check",
        ),
        Index("ix_task_events_task_created", "task_id", "created_at"),
    )
