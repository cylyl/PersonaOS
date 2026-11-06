"""Integration Gateway Event Bus and SSE Broadcasting Engine.

Per ADR 0041: Manages real-time event broadcasting, subscriber queues,
keepalive heartbeats, and client reconnection with Last-Event-ID replay.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from datetime import UTC, datetime
from typing import Any

import structlog

from personaos.integrations.common.gateway_schemas import GatewayEventMessage

logger = structlog.get_logger(__name__)


class GatewayEventBus:
    """In-memory event bus and replay ring buffer for SSE subscribers."""

    def __init__(self, buffer_size: int = 1000) -> None:
        self._buffer_size = buffer_size
        self._history: deque[GatewayEventMessage] = deque(maxlen=buffer_size)
        self._subscribers: set[asyncio.Queue[GatewayEventMessage]] = set()

    def publish(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        event_id: str | None = None,
        timestamp: datetime | None = None,
    ) -> GatewayEventMessage:
        """Publish an event to all active SSE subscribers and append to history buffer."""
        evt = GatewayEventMessage(
            event_id=event_id or str(uuid.uuid4()),
            event_type=event_type,
            payload=payload or {},
            timestamp=timestamp or datetime.now(UTC),
        )
        self._history.append(evt)

        dead_queues = []
        for q in list(self._subscribers):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                logger.warning("sse_subscriber_queue_full_dropping_oldest")
                try:
                    q.get_nowait()
                    q.put_nowait(evt)
                except Exception:
                    dead_queues.append(q)
            except Exception:
                dead_queues.append(q)

        for dq in dead_queues:
            self._subscribers.discard(dq)

        return evt

    def register_queue(self, queue: asyncio.Queue[GatewayEventMessage]) -> None:
        """Register a subscriber queue for live event broadcasting."""
        self._subscribers.add(queue)

    def unregister_queue(self, queue: asyncio.Queue[GatewayEventMessage]) -> None:
        """Unregister a subscriber queue."""
        self._subscribers.discard(queue)

    def get_missed_events(
        self,
        last_event_id: str | None = None,
        run_id: str | None = None,
    ) -> list[GatewayEventMessage]:
        """Return events from history buffer that occurred after last_event_id."""
        missed: list[GatewayEventMessage] = []
        if last_event_id:
            found = False
            for evt in self._history:
                if found:
                    match_run = not run_id or evt.payload.get("run_id") in (run_id, None)
                    if match_run:
                        missed.append(evt)
                elif evt.event_id == last_event_id:
                    found = True
        return missed

    def subscribe(
        self,
        last_event_id: str | None = None,
        run_id: str | None = None,
    ) -> tuple[asyncio.Queue[GatewayEventMessage], list[GatewayEventMessage]]:
        """Subscribe to live events and return (queue, missed_events).

        If last_event_id is provided, any events in the ring buffer that occurred
        after last_event_id are returned for immediate replay.
        """
        queue: asyncio.Queue[GatewayEventMessage] = asyncio.Queue(maxsize=500)
        self.register_queue(queue)
        missed = self.get_missed_events(last_event_id=last_event_id, run_id=run_id)
        return queue, missed

    def unsubscribe(self, queue: asyncio.Queue[GatewayEventMessage]) -> None:
        """Unsubscribe a queue from live events."""
        self.unregister_queue(queue)

    def get_history(self, limit: int = 50, run_id: str | None = None) -> list[GatewayEventMessage]:
        """Return snapshot of recent events from ring buffer."""
        events = list(self._history)
        if run_id:
            events = [e for e in events if e.payload.get("run_id") in (run_id, None)]
        return events[-limit:]

    def publish_task_status_changed(
        self,
        task_id: str,
        status: str,
        run_id: str | None = None,
        worker_id: str | None = None,
        extra: dict[str, Any] | None = None,
        event_id: str | None = None,
        timestamp: datetime | None = None,
    ) -> GatewayEventMessage:
        """Publish a task status change event."""
        payload: dict[str, Any] = {
            "task_id": task_id,
            "status": status,
        }
        if run_id:
            payload["run_id"] = run_id
        if worker_id:
            payload["worker_id"] = worker_id
        if extra:
            payload.update(extra)
        return self.publish(
            event_type="task_status_changed",
            payload=payload,
            event_id=event_id,
            timestamp=timestamp,
        )

    def publish_ready_wave_advanced(
        self,
        run_id: str,
        current_wave: int,
        ready_task_ids: list[str] | None = None,
        extra: dict[str, Any] | None = None,
        event_id: str | None = None,
        timestamp: datetime | None = None,
    ) -> GatewayEventMessage:
        """Publish a ready wave advancement event."""
        payload: dict[str, Any] = {
            "run_id": run_id,
            "current_wave": current_wave,
            "ready_task_ids": ready_task_ids or [],
        }
        if extra:
            payload.update(extra)
        return self.publish(
            event_type="ready_wave_advanced",
            payload=payload,
            event_id=event_id,
            timestamp=timestamp,
        )

    def publish_gate_created(
        self,
        gate_id: str,
        task_id: str,
        run_id: str | None = None,
        question: str | None = None,
        generation: int = 1,
        extra: dict[str, Any] | None = None,
        event_id: str | None = None,
        timestamp: datetime | None = None,
    ) -> GatewayEventMessage:
        """Publish a gate created (human review requested) event."""
        payload: dict[str, Any] = {
            "gate_id": gate_id,
            "task_id": task_id,
            "generation": generation,
        }
        if run_id:
            payload["run_id"] = run_id
        if question:
            payload["question"] = question
        if extra:
            payload.update(extra)
        return self.publish(
            event_type="gate_created",
            payload=payload,
            event_id=event_id,
            timestamp=timestamp,
        )

    def publish_gate_resolved(
        self,
        gate_id: str,
        task_id: str,
        run_id: str | None = None,
        decision: str = "approve",
        new_generation: int = 2,
        operator_id: str = "orca:operator",
        extra: dict[str, Any] | None = None,
        event_id: str | None = None,
        timestamp: datetime | None = None,
    ) -> GatewayEventMessage:
        """Publish a gate resolved (approval or rejection) event."""
        payload: dict[str, Any] = {
            "gate_id": gate_id,
            "task_id": task_id,
            "decision": decision,
            "status": "approved" if decision == "approve" else "rejected",
            "new_generation": new_generation,
            "operator_id": operator_id,
        }
        if run_id:
            payload["run_id"] = run_id
        if extra:
            payload.update(extra)
        return self.publish(
            event_type="gate_resolved",
            payload=payload,
            event_id=event_id,
            timestamp=timestamp,
        )

    def publish_heartbeat(
        self,
        timestamp: datetime | None = None,
        event_id: str | None = None,
    ) -> GatewayEventMessage:
        """Publish an explicit keepalive heartbeat frame."""
        now = timestamp or datetime.now(UTC)
        return self.publish(
            event_type="heartbeat",
            payload={"timestamp": now.isoformat()},
            event_id=event_id,
            timestamp=now,
        )

    def clear(self) -> None:
        """Clear all subscribers and history (useful for test resets)."""
        self._history.clear()
        self._subscribers.clear()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


# Global singleton instance for Gateway event routing
gateway_event_bus = GatewayEventBus()
