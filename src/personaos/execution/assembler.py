"""Execution context assembler.

Per ADR 0022 and specification:
Resolves upstream dependency results, resolves dynamic $dep:... input references,
gathers worker experience, and conditions the execution context with worker persona.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from personaos.db.models import Task as TaskModel
from personaos.domain.errors import DependencyArtifactResolutionError
from personaos.domain.task import Task
from personaos.execution.context import WorkerExecutionContext
from personaos.registry.profile import ProfileRegistry

_EXACT_DEP_REF = re.compile(r"^\$dep:([a-zA-Z0-9_\-]+)(?:\.([a-zA-Z0-9_\.\-]+))?$")
_EMBEDDED_DEP_REF = re.compile(r"\$dep:([a-zA-Z0-9_\-]+)(?:\.([a-zA-Z0-9_\.\-]+))?")


class ExecutionContextAssembler:
    """Assembles and enriches WorkerExecutionContext with upstream artifacts and persona context."""

    def __init__(
        self,
        session: AsyncSession,
        registry: ProfileRegistry | None = None,
    ) -> None:
        self._session = session
        self._registry = registry

    async def enrich(
        self,
        context: WorkerExecutionContext,
        max_recent_experience: int = 3,
    ) -> WorkerExecutionContext:
        """Enrich existing context with upstream results, substituted input, and experience."""
        # 1. Fetch upstream dependency results
        upstream_results = dict(context.upstream_results)
        deps = context.task.dependencies or []
        if deps:
            missing_deps = [d for d in deps if d not in upstream_results]
            if missing_deps:
                dep_rows = (
                    await self._session.execute(
                        select(TaskModel.id, TaskModel.result).where(
                            TaskModel.id.in_(missing_deps),
                            TaskModel.status == "completed",
                        )
                    )
                ).all()
                for dep_id, dep_res in dep_rows:
                    if isinstance(dep_res, dict):
                        upstream_results[dep_id] = dep_res
                    elif dep_res is not None:
                        upstream_results[dep_id] = {"output": dep_res}
                    else:
                        upstream_results[dep_id] = {}

        # 2. Resolve input template references ($dep:<task_id>.<path>)
        resolved_input = self.resolve_input_references(context.task.input, upstream_results)
        effective_task = dataclasses.replace(context.task, input=resolved_input)

        # 3. Gather recent experience for the worker
        recent_experience = list(context.recent_experience)
        if not recent_experience and context.worker.id and max_recent_experience > 0:
            exp_rows = (
                await self._session.execute(
                    select(TaskModel.id, TaskModel.title, TaskModel.result)
                    .where(
                        TaskModel.assigned_worker == context.worker.id,
                        TaskModel.status == "completed",
                        TaskModel.id != context.task.id,
                    )
                    .order_by(TaskModel.completed_at.desc())
                    .limit(max_recent_experience)
                )
            ).all()
            for t_id, t_title, t_res in exp_rows:
                summary: Any = None
                if isinstance(t_res, dict):
                    summary = t_res.get("summary") or t_res.get("message") or t_res
                elif t_res is not None:
                    summary = str(t_res)
                recent_experience.append(
                    {
                        "task_id": t_id,
                        "title": t_title,
                        "summary": summary,
                    }
                )

        return WorkerExecutionContext(
            worker=context.worker,
            profile=context.profile,
            task=effective_task,
            upstream_results=upstream_results,
            recent_experience=recent_experience,
        )

    async def assemble(
        self,
        task: Task,
        profile_version_override: int | None = None,
        max_recent_experience: int = 3,
    ) -> WorkerExecutionContext:
        """Resolve base profile snapshot and enrich with upstream artifacts and experience."""
        registry = self._registry or ProfileRegistry(session=self._session)
        base_ctx = await registry.resolve_execution_context(
            task, profile_version_override=profile_version_override
        )
        return await self.enrich(base_ctx, max_recent_experience=max_recent_experience)

    @classmethod
    def resolve_input_references(
        cls,
        task_input: Any,
        upstream_results: dict[str, dict[str, Any]],
    ) -> Any:
        """Recursively substitutes $dep:<task_id>.<key> expressions with upstream results."""
        if isinstance(task_input, dict):
            return {
                k: cls.resolve_input_references(v, upstream_results) for k, v in task_input.items()
            }
        if isinstance(task_input, list):
            return [cls.resolve_input_references(item, upstream_results) for item in task_input]
        if isinstance(task_input, str):
            # Check for exact single expression match first
            exact_match = _EXACT_DEP_REF.match(task_input)
            if exact_match:
                dep_id = exact_match.group(1)
                path = exact_match.group(2)
                return cls._extract_dep_value(dep_id, path, upstream_results)

            # Check for embedded expressions
            if "$dep:" in task_input:

                def _sub_match(m: re.Match[str]) -> str:
                    dep_id = m.group(1)
                    path = m.group(2)
                    val = cls._extract_dep_value(dep_id, path, upstream_results)
                    return str(val) if val is not None else ""

                return _EMBEDDED_DEP_REF.sub(_sub_match, task_input)

        return task_input

    @classmethod
    def _extract_dep_value(
        cls,
        dep_id: str,
        path: str | None,
        upstream_results: dict[str, dict[str, Any]],
    ) -> Any:
        if dep_id not in upstream_results:
            available = list(upstream_results.keys())
            msg = (
                f"Referenced dependency task {dep_id!r} not found in completed upstream results. "
                f"Available dependency tasks: {available}"
            )
            raise DependencyArtifactResolutionError(msg)

        res: Any = upstream_results[dep_id]
        if not path:
            return res

        curr = res
        parts = path.split(".")
        for part in parts:
            if isinstance(curr, dict) and part in curr:
                curr = curr[part]
            else:
                avail_keys = list(curr.keys()) if isinstance(curr, dict) else []
                msg = (
                    f"Field {part!r} in path {path!r} not found in result for task {dep_id!r}. "
                    f"Available keys at this level: {avail_keys}"
                )
                raise DependencyArtifactResolutionError(msg)
        return curr
