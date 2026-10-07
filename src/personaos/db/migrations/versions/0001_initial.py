"""initial schema — six v0.1 tables per ADR 0008

Revision ID: 0001
Revises:
Create Date: 2026-10-07

Per ADR 0008: workers, tasks, worker_skills, worker_nodes, task_events,
task_checkpoints. NO policies, leases, pending_approvals, unified events,
or memory tables (those are v0.2+).

FK notes:
  - tasks.assigned_worker → workers.id (ON DELETE SET NULL)
  - tasks.checkpoint_ref → task_checkpoints.ref (deferred, added after both
    tables exist via op.create_foreign_key to avoid circular dep)
  - task_checkpoints.task_id → tasks.id (ON DELETE CASCADE)
  - worker_skills.worker_id → workers.id (ON DELETE CASCADE)
  - worker_nodes.worker_id → workers.id (ON DELETE CASCADE)
  - task_events.task_id → tasks.id (ON DELETE CASCADE)
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. workers (no FKs)
    op.create_table(
        "workers",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="idle"),
        sa.Column("runtime", sa.Text(), nullable=False, server_default="openclaw"),
        sa.Column(
            "max_concurrent_tasks",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "permissions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "personality",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("memory_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('idle', 'busy', 'paused', 'quarantined', 'retired')",
            name="workers_status_check",
        ),
    )
    op.create_index("ix_workers_status", "workers", ["status"])
    op.create_index("ix_workers_role", "workers", ["role"])

    # 2. worker_skills (FK → workers)
    op.create_table(
        "worker_skills",
        sa.Column(
            "worker_id",
            sa.Text(),
            sa.ForeignKey("workers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("skill", sa.Text(), primary_key=True),
    )
    op.create_index("ix_worker_skills_skill", "worker_skills", ["skill"])

    # 3. worker_nodes (FK → workers; node_id is opaque, no FK)
    op.create_table(
        "worker_nodes",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "worker_id",
            sa.Text(),
            sa.ForeignKey("workers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column(
            "bound_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("unbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
    )
    op.create_index("ix_worker_nodes_worker", "worker_nodes", ["worker_id"])
    op.create_index("ix_worker_nodes_node", "worker_nodes", ["node_id"])
    # Partial index for current bindings (unbound_at IS NULL)
    op.create_index(
        "ix_worker_nodes_current",
        "worker_nodes",
        ["worker_id", "node_id"],
        postgresql_where=sa.text("unbound_at IS NULL"),
    )

    # 4. tasks (FK → workers; checkpoint_ref FK added after both tables exist)
    op.create_table(
        "tasks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("priority", sa.Text(), nullable=False, server_default="medium"),
        sa.Column("type", sa.Text(), nullable=True),
        sa.Column(
            "required_skills",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "required_capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "assigned_worker",
            sa.Text(),
            sa.ForeignKey("workers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("assigned_node", sa.Text(), nullable=True),
        sa.Column(
            "dependencies",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "input", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("checkpoint_ref", sa.Text(), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'assigned', 'in_progress', 'blocked', "
            "'review', 'completed', 'failed', 'canceled')",
            name="tasks_status_check",
        ),
        sa.CheckConstraint(
            "priority IN ('low', 'medium', 'high', 'critical')",
            name="tasks_priority_check",
        ),
    )
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_assigned_worker", "tasks", ["assigned_worker"])
    op.create_index("ix_tasks_assigned_node", "tasks", ["assigned_node"])
    op.create_index(
        "ix_tasks_priority_created", "tasks", ["priority", "created_at"]
    )

    # 5. task_checkpoints (FK → tasks; tasks.checkpoint_ref FK added next)
    op.create_table(
        "task_checkpoints",
        sa.Column("ref", sa.Text(), primary_key=True),
        sa.Column(
            "task_id",
            sa.Text(),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("last_step", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_task_checkpoints_task", "task_checkpoints", ["task_id", "created_at"]
    )

    # 6. Now add the circular FK: tasks.checkpoint_ref → task_checkpoints.ref
    op.create_foreign_key(
        "tasks_checkpoint_ref_fkey",
        "tasks",
        "task_checkpoints",
        ["checkpoint_ref"],
        ["ref"],
        ondelete="SET NULL",
    )

    # 7. task_events (FK → tasks)
    op.create_table(
        "task_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "task_id",
            sa.Text(),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "type IN ('task.created', 'task.claimed', 'task.checkpointed', "
            "'task.completed', 'task.failed')",
            name="task_events_type_check",
        ),
    )
    op.create_index(
        "ix_task_events_task_created", "task_events", ["task_id", "created_at"]
    )


def downgrade() -> None:
    # Reverse order: events → checkpoints (drop FK first) → tasks →
    # worker_nodes → worker_skills → workers
    op.drop_table("task_events")
    op.drop_constraint("tasks_checkpoint_ref_fkey", "tasks", type_="foreignkey")
    op.drop_table("task_checkpoints")
    op.drop_table("tasks")
    op.drop_index("ix_worker_nodes_current", table_name="worker_nodes")
    op.drop_table("worker_nodes")
    op.drop_table("worker_skills")
    op.drop_table("workers")
