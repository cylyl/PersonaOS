"""Unit tests for Orca CLI Orchestration Client extensions (ADR 0040)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from personaos.integrations.common.contracts import (
    OrcaGateDescriptor,
    OrcaMessageDescriptor,
    OrcaRunDescriptor,
    OrcaTaskDescriptor,
)
from personaos.integrations.orca.client import OrcaCLIClient
from personaos.integrations.orca.exceptions import (
    OrcaCLIError,
    OrcaTimeoutError,
    OrcaUnavailableError,
)


@pytest.fixture
def orca_client(tmp_path: Path) -> OrcaCLIClient:
    fake_bin = tmp_path / "fake-orca"
    fake_bin.write_text("#!/bin/sh\nexit 0")
    fake_bin.chmod(0o755)
    return OrcaCLIClient(orca_binary=str(fake_bin))


# =============================================================================
# Capability Detection Tests
# =============================================================================


@pytest.mark.asyncio
async def test_has_orchestration_capability_true(orca_client: OrcaCLIClient) -> None:
    with patch.object(
        orca_client,
        "_run_orca_cmd",
        new_callable=AsyncMock,
        return_value={"ok": True, "result": {"runs": []}},
    ) as mock_cmd:
        res = await orca_client.has_orchestration_capability()
        assert res is True
        mock_cmd.assert_called_once_with(["orchestration", "run-list"], timeout=5.0)


@pytest.mark.asyncio
async def test_has_orchestration_capability_false_on_cli_error(
    orca_client: OrcaCLIClient,
) -> None:
    with patch.object(
        orca_client,
        "_run_orca_cmd",
        new_callable=AsyncMock,
        side_effect=OrcaCLIError("Unknown command: orchestration", exit_code=1),
    ):
        res = await orca_client.has_orchestration_capability()
        assert res is False


@pytest.mark.asyncio
async def test_has_orchestration_capability_false_on_unavailable(
    orca_client: OrcaCLIClient,
) -> None:
    with patch.object(
        orca_client,
        "_run_orca_cmd",
        new_callable=AsyncMock,
        side_effect=OrcaUnavailableError("Binary not found"),
    ):
        res = await orca_client.has_orchestration_capability()
        assert res is False


# =============================================================================
# Run Lifecycle Tests
# =============================================================================


@pytest.mark.asyncio
async def test_orchestration_run_create(orca_client: OrcaCLIClient) -> None:
    payload = {
        "ok": True,
        "result": {
            "run": {
                "id": "run_test_123",
                "objective": "Complete auth refactoring",
                "coordinator_handle": "coord_01",
                "consumer_generation": 1,
                "legacy": 0,
                "created_at": "2026-11-01T03:00:00Z",
                "updated_at": "2026-11-01T03:00:00Z",
            }
        },
    }
    with patch.object(
        orca_client,
        "_run_orca_cmd",
        new_callable=AsyncMock,
        return_value=payload,
    ) as mock_cmd:
        run = await orca_client.orchestration_run_create(
            objective="Complete auth refactoring",
            from_handle="coord_01",
        )
        assert isinstance(run, OrcaRunDescriptor)
        assert run.run_id == "run_test_123"
        assert run.objective == "Complete auth refactoring"
        assert run.coordinator_handle == "coord_01"
        assert run.consumer_generation == 1
        assert run.legacy is False
        mock_cmd.assert_called_once_with(
            [
                "orchestration",
                "run-create",
                "--objective",
                "Complete auth refactoring",
                "--from",
                "coord_01",
            ]
        )


@pytest.mark.asyncio
async def test_orchestration_run_list(orca_client: OrcaCLIClient) -> None:
    payload = {
        "ok": True,
        "result": {
            "runs": [
                {
                    "id": "run_1",
                    "objective": "Objective 1",
                    "coordinator_handle": None,
                    "consumer_generation": 0,
                    "legacy": 1,
                    "created_at": "2026-10-15T00:00:00Z",
                    "updated_at": "2026-10-15T00:00:00Z",
                },
                {
                    "id": "run_2",
                    "objective": "Objective 2",
                    "coordinator_handle": "handle_2",
                    "consumer_generation": 3,
                    "legacy": 0,
                    "created_at": "2026-11-01T00:00:00Z",
                    "updated_at": "2026-11-01T00:00:00Z",
                },
            ]
        },
    }
    with patch.object(
        orca_client,
        "_run_orca_cmd",
        new_callable=AsyncMock,
        return_value=payload,
    ) as mock_cmd:
        runs = await orca_client.orchestration_run_list()
        assert len(runs) == 2
        assert runs[0].run_id == "run_1"
        assert runs[0].legacy is True
        assert runs[1].run_id == "run_2"
        assert runs[1].consumer_generation == 3
        mock_cmd.assert_called_once_with(["orchestration", "run-list"])


@pytest.mark.asyncio
async def test_orchestration_run_show(orca_client: OrcaCLIClient) -> None:
    payload = {
        "ok": True,
        "result": {
            "run": {
                "id": "run_target",
                "objective": "Inspect run objective",
                "coordinator_handle": "lead",
                "consumer_generation": 2,
                "legacy": 0,
                "created_at": "2026-11-01T01:00:00Z",
                "updated_at": "2026-11-01T02:00:00Z",
            }
        },
    }
    with patch.object(
        orca_client,
        "_run_orca_cmd",
        new_callable=AsyncMock,
        return_value=payload,
    ) as mock_cmd:
        run = await orca_client.orchestration_run_show("run_target")
        assert run.run_id == "run_target"
        assert run.objective == "Inspect run objective"
        mock_cmd.assert_called_once_with(
            ["orchestration", "run-show", "--id", "run_target"]
        )


# =============================================================================
# Task DAG Tests
# =============================================================================


@pytest.mark.asyncio
async def test_orchestration_task_create_with_deps(orca_client: OrcaCLIClient) -> None:
    payload = {
        "ok": True,
        "result": {
            "task": {
                "id": "task_sub_01",
                "spec": "Run unit tests and check regression",
                "taskTitle": "QA Regression",
                "displayName": "QA",
                "status": "pending",
                "deps": ["task_dev_01"],
                "parentId": "task_parent",
            },
            "runId": "run_main",
        },
    }
    with patch.object(
        orca_client,
        "_run_orca_cmd",
        new_callable=AsyncMock,
        return_value=payload,
    ) as mock_cmd:
        task = await orca_client.orchestration_task_create(
            spec="Run unit tests and check regression",
            deps=["task_dev_01"],
            task_title="QA Regression",
            display_name="QA",
            parent_id="task_parent",
            run_id="run_main",
            from_handle="lead_handle",
        )
        assert isinstance(task, OrcaTaskDescriptor)
        assert task.task_id == "task_sub_01"
        assert task.task_title == "QA Regression"
        assert task.display_name == "QA"
        assert task.deps == ["task_dev_01"]
        assert task.parent_id == "task_parent"
        assert task.run_id == "run_main"

        mock_cmd.assert_called_once_with(
            [
                "orchestration",
                "task-create",
                "--spec",
                "Run unit tests and check regression",
                "--deps",
                '["task_dev_01"]',
                "--task-title",
                "QA Regression",
                "--display-name",
                "QA",
                "--parent",
                "task_parent",
                "--run",
                "run_main",
                "--from",
                "lead_handle",
            ]
        )


@pytest.mark.asyncio
async def test_orchestration_task_list(orca_client: OrcaCLIClient) -> None:
    payload = {
        "ok": True,
        "result": {
            "runId": "run_01",
            "tasks": [
                {
                    "id": "t1",
                    "spec": "Spec 1",
                    "taskTitle": "Title 1",
                    "status": "settled",
                    "deps": [],
                },
                {
                    "id": "t2",
                    "spec": "Spec 2",
                    "taskTitle": "Title 2",
                    "status": "ready",
                    "deps": ["t1"],
                },
            ],
            "count": 2,
        },
    }
    with patch.object(
        orca_client,
        "_run_orca_cmd",
        new_callable=AsyncMock,
        return_value=payload,
    ) as mock_cmd:
        tasks = await orca_client.orchestration_task_list(
            run_id="run_01",
            ready_only=True,
            brief=True,
        )
        assert len(tasks) == 2
        assert tasks[0].task_id == "t1"
        assert tasks[1].task_id == "t2"
        assert tasks[1].deps == ["t1"]
        assert tasks[1].run_id == "run_01"

        mock_cmd.assert_called_once_with(
            ["orchestration", "task-list", "--run", "run_01", "--ready", "--brief"]
        )


# =============================================================================
# Decision Gate Tests
# =============================================================================


@pytest.mark.asyncio
async def test_orchestration_gate_create(orca_client: OrcaCLIClient) -> None:
    payload = {
        "ok": True,
        "result": {
            "gate": {
                "id": "gate_101",
                "taskId": "task_55",
                "question": "Deploy to production?",
                "options": ["yes", "no"],
                "status": "pending",
                "resolution": None,
                "runId": "run_prod",
            }
        },
    }
    with patch.object(
        orca_client,
        "_run_orca_cmd",
        new_callable=AsyncMock,
        return_value=payload,
    ) as mock_cmd:
        gate = await orca_client.orchestration_gate_create(
            task_id="task_55",
            question="Deploy to production?",
            options=["yes", "no"],
            from_handle="lead_handle",
        )
        assert isinstance(gate, OrcaGateDescriptor)
        assert gate.gate_id == "gate_101"
        assert gate.task_id == "task_55"
        assert gate.options == ["yes", "no"]
        assert gate.status == "pending"
        assert gate.resolution is None

        mock_cmd.assert_called_once_with(
            [
                "orchestration",
                "gate-create",
                "--task",
                "task_55",
                "--question",
                "Deploy to production?",
                "--options",
                '["yes", "no"]',
                "--from",
                "lead_handle",
            ]
        )


@pytest.mark.asyncio
async def test_orchestration_gate_resolve(orca_client: OrcaCLIClient) -> None:
    with patch.object(
        orca_client,
        "_run_orca_cmd",
        new_callable=AsyncMock,
        return_value={"ok": True, "result": {"resolved": True}},
    ) as mock_cmd:
        res = await orca_client.orchestration_gate_resolve(
            gate_id="gate_101",
            resolution="yes",
            from_handle="operator_1",
        )
        assert res is True
        mock_cmd.assert_called_once_with(
            [
                "orchestration",
                "gate-resolve",
                "--id",
                "gate_101",
                "--resolution",
                "yes",
                "--from",
                "operator_1",
            ]
        )


@pytest.mark.asyncio
async def test_orchestration_gate_list(orca_client: OrcaCLIClient) -> None:
    payload = {
        "ok": True,
        "result": {
            "gates": [
                {
                    "id": "gate_1",
                    "taskId": "t_10",
                    "question": "Approve schema change?",
                    "options": ["approve", "reject"],
                    "status": "resolved",
                    "resolution": "approve",
                    "runId": "run_01",
                }
            ]
        },
    }
    with patch.object(
        orca_client,
        "_run_orca_cmd",
        new_callable=AsyncMock,
        return_value=payload,
    ) as mock_cmd:
        gates = await orca_client.orchestration_gate_list(
            task_id="t_10",
            run_id="run_01",
        )
        assert len(gates) == 1
        assert gates[0].gate_id == "gate_1"
        assert gates[0].resolution == "approve"
        mock_cmd.assert_called_once_with(
            ["orchestration", "gate-list", "--task", "t_10", "--run", "run_01"]
        )


# =============================================================================
# Messaging Tests (Send, Check, Reply, Ask)
# =============================================================================


@pytest.mark.asyncio
async def test_orchestration_send(orca_client: OrcaCLIClient) -> None:
    with patch.object(
        orca_client,
        "_run_orca_cmd",
        new_callable=AsyncMock,
        return_value={"ok": True, "result": {"messageId": "msg_001"}},
    ) as mock_cmd:
        res = await orca_client.orchestration_send(
            subject="Refactored user authentication",
            to_handle="dispatch:disp_01",
            run_id="run_01",
            from_handle="worker_01",
            body="Replaced sessions with JWT. Unit tests passing. Need review.",
            msg_type="worker_done",
            outcome="succeeded",
            files_modified=["src/auth.ts", "tests/auth.test.ts"],
            phase="completed",
        )
        assert res.get("messageId") == "msg_001"
        mock_cmd.assert_called_once_with(
            [
                "orchestration",
                "send",
                "--subject",
                "Refactored user authentication",
                "--type",
                "worker_done",
                "--to",
                "dispatch:disp_01",
                "--run",
                "run_01",
                "--from",
                "worker_01",
                "--body",
                "Replaced sessions with JWT. Unit tests passing. Need review.",
                "--outcome",
                "succeeded",
                "--files-modified",
                "src/auth.ts,tests/auth.test.ts",
                "--phase",
                "completed",
            ]
        )


@pytest.mark.asyncio
async def test_orchestration_check(orca_client: OrcaCLIClient) -> None:
    payload = {
        "ok": True,
        "result": {
            "deliveryId": "deliv_123",
            "messages": [
                {
                    "id": "msg_01",
                    "type": "heartbeat",
                    "subject": "alive",
                }
            ],
            "count": 1,
        },
    }
    with patch.object(
        orca_client,
        "_run_orca_cmd",
        new_callable=AsyncMock,
        return_value=payload,
    ) as mock_cmd:
        res = await orca_client.orchestration_check(
            terminal_handle="term_01",
            ack_delivery_id="deliv_old",
            wait=True,
            timeout_ms=15000,
            types=["heartbeat", "worker_done"],
        )
        assert res.get("result", {}).get("deliveryId") == "deliv_123"
        mock_cmd.assert_called_once_with(
            [
                "orchestration",
                "check",
                "--terminal",
                "term_01",
                "--ack",
                "deliv_old",
                "--wait",
                "--timeout-ms",
                "15000",
                "--types",
                "heartbeat,worker_done",
            ],
            timeout=20.0,
        )


@pytest.mark.asyncio
async def test_orchestration_reply(orca_client: OrcaCLIClient) -> None:
    with patch.object(
        orca_client,
        "_run_orca_cmd",
        new_callable=AsyncMock,
        return_value={"ok": True, "result": {"replied": True}},
    ) as mock_cmd:
        res = await orca_client.orchestration_reply(
            message_id="msg_q1",
            body="Use PostgreSQL dialect for migrations.",
            run_id="run_01",
            from_handle="lead_handle",
        )
        assert res is True
        mock_cmd.assert_called_once_with(
            [
                "orchestration",
                "reply",
                "--id",
                "msg_q1",
                "--body",
                "Use PostgreSQL dialect for migrations.",
                "--run",
                "run_01",
                "--from",
                "lead_handle",
            ]
        )


@pytest.mark.asyncio
async def test_orchestration_ask(orca_client: OrcaCLIClient) -> None:
    payload = {
        "ok": True,
        "result": {
            "messageId": "msg_ask_1",
            "answer": "choice-a",
        },
    }
    with patch.object(
        orca_client,
        "_run_orca_cmd",
        new_callable=AsyncMock,
        return_value=payload,
    ) as mock_cmd:
        res = await orca_client.orchestration_ask(
            question="Which database engine?",
            options=["choice-a", "choice-b"],
            to_handle="run:run_01",
            from_handle="worker_handle",
            timeout_ms=30000,
        )
        assert res.get("result", {}).get("answer") == "choice-a"
        mock_cmd.assert_called_once_with(
            [
                "orchestration",
                "ask",
                "--question",
                "Which database engine?",
                "--options",
                "choice-a,choice-b",
                "--to",
                "run:run_01",
                "--from",
                "worker_handle",
                "--timeout-ms",
                "30000",
            ],
            timeout=40.0,
        )


# =============================================================================
# Descriptors Serialization Tests
# =============================================================================


def test_descriptors_to_dict() -> None:
    run_desc = OrcaRunDescriptor(
        run_id="r1",
        objective="Obj",
        coordinator_handle="coord",
        consumer_generation=2,
        legacy=False,
        created_at="2026-11-01T00:00:00Z",
    )
    d = run_desc.to_dict()
    assert d["run_id"] == "r1"
    assert d["consumer_generation"] == 2

    task_desc = OrcaTaskDescriptor(
        task_id="t1",
        spec="Spec",
        deps=["dep1"],
        status="ready",
    )
    td = task_desc.to_dict()
    assert td["task_id"] == "t1"
    assert td["deps"] == ["dep1"]

    gate_desc = OrcaGateDescriptor(
        gate_id="g1",
        task_id="t1",
        question="Q?",
        options=["opt1"],
    )
    gd = gate_desc.to_dict()
    assert gd["gate_id"] == "g1"
    assert gd["options"] == ["opt1"]

    msg_desc = OrcaMessageDescriptor(
        message_id="m1",
        msg_type="heartbeat",
        subject="alive",
    )
    md = msg_desc.to_dict()
    assert md["message_id"] == "m1"
    assert md["msg_type"] == "heartbeat"
