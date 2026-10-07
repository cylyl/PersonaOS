"""add profile_version columns for WorkerProfile versioning

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-07

Per ADR 0008 Addendum — Versioned WorkerProfile + Immutable Task Snapshots.
Two columns added:
  - workers.current_profile_version: pointer to the active profile version
  - tasks.profile_version: snapshot at enqueue of the profile version used

No new tables. Profile versions live as immutable YAML files on disk.

Invariants enforced (see docs/specs/worker-profile.md Section 8):
  1. Worker has one active profile version
  2. Profile version is immutable after activation
  3. Every task captures the exact profile version used
  4. Historical tasks remain reproducible
  5. Activation is atomic
  6. Registry owns profile lifecycle

CRITICAL: tasks.profile_version records the configuration ASSIGNED to the
task, NOT necessarily the configuration that ultimately EXECUTED it. This
distinction will matter when retries/failover/execution attempts are
introduced in v0.2+.

Deferred to v0.2+ (NOT added here):
  - worker_profile_versions DB table
  - tasks.execution_node, tasks.attempt, tasks.lease_expires_at
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # workers: pointer to active profile version (atomic activation target).
    op.add_column(
        "workers",
        sa.Column(
            "current_profile_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    # tasks: snapshot of profile version at enqueue time (never updated).
    op.add_column(
        "tasks",
        sa.Column(
            "profile_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )


def downgrade() -> None:
    op.drop_column("tasks", "profile_version")
    op.drop_column("workers", "current_profile_version")
