"""Unit tests for Integration Gateway Event Bus (ADR 0041 M6).

Verifies in-memory ring buffer, queue distribution, Last-Event-ID replay,
heartbeat generation, and helper publisher methods.
"""

from __future__ import annotations

import asyncio

from personaos.integrations.common.event_bus import GatewayEventBus
from personaos.integrations.common.gateway_schemas import GatewayEventMessage


def test_event_bus_publish_and_subscriber_delivery():
    """Events published are delivered to registered subscriber queues."""
    bus = GatewayEventBus(buffer_size=10)
    queue: asyncio.Queue[GatewayEventMessage] = asyncio.Queue()
    bus.register_queue(queue)

    evt = bus.publish(
        event_type="task_status_changed",
        payload={"task_id": "task-001", "status": "running"},
        event_id="evt-1",
    )

    assert evt.event_id == "evt-1"
    assert evt.event_type == "task_status_changed"
    assert bus.subscriber_count == 1

    # Verify message in queue
    delivered = queue.get_nowait()
    assert delivered.event_id == "evt-1"
    assert delivered.payload["status"] == "running"

    bus.unregister_queue(queue)
    assert bus.subscriber_count == 0


def test_event_bus_ring_buffer_eviction():
    """Ring buffer limits historical events to buffer_size."""
    bus = GatewayEventBus(buffer_size=3)
    for i in range(5):
        bus.publish("task_status_changed", {"idx": i}, event_id=f"evt-{i}")

    history = bus.get_history(limit=10)
    assert len(history) == 3
    assert [e.event_id for e in history] == ["evt-2", "evt-3", "evt-4"]


def test_event_bus_get_missed_events_with_last_event_id():
    """get_missed_events returns events following last_event_id."""
    bus = GatewayEventBus(buffer_size=10)
    bus.publish("task_status_changed", {"idx": 1}, event_id="evt-1")
    bus.publish("ready_wave_advanced", {"wave": 2}, event_id="evt-2")
    bus.publish("gate_created", {"gate_id": "g-1"}, event_id="evt-3")
    bus.publish("gate_resolved", {"gate_id": "g-1"}, event_id="evt-4")

    missed = bus.get_missed_events(last_event_id="evt-2")
    assert len(missed) == 2
    assert [e.event_id for e in missed] == ["evt-3", "evt-4"]

    # If last_event_id is the newest, missed is empty
    assert bus.get_missed_events(last_event_id="evt-4") == []

    # If last_event_id is not in buffer, missed is empty
    assert bus.get_missed_events(last_event_id="evt-unknown") == []


def test_event_bus_filtering_by_run_id():
    """Event bus filters missed events and history by run_id."""
    bus = GatewayEventBus(buffer_size=10)
    bus.publish("task_status_changed", {"run_id": "run-a", "step": 1}, event_id="e1")
    bus.publish("task_status_changed", {"run_id": "run-b", "step": 2}, event_id="e2")
    bus.publish("task_status_changed", {"run_id": "run-a", "step": 3}, event_id="e3")

    # Filtered by run-a
    missed_a = bus.get_missed_events(last_event_id="e1", run_id="run-a")
    assert len(missed_a) == 1
    assert missed_a[0].event_id == "e3"

    # Filtered history
    hist_b = bus.get_history(run_id="run-b")
    assert len(hist_b) == 1
    assert hist_b[0].event_id == "e2"


def test_event_bus_queue_overflow_drop_oldest():
    """When a subscriber queue is full, oldest item is dropped without crashing."""
    bus = GatewayEventBus(buffer_size=10)
    small_queue: asyncio.Queue[GatewayEventMessage] = asyncio.Queue(maxsize=2)
    bus.register_queue(small_queue)

    bus.publish("task_status_changed", {"i": 1}, event_id="e1")
    bus.publish("task_status_changed", {"i": 2}, event_id="e2")
    # Queue is now full (maxsize=2). Third publish will drop e1 and keep e2, e3
    bus.publish("task_status_changed", {"i": 3}, event_id="e3")

    assert small_queue.qsize() == 2
    item1 = small_queue.get_nowait()
    item2 = small_queue.get_nowait()
    assert item1.event_id == "e2"
    assert item2.event_id == "e3"


def test_event_bus_helper_publishers():
    """Helper publisher methods populate typed payloads accurately."""
    bus = GatewayEventBus(buffer_size=10)

    # 1. Task status changed
    evt_task = bus.publish_task_status_changed(
        task_id="t-123",
        status="completed",
        run_id="run-main",
        worker_id="se",
        extra={"exit_code": 0},
    )
    assert evt_task.event_type == "task_status_changed"
    assert evt_task.payload["task_id"] == "t-123"
    assert evt_task.payload["status"] == "completed"
    assert evt_task.payload["run_id"] == "run-main"
    assert evt_task.payload["worker_id"] == "se"
    assert evt_task.payload["exit_code"] == 0

    # 2. Ready wave advanced
    evt_wave = bus.publish_ready_wave_advanced(
        run_id="run-main",
        current_wave=3,
        ready_task_ids=["t-201", "t-202"],
    )
    assert evt_wave.event_type == "ready_wave_advanced"
    assert evt_wave.payload["current_wave"] == 3
    assert evt_wave.payload["ready_task_ids"] == ["t-201", "t-202"]

    # 3. Gate created
    evt_gc = bus.publish_gate_created(
        gate_id="gate-t-301",
        task_id="t-301",
        run_id="run-main",
        question="Approve canary?",
        generation=2,
    )
    assert evt_gc.event_type == "gate_created"
    assert evt_gc.payload["gate_id"] == "gate-t-301"
    assert evt_gc.payload["generation"] == 2
    assert evt_gc.payload["question"] == "Approve canary?"

    # 4. Gate resolved
    evt_gr = bus.publish_gate_resolved(
        gate_id="gate-t-301",
        task_id="t-301",
        run_id="run-main",
        decision="approve",
        new_generation=3,
        operator_id="orca:lead",
    )
    assert evt_gr.event_type == "gate_resolved"
    assert evt_gr.payload["status"] == "approved"
    assert evt_gr.payload["new_generation"] == 3
    assert evt_gr.payload["operator_id"] == "orca:lead"

    # 5. Heartbeat
    evt_hb = bus.publish_heartbeat()
    assert evt_hb.event_type == "heartbeat"
    assert "timestamp" in evt_hb.payload


def test_event_bus_subscribe_and_unsubscribe():
    """subscribe returns both a new queue and replayed missed events."""
    bus = GatewayEventBus(buffer_size=10)
    bus.publish("task_status_changed", {"i": 1}, event_id="e1")
    bus.publish("task_status_changed", {"i": 2}, event_id="e2")

    queue, missed = bus.subscribe(last_event_id="e1")
    assert len(missed) == 1
    assert missed[0].event_id == "e2"
    assert bus.subscriber_count == 1

    bus.unsubscribe(queue)
    assert bus.subscriber_count == 0


def test_event_bus_clear():
    """clear clears all subscribers and history."""
    bus = GatewayEventBus(buffer_size=10)
    q: asyncio.Queue[GatewayEventMessage] = asyncio.Queue()
    bus.register_queue(q)
    bus.publish("task_status_changed", {"i": 1})

    assert bus.subscriber_count == 1
    assert len(bus.get_history()) == 1

    bus.clear()
    assert bus.subscriber_count == 0
    assert len(bus.get_history()) == 0
