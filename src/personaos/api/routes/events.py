"""Events route — read-only event log inspection."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from personaos.api.deps import SessionDep
from personaos.api.schemas import EventResponse
from personaos.db.models import TaskEvent

router = APIRouter(prefix="/events", tags=["events"])


@router.get("", response_model=list[EventResponse])
async def list_events(
    session: SessionDep,
    task_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TaskEvent]:
    """Query append-only task events log in reverse chronological order."""
    query = select(TaskEvent)
    if task_id:
        query = query.where(TaskEvent.task_id == task_id)
    query = (
        query.order_by(TaskEvent.created_at.desc(), TaskEvent.id.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(query)
    return list(result.scalars().all())
