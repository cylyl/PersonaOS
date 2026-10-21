"""add tasks recovery columns and partial indexes (Step 12a / ADR 0016)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-12

Per ADR 0016 — In-Progress Task Recovery.

Four new columns on ``tasks`` (v0.2 schema additions, all additive):
  - lock_expires_at             TIMESTAMPTZ NULL
    NULL = no active lease held. Set by reclaim (NOW() + :lease_ttl);
    cleared on terminal status. Drives the recovery-loop scan predicate.
  - generation                  INTEGER      NOT NULL DEFAULT 0
    Fence counter, incremented on every ownership transfer. The stale
    owner holds an outdated generation and is fenced out of all
    subsequent writes.
  - side_effect_classification  TEXT         NOT NULL DEFAULT 'UNKNOWN'
    CHECK (side_effect_classification IN ('UNKNOWN', 'IDEMPOTENT',
    'HAS_SIDE_EFFECTS')). 'UNKNOWN' / 'HAS_SIDE_EFFECTS' tasks are never
    auto-reclaimed; they transition to 'review' on retry exhaustion.
    'IDEMPOTENT' tasks are auto-reclaimed and may transition to 'failed'
    on retry exhaustion.
  - exhaustion_reason           TEXT         NULL
    Set on retry-exhaustion transition. Distinct from ``last_error``
    (which is set on every failure path) — gives the operator UI an
    unambiguous signal that this is crash-recovery retry-exhaustion, not
    an adapter-reported failure.

Reused (no schema change):
  - max_retries           — already exists (migration 0001, default 3, NOT NULL)
  - retry_count           — already exists (migration 0001, default 0, NOT NULL)
  - last_error            — already exists
  - status='review'       — already in tasks_status_check CHECK
  - assigned_worker       — already exists (ownership field, NOT renamed)
  - created_at            — already exists (NOT NULL, server_default now())

Two new partial indexes:
  - ix_tasks_recovery_scan ON (lock_expires_at) WHERE status = 'in_progress'
    Recovery-loop primary scan: WHERE status='in_progress' AND
    lock_expires_at < NOW(). Both reclaim eligibility and retry-exhaustion
    scans reduce to this predicate.
  - ix_tasks_review_queue ON (priority, created_at) WHERE status = 'review'
    Operator review queue: SELECT ... FROM tasks WHERE status = 'review'
    ORDER BY priority, created_at LIMIT 100.

CRITICAL INVARIANT (from ADR 0016 §"Reclaim SQL" and Step 12b acceptance):
  The reclaim SQL atomically updates BOTH ``generation = generation + 1``
  AND ``retry_count = retry_count + 1`` in the same SET clause as the
  WHERE predicate. A failed conditional update (rowcount=0) consumes
  neither a retry nor a generation — the increment is conditional on
  the row match.

Field-name conventions (per Step 11a design):
  - Uses ``assigned_worker`` (not ``owner_id``) — existing column, no change
  - Uses ``lock_expires_at`` (not ``lease_expires_at``) — new column

No duplicate of ``max_retries``, ``retry_count``, or any other existing
field. All four columns and two indexes are additive.

Recovery logic deferred to Step 12b–12f.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. lock_expires_at — nullable; NULL means no active lease held.
    op.add_column(
        "tasks",
        sa.Column(
            "lock_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    # 2. generation — fence counter, default 0 for existing rows.
    op.add_column(
        "tasks",
        sa.Column(
            "generation",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    # 3. side_effect_classification — default 'UNKNOWN' so existing tasks
    #    are not auto-reclaimed; only tasks explicitly classified as
    #    'IDEMPOTENT' participate in automatic reclaim.
    op.add_column(
        "tasks",
        sa.Column(
            "side_effect_classification",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'UNKNOWN'"),
        ),
    )
    op.create_check_constraint(
        "tasks_side_effect_classification_check",
        "tasks",
        "side_effect_classification IN ('UNKNOWN', 'IDEMPOTENT', 'HAS_SIDE_EFFECTS')",
    )

    # 4. exhaustion_reason — nullable; set on retry-exhaustion transition.
    op.add_column(
        "tasks",
        sa.Column(
            "exhaustion_reason",
            sa.Text(),
            nullable=True,
        ),
    )

    # 5. ix_tasks_recovery_scan — partial index on lock_expires_at for
    #    tasks currently in_progress. Drives the recovery-loop primary scan.
    op.create_index(
        "ix_tasks_recovery_scan",
        "tasks",
        ["lock_expires_at"],
        postgresql_where=sa.text("status = 'in_progress'"),
    )

    # 6. ix_tasks_review_queue — partial index on (priority, created_at) for
    #    tasks in the operator review queue.
    op.create_index(
        "ix_tasks_review_queue",
        "tasks",
        ["priority", "created_at"],
        postgresql_where=sa.text("status = 'review'"),
    )


def downgrade() -> None:
    # Reverse order: drop indexes first (depend on columns), then the
    # CHECK constraint (depends on its column), then drop columns in
    # reverse order of addition. Dependency-safe: each step removes one
    # schema object without affecting dependents that have not yet been
    # removed. No column-level FKs between the four new columns, so the
    # column drops are mutually independent.

    # 1. Drop partial indexes (depend on columns they reference).
    op.drop_index("ix_tasks_review_queue", table_name="tasks")
    op.drop_index("ix_tasks_recovery_scan", table_name="tasks")

    # 2. Drop CHECK constraint on side_effect_classification.
    op.drop_constraint(
        "tasks_side_effect_classification_check",
        "tasks",
        type_="check",
    )

    # 3. Drop columns in reverse order of addition. PostgreSQL cascades
    #    CHECK constraints on a dropped column, but we already removed
    #    the one CHECK above for explicitness. The remaining columns
    #    carry only the implicit NOT NULL / server_default, which drop
    #    automatically with the column.
    op.drop_column("tasks", "exhaustion_reason")
    op.drop_column("tasks", "side_effect_classification")
    op.drop_column("tasks", "generation")
    op.drop_column("tasks", "lock_expires_at")
