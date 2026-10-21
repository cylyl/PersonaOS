"""Step 12a — Task model additions (UNTRACKED DRAFT for review)

NOT APPLIED — this file is a design draft showing what would be added to
the ``Task`` class in ``src/personaos/db/models.py`` for Step 12a
(ADR 0016). It is written to the workspace as an untracked file so the
additions can be reviewed before any merge into ``models.py``.

When approved, merge the two blocks below into the ``Task`` class in
``src/personaos/db/models.py``. All additions are additive; no existing
field is renamed, removed, or duplicated.

Conventions verified against the existing model:
  - ``Mapped[Optional[...]]`` for nullable columns
    (matches ``description``, ``last_error``, ``deadline``, etc.)
  - ``Mapped[int]`` / ``Mapped[str]`` for NOT NULL columns
    (matches ``retry_count``, ``status``, ``max_retries``, etc.)
  - ``server_default=text("0")`` for integer defaults
    (matches ``retry_count``, ``max_retries``, ``current_profile_version``)
  - ``server_default=text("'UNKNOWN'")`` for string defaults
    (matches ``status='idle'``, ``runtime='openclaw'``, etc.)
  - ``postgresql_where=text("...")`` for partial indexes
    (matches ``ix_worker_nodes_current`` on ``unbound_at IS NULL``)
  - CheckConstraint name follows the pattern ``tasks_<column>_check``
    (matches ``workers_status_check``, ``tasks_status_check``,
    ``tasks_priority_check``)

Field-name corrections per the Step 11a design:
  - Uses ``assigned_worker`` (not ``owner_id``) — existing column, no change
  - Uses ``lock_expires_at`` (not ``lease_expires_at``) — new column

No duplicate of ``max_retries``, ``retry_count``, ``last_error``, or any
other existing field. All four columns and two indexes are additive.

Companion file: ``src/personaos/db/migrations/versions/0004_recovery_columns.py``
"""

# ---------------------------------------------------------------------------
# Block 1 — Column additions
# ---------------------------------------------------------------------------
# Insert these four ``mapped_column`` declarations after ``last_error``
# and before ``deadline`` in the ``Task`` class body. The grouping keeps
# recovery-related state (lease + fence + classification + exhaustion)
# together and adjacent to the existing retry/error/deadline fields.
#
#     lock_expires_at: Mapped[Optional[datetime]] = mapped_column(
#         DateTime(timezone=True), nullable=True
#     )
#     generation: Mapped[int] = mapped_column(
#         Integer, nullable=False, server_default=text("0")
#     )
#     side_effect_classification: Mapped[str] = mapped_column(
#         Text, nullable=False, server_default=text("'UNKNOWN'")
#     )
#     exhaustion_reason: Mapped[Optional[str]] = mapped_column(
#         Text, nullable=True
#     )
#

# ---------------------------------------------------------------------------
# Block 2 — __table_args__ additions
# ---------------------------------------------------------------------------
# Add these three entries to the existing ``__table_args__`` tuple of the
# ``Task`` class, alongside the existing CheckConstraints and Indexes.
# The new CheckConstraint enforces the canonical three-way vocabulary at
# the database boundary; the new Indexes serve the recovery-loop scan
# and the operator review queue respectively.
#
#     CheckConstraint(
#         "side_effect_classification IN ('UNKNOWN', 'IDEMPOTENT', 'HAS_SIDE_EFFECTS')",
#         name="tasks_side_effect_classification_check",
#     ),
#     Index(
#         "ix_tasks_recovery_scan",
#         "lock_expires_at",
#         postgresql_where=text("status = 'in_progress'"),
#     ),
#     Index(
#         "ix_tasks_review_queue",
#         text("updated_at DESC"),
#         postgresql_where=text("status = 'review'"),
#     ),
#

# ---------------------------------------------------------------------------
# Block 3 — Full updated __table_args__ (for reference / patch context)
# ---------------------------------------------------------------------------
# The current ``__table_args__`` (from the v0.1 model) is:
#
#     __table_args__ = (
#         CheckConstraint(
#             "status IN ('queued', 'assigned', 'in_progress', 'blocked', "
#             "'review', 'completed', 'failed', 'canceled')",
#             name="tasks_status_check",
#         ),
#         CheckConstraint(
#             "priority IN ('low', 'medium', 'high', 'critical')",
#             name="tasks_priority_check",
#         ),
#         Index("ix_tasks_status", "status"),
#         Index("ix_tasks_assigned_worker", "assigned_worker"),
#         Index("ix_tasks_assigned_node", "assigned_node"),
#         Index("ix_tasks_priority_created", "priority", "created_at"),
#         Index("ix_tasks_simulated_from", "simulated_from"),
#     )
#
# After merge, it becomes:
#
#     __table_args__ = (
#         CheckConstraint(
#             "status IN ('queued', 'assigned', 'in_progress', 'blocked', "
#             "'review', 'completed', 'failed', 'canceled')",
#             name="tasks_status_check",
#         ),
#         CheckConstraint(
#             "priority IN ('low', 'medium', 'high', 'critical')",
#             name="tasks_priority_check",
#         ),
#         CheckConstraint(
#             "side_effect_classification IN ('UNKNOWN', 'IDEMPOTENT', 'HAS_SIDE_EFFECTS')",
#             name="tasks_side_effect_classification_check",
#         ),
#         Index("ix_tasks_status", "status"),
#         Index("ix_tasks_assigned_worker", "assigned_worker"),
#         Index("ix_tasks_assigned_node", "assigned_node"),
#         Index("ix_tasks_priority_created", "priority", "created_at"),
#         Index("ix_tasks_simulated_from", "simulated_from"),
#         Index(
#             "ix_tasks_recovery_scan",
#             "lock_expires_at",
#             postgresql_where=text("status = 'in_progress'"),
#         ),
#         Index(
#             "ix_tasks_review_queue",
#             text("updated_at DESC"),
#             postgresql_where=text("status = 'review'"),
#         ),
#     )
