"""add task.requeued to task_events check constraint (Step 15 / ADR 0018)

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-22

Per ADR 0018 — Task Review Resolution & Operator Re-Entry Lifecycle.
Adds 'task.requeued' to the allowed values of task_events_type_check.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Drop existing 5-element constraint
    op.drop_constraint("task_events_type_check", "task_events", type_="check")

    # 2. Add updated 6-element constraint including 'task.requeued'
    op.create_check_constraint(
        "task_events_type_check",
        "task_events",
        "type IN ('task.created', 'task.claimed', 'task.checkpointed', "
        "'task.completed', 'task.failed', 'task.requeued')",
    )


def downgrade() -> None:
    # 1. Preserve historical data: map task.requeued -> task.created with payload marker
    op.execute(
        "UPDATE task_events "
        "SET type = 'task.created', "
        "    payload = jsonb_set(payload, '{original_type}', '\"task.requeued\"'::jsonb) "
        "WHERE type = 'task.requeued'"
    )

    # 2. Drop 6-element constraint
    op.drop_constraint("task_events_type_check", "task_events", type_="check")

    # 3. Restore 5-element constraint
    op.create_check_constraint(
        "task_events_type_check",
        "task_events",
        "type IN ('task.created', 'task.claimed', 'task.checkpointed', "
        "'task.completed', 'task.failed')",
    )
