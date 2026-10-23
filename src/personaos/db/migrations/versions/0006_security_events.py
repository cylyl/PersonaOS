"""add security events (policy.allowed, policy.denied, approval.requested) to task_events check constraint (Step 17 / ADR 0020)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-23

Per ADR 0020 — Security Policy Enforcement.
Adds 'policy.allowed', 'policy.denied', 'approval.requested' to the allowed values of task_events_type_check.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Drop existing 6-element constraint
    op.drop_constraint("task_events_type_check", "task_events", type_="check")

    # 2. Add updated 9-element constraint including security events
    op.create_check_constraint(
        "task_events_type_check",
        "task_events",
        "type IN ('task.created', 'task.claimed', 'task.checkpointed', "
        "'task.completed', 'task.failed', 'task.requeued', "
        "'policy.allowed', 'policy.denied', 'approval.requested')",
    )


def downgrade() -> None:
    # 1. Preserve historical data: map security events -> task.failed with payload marker
    op.execute(
        "UPDATE task_events "
        "SET type = 'task.failed', "
        "    payload = jsonb_set(payload, '{original_type}', to_jsonb(type)) "
        "WHERE type IN ('policy.allowed', 'policy.denied', 'approval.requested')"
    )

    # 2. Drop 9-element constraint
    op.drop_constraint("task_events_type_check", "task_events", type_="check")

    # 3. Restore 6-element constraint
    op.create_check_constraint(
        "task_events_type_check",
        "task_events",
        "type IN ('task.created', 'task.claimed', 'task.checkpointed', "
        "'task.completed', 'task.failed', 'task.requeued')",
    )
