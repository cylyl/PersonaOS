"""add tasks.simulated_from for task simulation / replay (Step 6 / ADR 0011)

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-08

Per ADR 0011 — Task Simulation / Replay:

  Simulation/replay re-runs a historical task without mutating the original.
  The simulation is a NEW row in ``tasks`` linked to the source via the
  ``simulated_from`` FK. The source row is unchanged.

  +-------------------------+       +-------------------------+
  | source task             |       | simulation task         |
  | id = task-123           |  <--- | id = task-NEW           |
  | simulated_from = NULL   |  FK   | simulated_from = task-123|
  | profile_version = 2     |       | profile_version = 2     |  (snapshot)
  | input = {...}           |       | input = {...}           |  (copy)
  | result = <produced>     |       | result = <simulated>    |  (adapter output)
  +-------------------------+       +-------------------------+

Schema:
  - tasks.simulated_from: text, nullable, FK -> tasks.id (ON DELETE SET NULL)
  - ix_tasks_simulated_from: index on simulated_from for reverse queries

Why ON DELETE SET NULL (not CASCADE):
  Replay rows are audit history. If someone deletes the source task, the
  simulation rows must survive (their FK becomes NULL). This matches the
  audit invariant: "show me every simulation that has ever replayed task X".

No new table. Stays inside the six-table boundary from ADR 0008.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add the nullable column.
    op.add_column(
        "tasks",
        sa.Column("simulated_from", sa.Text(), nullable=True),
    )

    # 2. Self-referential FK: tasks.simulated_from -> tasks.id (ON DELETE SET NULL).
    #    CASCADE would lose replay history when the source is deleted.
    op.create_foreign_key(
        "tasks_simulated_from_fkey",
        "tasks",
        "tasks",
        ["simulated_from"],
        ["id"],
        ondelete="SET NULL",
    )

    # 3. Index for the reverse query "find all simulations of task X".
    op.create_index(
        "ix_tasks_simulated_from",
        "tasks",
        ["simulated_from"],
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_simulated_from", table_name="tasks")
    op.drop_constraint("tasks_simulated_from_fkey", "tasks", type_="foreignkey")
    op.drop_column("tasks", "simulated_from")
