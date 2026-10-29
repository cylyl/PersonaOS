"""create control_plane_nodes table and add claimed_by_cp_id to tasks (v0.2-A / ADR 0033)

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-29

Per ADR 0033 — Multi-Node Control-Plane Distribution:
- Creates control_plane_nodes table for active-active cluster membership and heartbeats.
- Adds claimed_by_cp_id to tasks table for tracking control plane instance ownership.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create control_plane_nodes table
    op.create_table(
        "control_plane_nodes",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("ip_address", sa.String(length=45), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("api_endpoint", sa.String(length=512), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column(
            "last_heartbeat_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'draining', 'offline')",
            name="control_plane_nodes_status_check",
        ),
    )

    op.create_index(
        "ix_control_plane_nodes_status_heartbeat",
        "control_plane_nodes",
        ["status", "last_heartbeat_at"],
    )

    # 2. Add claimed_by_cp_id to tasks table
    op.add_column(
        "tasks",
        sa.Column("claimed_by_cp_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_tasks_claimed_by_cp_id",
        "tasks",
        ["claimed_by_cp_id"],
    )


def downgrade() -> None:
    # 1. Drop claimed_by_cp_id from tasks
    op.drop_index("ix_tasks_claimed_by_cp_id", table_name="tasks")
    op.drop_column("tasks", "claimed_by_cp_id")

    # 2. Drop control_plane_nodes table
    op.drop_index(
        "ix_control_plane_nodes_status_heartbeat",
        table_name="control_plane_nodes",
    )
    op.drop_table("control_plane_nodes")
