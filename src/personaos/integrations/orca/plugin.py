"""Orca ADE Plugin for PersonaOS.

Provides the visual and programmatic bridge between Orca ADE workspaces and
PersonaOS workforce management:
- Discover persistent AI workers (GET /workers)
- Enqueue tasks with Orca active worktree binding (POST /tasks)
- Track task checkpoints and execution status (GET /tasks/{id})
- Human-in-the-loop review inspection and decisioning (GET/POST /tasks/{id}/review-context)

Strict Architectural Invariants:
- Zero direct agent CLI calls: The Orca plugin NEVER invokes codex, claude, agy,
  or openclaw binaries directly.
- PersonaOS remains the authoritative owner of task lifecycle, leases, and review states.
- All external agent runtime processes are dispatched through PersonaOS -> PAB.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast

import httpx

from personaos.integrations.common.gateway_schemas import (
    GatewayDiagnosticsResponse,
    GatewayEventMessage,
    GatewayGateResolveResponse,
    GatewayHealthResponse,
    GatewayWorkerSummary,
)
from personaos.integrations.orca.client import OrcaCLIClient
from personaos.integrations.orca.exceptions import OrcaError

logger = logging.getLogger(__name__)


class PersonaOSOrcaPluginError(OrcaError):
    """Raised when Orca plugin encounters an error interacting with PersonaOS or Orca."""


class DirectAgentExecutionViolationError(PersonaOSOrcaPluginError):
    """Raised if any component attempts direct agent CLI invocation from the plugin."""


class PersonaOSOrcaPlugin:
    """Orca ADE Plugin bridging the visual IDE/workspaces to PersonaOS."""

    def __init__(
        self,
        api_base_url: str = "http://localhost:8000",
        api_key: str | None = None,
        orca_client: OrcaCLIClient | None = None,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.orca_client = orca_client or OrcaCLIClient()
        self.transport = transport

    def _create_client(self, timeout: float | None = None) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {"timeout": timeout if timeout is not None else self.timeout}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        return httpx.AsyncClient(**kwargs)

    def _get_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def list_workers(self) -> list[dict[str, Any]]:
        """List registered PersonaOS workers and their current status."""
        url = f"{self.api_base_url}/workers"
        async with self._create_client() as client:
            resp = await client.get(url, headers=self._get_headers())
            if resp.status_code != 200:
                raise PersonaOSOrcaPluginError(
                    f"Failed to list workers ({resp.status_code}): {resp.text}"
                )
            return cast(list[dict[str, Any]], resp.json())

    async def get_worker(self, worker_id: str) -> dict[str, Any]:
        """Fetch worker details and capabilities by worker ID."""
        url = f"{self.api_base_url}/workers/{worker_id}"
        async with self._create_client() as client:
            resp = await client.get(url, headers=self._get_headers())
            if resp.status_code != 200:
                raise PersonaOSOrcaPluginError(
                    f"Failed to get worker '{worker_id}' ({resp.status_code}): {resp.text}"
                )
            return cast(dict[str, Any], resp.json())

    async def submit_task(
        self,
        worker_id: str,
        title: str,
        objective: str,
        input_data: dict[str, Any] | None = None,
        required_skills: list[str] | None = None,
        required_capabilities: list[str] | None = None,
        priority: str = "medium",
        task_type: str | None = None,
        link_active_worktree: bool = True,
        dependencies: list[str] | None = None,
    ) -> dict[str, Any]:
        """Enqueue a new task into PersonaOS, automatically linking Orca worktree context."""
        payload_input = dict(input_data or {})

        # Bind active Orca worktree context if available and requested
        if link_active_worktree and "worktree" not in payload_input:
            try:
                if self.orca_client.is_available():
                    wt = await self.orca_client.get_current_worktree()
                    payload_input["worktree"] = {
                        "worktree_id": wt.worktree_id,
                        "repo_id": wt.repo_id,
                        "path": str(wt.path),
                        "branch": wt.branch,
                        "head": wt.head,
                        "display_name": wt.display_name,
                    }
                    payload_input["workspace_path"] = str(wt.path)
            except Exception as exc:
                logger.debug("Could not link active Orca worktree: %s", exc)

        body = {
            "worker_id": worker_id,
            "title": title,
            "objective": objective,
            "input": payload_input,
            "required_skills": required_skills or [],
            "required_capabilities": required_capabilities or [],
            "priority": priority,
            "type": task_type,
            "dependencies": dependencies or [],
            "side_effect_classification": "UNKNOWN",
        }

        url = f"{self.api_base_url}/tasks"
        async with self._create_client() as client:
            resp = await client.post(url, json=body, headers=self._get_headers())
            if resp.status_code not in (200, 201):
                raise PersonaOSOrcaPluginError(
                    f"Failed to enqueue task ({resp.status_code}): {resp.text}"
                )
            return cast(dict[str, Any], resp.json())

    async def get_task(self, task_id: str) -> dict[str, Any]:
        """Retrieve task details, execution state, and progress."""
        url = f"{self.api_base_url}/tasks/{task_id}"
        async with self._create_client() as client:
            resp = await client.get(url, headers=self._get_headers())
            if resp.status_code != 200:
                raise PersonaOSOrcaPluginError(
                    f"Failed to get task '{task_id}' ({resp.status_code}): {resp.text}"
                )
            return cast(dict[str, Any], resp.json())

    async def get_task_review_context(self, task_id: str) -> dict[str, Any]:
        """Retrieve aggregated diagnostic context for a task awaiting human review."""
        url = f"{self.api_base_url}/tasks/{task_id}/review-context"
        async with self._create_client() as client:
            resp = await client.get(url, headers=self._get_headers())
            if resp.status_code != 200:
                raise PersonaOSOrcaPluginError(
                    f"Failed to get review context for task '{task_id}' "
                    f"({resp.status_code}): {resp.text}"
                )
            return cast(dict[str, Any], resp.json())

    async def approve_task(
        self,
        task_id: str,
        review_notes: str | None = None,
        feedback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Approve a task currently in review queue."""
        url = f"{self.api_base_url}/tasks/{task_id}/approve"
        body = {"review_notes": review_notes, "feedback": feedback or {}}
        async with self._create_client() as client:
            resp = await client.post(url, json=body, headers=self._get_headers())
            if resp.status_code != 200:
                raise PersonaOSOrcaPluginError(
                    f"Failed to approve task '{task_id}' ({resp.status_code}): {resp.text}"
                )
            return cast(dict[str, Any], resp.json())

    async def reject_task(
        self,
        task_id: str,
        reason: str,
        feedback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reject a task currently in review queue."""
        url = f"{self.api_base_url}/tasks/{task_id}/reject"
        body = {"reason": reason, "feedback": feedback or {}}
        async with self._create_client() as client:
            resp = await client.post(url, json=body, headers=self._get_headers())
            if resp.status_code != 200:
                raise PersonaOSOrcaPluginError(
                    f"Failed to reject task '{task_id}' ({resp.status_code}): {resp.text}"
                )
            return cast(dict[str, Any], resp.json())

    def guard_no_direct_agent_execution(self, command: list[str] | str) -> None:
        """Enforces that Orca plugin never executes external agent CLIs directly."""
        cmd_str = " ".join(command) if isinstance(command, list) else command
        prohibited = [
            "openclaw",
            "codex",
            "claude",
            "agy",
            "mcode",
            "dsh",
            "pi",
            "gemini",
            "cursor",
            "opencode",
            "opencode2",
        ]
        first_token = cmd_str.strip().split()[0].lower() if cmd_str.strip() else ""
        for agent in prohibited:
            if first_token == agent or f"/{agent}" in first_token:
                raise DirectAgentExecutionViolationError(
                    f"Direct CLI invocation of '{agent}' is prohibited in Orca ADE. "
                    "All agent executions must be routed via Task Queue -> PAB Control Plane."
                )

    # -------------------------------------------------------------------------
    # Standardized Gateway API Client Methods (/integration/*) - ADR 0041
    # -------------------------------------------------------------------------

    async def get_health(self) -> GatewayHealthResponse:
        """Check operational health of the Integration Gateway and upstream connectivity."""
        url = f"{self.api_base_url}/integration/health"
        async with self._create_client() as client:
            resp = await client.get(url, headers=self._get_headers())
            if resp.status_code != 200:
                raise PersonaOSOrcaPluginError(
                    f"Gateway health probe failed ({resp.status_code}): {resp.text}"
                )
            return GatewayHealthResponse.model_validate(resp.json())

    async def get_diagnostics(self, target_url: str | None = None) -> GatewayDiagnosticsResponse:
        """Execute deep connectivity diagnostics and return structured classification."""
        url = f"{self.api_base_url}/integration/diagnostics"
        params = {"target_url": target_url} if target_url else None
        async with self._create_client() as client:
            resp = await client.get(url, params=params, headers=self._get_headers())
            if resp.status_code != 200:
                raise PersonaOSOrcaPluginError(
                    f"Gateway diagnostics probe failed ({resp.status_code}): {resp.text}"
                )
            return GatewayDiagnosticsResponse.model_validate(resp.json())

    async def list_integration_workers(
        self, worker_status: str | None = None, role: str | None = None
    ) -> list[GatewayWorkerSummary]:
        """Fetch workforce roster via standardized Gateway API (/integration/workers)."""
        url = f"{self.api_base_url}/integration/workers"
        params: dict[str, Any] = {}
        if worker_status:
            params["status"] = worker_status
        if role:
            params["role"] = role
        async with self._create_client() as client:
            resp = await client.get(url, params=params or None, headers=self._get_headers())
            if resp.status_code != 200:
                raise PersonaOSOrcaPluginError(
                    f"Failed to list gateway workers ({resp.status_code}): {resp.text}"
                )
            items = cast(list[dict[str, Any]], resp.json())
            return [GatewayWorkerSummary.model_validate(w) for w in items]

    async def list_runs(self) -> list[dict[str, Any]]:
        """List active external coordination runs."""
        url = f"{self.api_base_url}/integration/runs"
        async with self._create_client() as client:
            resp = await client.get(url, headers=self._get_headers())
            if resp.status_code != 200:
                raise PersonaOSOrcaPluginError(
                    f"Failed to list coordination runs ({resp.status_code}): {resp.text}"
                )
            return cast(list[dict[str, Any]], resp.json())

    async def create_run(
        self,
        run_id: str,
        objective: str,
        coordinator_handle: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or link an external orchestration coordination run."""
        url = f"{self.api_base_url}/integration/runs"
        body = {
            "run_id": run_id,
            "objective": objective,
            "coordinator_handle": coordinator_handle,
            "metadata": metadata or {},
        }
        async with self._create_client() as client:
            resp = await client.post(url, json=body, headers=self._get_headers())
            if resp.status_code not in (200, 201):
                raise PersonaOSOrcaPluginError(
                    f"Failed to create coordination run ({resp.status_code}): {resp.text}"
                )
            return cast(dict[str, Any], resp.json())

    async def get_run_dag(self, run_id: str) -> dict[str, Any]:
        """Return pre-evaluated DAG projection with task nodes, dependencies, and waves."""
        url = f"{self.api_base_url}/integration/runs/{run_id}/dag"
        async with self._create_client() as client:
            resp = await client.get(url, headers=self._get_headers())
            if resp.status_code != 200:
                raise PersonaOSOrcaPluginError(
                    f"Failed to fetch DAG projection for run '{run_id}' "
                    f"({resp.status_code}): {resp.text}"
                )
            return cast(dict[str, Any], resp.json())

    async def get_run_ready_waves(self, run_id: str) -> dict[str, Any]:
        """Return ready-to-execute tasks grouped by evaluation wave."""
        url = f"{self.api_base_url}/integration/runs/{run_id}/ready-waves"
        async with self._create_client() as client:
            resp = await client.get(url, headers=self._get_headers())
            if resp.status_code != 200:
                raise PersonaOSOrcaPluginError(
                    f"Failed to fetch ready waves for run '{run_id}' "
                    f"({resp.status_code}): {resp.text}"
                )
            return cast(dict[str, Any], resp.json())

    async def list_decision_gates(self, run_id: str | None = None) -> list[dict[str, Any]]:
        """Return pending decision gates mapped from tasks in review status."""
        url = f"{self.api_base_url}/integration/decision-gates"
        params = {"run_id": run_id} if run_id else None
        async with self._create_client() as client:
            resp = await client.get(url, params=params, headers=self._get_headers())
            if resp.status_code != 200:
                raise PersonaOSOrcaPluginError(
                    f"Failed to list decision gates ({resp.status_code}): {resp.text}"
                )
            return cast(list[dict[str, Any]], resp.json())

    async def get_decision_gate(self, gate_id: str) -> dict[str, Any]:
        """Retrieve single decision gate context."""
        url = f"{self.api_base_url}/integration/decision-gates/{gate_id}"
        async with self._create_client() as client:
            resp = await client.get(url, headers=self._get_headers())
            if resp.status_code != 200:
                raise PersonaOSOrcaPluginError(
                    f"Failed to get decision gate '{gate_id}' ({resp.status_code}): {resp.text}"
                )
            return cast(dict[str, Any], resp.json())

    async def resolve_decision_gate(
        self,
        gate_id: str,
        decision: str,
        expected_generation: int,
        review_notes: str | None = None,
        selected_option: str | None = None,
    ) -> GatewayGateResolveResponse:
        """Resolve a decision gate with generation fencing."""
        url = f"{self.api_base_url}/integration/decision-gates/{gate_id}/resolve"
        body = {
            "decision": decision,
            "expected_generation": expected_generation,
            "review_notes": review_notes,
            "selected_option": selected_option,
        }
        async with self._create_client() as client:
            resp = await client.post(url, json=body, headers=self._get_headers())
            if resp.status_code != 200:
                raise PersonaOSOrcaPluginError(
                    f"Failed to resolve decision gate '{gate_id}' ({resp.status_code}): {resp.text}"
                )
            return GatewayGateResolveResponse.model_validate(resp.json())

    async def publish_event(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        event_id: str | None = None,
        timestamp: datetime | None = None,
    ) -> GatewayEventMessage:
        """Publish an event to the Integration Gateway SSE bus."""
        url = f"{self.api_base_url}/integration/events/publish"
        body: dict[str, Any] = {
            "event_id": event_id or f"evt-{int(time.time() * 1000)}",
            "event_type": event_type,
            "payload": payload or {},
            "timestamp": (timestamp or datetime.now(UTC)).isoformat(),
        }
        async with self._create_client() as client:
            resp = await client.post(url, json=body, headers=self._get_headers())
            if resp.status_code != 200:
                raise PersonaOSOrcaPluginError(
                    f"Failed to publish event ({resp.status_code}): {resp.text}"
                )
            return GatewayEventMessage.model_validate(resp.json())

    # -------------------------------------------------------------------------
    # Server-Sent Events (SSE) Lifecycle Streaming with Auto-Reconnection (M6)
    # -------------------------------------------------------------------------

    async def stream_events(
        self,
        run_id: str | None = None,
        last_event_id: str | None = None,
        heartbeat_interval: float = 15.0,
        auto_reconnect: bool = True,
        max_reconnect_attempts: int = 5,
        initial_backoff: float = 0.5,
        backoff_multiplier: float = 1.5,
        max_backoff: float = 10.0,
        max_events: int | None = None,
    ) -> AsyncIterator[GatewayEventMessage]:
        """Stream real-time lifecycle events with auto-reconnection and Last-Event-ID resumption.

        Yields GatewayEventMessage objects for all lifecycle transitions:
        - task_status_changed
        - ready_wave_advanced
        - gate_created
        - gate_resolved
        - heartbeat
        """
        current_last_id = last_event_id
        events_yielded = 0
        attempts = 0

        while True:
            params: dict[str, Any] = {"heartbeat_interval": heartbeat_interval}
            if run_id:
                params["run_id"] = run_id
            if current_last_id:
                params["last_event_id"] = current_last_id
            if max_events is not None:
                params["max_events"] = max_events

            headers = self._get_headers()
            if current_last_id:
                headers["Last-Event-ID"] = current_last_id

            url = f"{self.api_base_url}/integration/events"

            try:
                async with (
                    self._create_client(timeout=None) as client,
                    client.stream("GET", url, params=params, headers=headers) as resp,
                ):
                    if resp.status_code != 200:
                        err_body = await resp.aread()
                        err_msg = err_body.decode("utf-8", errors="replace")
                        raise PersonaOSOrcaPluginError(
                            f"SSE event stream failed with status {resp.status_code}: {err_msg}"
                        )

                    attempts = 0  # Connected successfully, reset retry backoff
                    current_event_id = None
                    current_event_type = None
                    current_data_lines: list[str] = []

                    async for line in resp.aiter_lines():
                        if line is None:
                            continue

                        line = line.rstrip("\r\n")

                        if not line:
                            # End of event frame, dispatch
                            if current_event_type or current_data_lines:
                                data_str = "\n".join(current_data_lines)
                                evt: GatewayEventMessage | None = None
                                if current_event_type == "heartbeat":
                                    hb_payload: dict[str, Any] = {}
                                    with contextlib.suppress(Exception):
                                        hb_payload = json.loads(data_str) if data_str else {}
                                    evt = GatewayEventMessage(
                                        event_id=current_event_id or f"hb-{time.time()}",
                                        event_type="heartbeat",
                                        payload=hb_payload,
                                        timestamp=datetime.now(UTC),
                                    )
                                elif data_str:
                                    try:
                                        evt = GatewayEventMessage.model_validate_json(data_str)
                                    except Exception:
                                        evt = GatewayEventMessage(
                                            event_id=current_event_id or f"evt-{time.time()}",
                                            event_type=current_event_type or "message",
                                            payload={"raw": data_str},
                                            timestamp=datetime.now(UTC),
                                        )

                                if evt is not None:
                                    if evt.event_id and not evt.event_id.startswith("hb-"):
                                        current_last_id = evt.event_id

                                    events_yielded += 1
                                    yield evt

                                    if max_events is not None and events_yielded >= max_events:
                                        return

                            # Reset frame buffer
                            current_event_id = None
                            current_event_type = None
                            current_data_lines = []
                            continue

                        if line.startswith(":"):
                            # SSE comment frame (e.g. ": connected")
                            continue

                        if line.startswith("id:"):
                            current_event_id = line[3:].strip()
                        elif line.startswith("event:"):
                            current_event_type = line[6:].strip()
                        elif line.startswith("data:"):
                            current_data_lines.append(line[5:].strip())
                        elif line.startswith("retry:"):
                            pass

                    # Flush remaining frame if stream ended without trailing blank line
                    if current_event_type or current_data_lines:
                        data_str = "\n".join(current_data_lines)
                        evt = None
                        if current_event_type == "heartbeat":
                            hb_payload = {}
                            with contextlib.suppress(Exception):
                                hb_payload = json.loads(data_str) if data_str else {}
                            evt = GatewayEventMessage(
                                event_id=current_event_id or f"hb-{time.time()}",
                                event_type="heartbeat",
                                payload=hb_payload,
                                timestamp=datetime.now(UTC),
                            )
                        elif data_str:
                            try:
                                evt = GatewayEventMessage.model_validate_json(data_str)
                            except Exception:
                                evt = GatewayEventMessage(
                                    event_id=current_event_id or f"evt-{time.time()}",
                                    event_type=current_event_type or "message",
                                    payload={"raw": data_str},
                                    timestamp=datetime.now(UTC),
                                )
                        if evt is not None:
                            if evt.event_id and not evt.event_id.startswith("hb-"):
                                current_last_id = evt.event_id
                            events_yielded += 1
                            yield evt

                    if max_events is not None and events_yielded >= max_events:
                        return

                    if not auto_reconnect:
                        return

                    # Server cleanly finished stream; wait and reconnect
                    backoff = min(
                        initial_backoff * (backoff_multiplier ** attempts), max_backoff
                    )
                    attempts += 1
                    await asyncio.sleep(backoff)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                if not auto_reconnect or attempts >= max_reconnect_attempts:
                    logger.error("SSE stream disconnected, aborting auto-reconnect", exc_info=exc)
                    raise PersonaOSOrcaPluginError(f"SSE stream failed: {exc}") from exc

                backoff = min(initial_backoff * (backoff_multiplier ** attempts), max_backoff)
                attempts += 1
                logger.warning(
                    "SSE stream disconnected, auto-reconnecting with Last-Event-ID",
                    extra={
                        "attempt": attempts,
                        "last_event_id": current_last_id,
                        "backoff": backoff,
                        "error": str(exc),
                    },
                )
                await asyncio.sleep(backoff)
