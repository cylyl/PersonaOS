"""Scheduler — Worker Skill, Capability, and Affinity Scheduling.

Per ADR 0019 & docs/specs/worker-skill-capability-scheduler.md:
- Strictly read-only candidate selection in PostgreSQL.
- Evaluates worker affinity, node affinity, deadline, dependencies,
  capabilities containment, and skill subset matching.
- Orders candidates by priority rank first, then deterministic FIFO
  (created_at ASC, id ASC).
- Does NOT mutate task status, does NOT lock rows (no FOR UPDATE), and
  does NOT emit task events.
- Kernel remains the sole owner of atomic task claims.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from personaos.domain.task import Task

_CANDIDATE_SELECTION_SQL = text(
    """
    SELECT t.id, t.title, t.objective, t.status, t.priority, t.type,
           t.required_skills, t.required_capabilities,
           t.assigned_worker, t.assigned_node,
           t.dependencies, t.input, t.checkpoint_ref, t.result,
           t.retry_count, t.max_retries, t.last_error,
           t.deadline, t.created_at, t.started_at, t.completed_at,
           t.profile_version, t.lock_expires_at, t.generation,
           t.side_effect_classification, t.exhaustion_reason
    FROM tasks t
    WHERE t.status = 'queued'
      -- 1. Worker Affinity: unassigned OR pre-assigned to this worker
      AND (t.assigned_worker IS NULL OR t.assigned_worker = :worker_id)
      -- 2. Node Affinity: unassigned OR pre-assigned to this node
      AND (
          CAST(:node_id AS text) IS NULL
          OR t.assigned_node IS NULL
          OR t.assigned_node = CAST(:node_id AS text)
      )
      -- 3. Deadline Gate: unexpired tasks only
      AND (t.deadline IS NULL OR t.deadline > NOW())
      -- 4. Dependency Gate: all declared dependencies must be completed
      AND (
          t.dependencies IS NULL
          OR jsonb_array_length(t.dependencies) = 0
          OR NOT EXISTS (
              SELECT 1
              FROM jsonb_array_elements_text(t.dependencies) AS dep_id
              LEFT JOIN tasks dep ON dep.id = dep_id
              WHERE dep.id IS NULL              -- missing dependency
                 OR dep.status <> 'completed'   -- dependency incomplete
          )
      )
      -- 5. Capability Match: all required capabilities must be in worker capabilities/permissions
      AND (
          t.required_capabilities IS NULL
          OR jsonb_array_length(t.required_capabilities) = 0
          OR t.required_capabilities <@ (
              SELECT COALESCE(capabilities, CAST('[]' AS jsonb))
                     || COALESCE(permissions, CAST('[]' AS jsonb))
              FROM workers
              WHERE id = :worker_id
          )
      )
      -- 6. Skill Match: all required skills must be possessed by worker
      AND (
          t.required_skills IS NULL
          OR jsonb_array_length(t.required_skills) = 0
          OR NOT EXISTS (
              SELECT 1
              FROM jsonb_array_elements_text(t.required_skills) AS req_skill
              WHERE lower(trim(req_skill)) NOT IN (
                  SELECT lower(trim(skill)) FROM worker_skills WHERE worker_id = :worker_id
                  UNION
                  SELECT lower(trim(jsonb_array_elements_text(
                      COALESCE(capabilities, CAST('[]' AS jsonb))
                      || COALESCE(permissions, CAST('[]' AS jsonb))
                  )))
                  FROM workers
                  WHERE id = :worker_id
              )
          )
      )
    ORDER BY
      CASE t.priority
        WHEN 'critical' THEN 4
        WHEN 'high'     THEN 3
        WHEN 'medium'   THEN 2
        WHEN 'low'      THEN 1
        ELSE 2
      END DESC,
      t.created_at ASC,
      t.id ASC
    LIMIT :limit
    """
)


class WorkloadScheduler:
    """Read-only candidate selector for worker-aware task scheduling."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_candidate(
        self,
        worker_id: str,
        node_id: str | None = None,
    ) -> Task | None:
        """Find the highest-priority, oldest eligible task for the worker.

        Returns None if no runnable tasks match worker affinity, skills,
        capabilities, node, deadline, or dependencies.
        """
        candidates = await self.find_candidates(worker_id=worker_id, node_id=node_id, limit=1)
        return candidates[0] if candidates else None

    async def find_candidates(
        self,
        worker_id: str,
        node_id: str | None = None,
        limit: int = 1,
    ) -> list[Task]:
        """Query PostgreSQL for eligible tasks ordered by priority and FIFO."""
        if limit <= 0:
            return []

        res = await self._session.execute(
            _CANDIDATE_SELECTION_SQL,
            {
                "worker_id": worker_id,
                "node_id": node_id,
                "limit": limit,
            },
        )
        rows = res.fetchall()
        return [self._row_to_task(row) for row in rows]

    @staticmethod
    def _row_to_task(row: Any) -> Task:
        return Task(
            id=row.id,
            title=row.title,
            objective=row.objective,
            status=row.status,
            priority=row.priority,
            type=row.type,
            required_skills=list(row.required_skills or []),
            required_capabilities=list(row.required_capabilities or []),
            assigned_worker=row.assigned_worker,
            assigned_node=row.assigned_node,
            dependencies=list(row.dependencies or []),
            input=row.input,
            checkpoint_ref=row.checkpoint_ref,
            result=row.result,
            retry_count=row.retry_count,
            max_retries=row.max_retries,
            last_error=row.last_error,
            deadline=row.deadline,
            created_at=row.created_at,
            started_at=row.started_at,
            completed_at=row.completed_at,
            profile_version=row.profile_version,
            lock_expires_at=row.lock_expires_at,
            generation=row.generation,
            side_effect_classification=row.side_effect_classification,
            exhaustion_reason=row.exhaustion_reason,
        )
