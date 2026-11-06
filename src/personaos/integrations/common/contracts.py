"""PersonaOS Common Integration Contracts.

Defines independent, normalized execution and workspace contracts for external
runtimes and hosts (such as Orca). Completely decoupled from Orca internals and
individual agent CLI proprietary formats.

Invariants:
- Native session IDs, terminal handles, and transcripts remain opaque references.
- PersonaOS remains the sole authority for task claiming, state machine, and completion.
- Adapters return normalized results and evidence to the PersonaOS kernel.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class ExecutionStatus(StrEnum):
    """Normalized lifecycle states for an external execution."""

    PENDING = "pending"
    PREPARING = "preparing"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class AgentDescriptor:
    """Agent runtime identity and parameters."""

    kind: str  # e.g., 'codex', 'claude', 'agy', 'openclaw', 'mock', 'custom'
    model: str | None = None
    effort: str | None = None
    system_prompt: str | None = None
    options: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "model": self.model,
            "effort": self.effort,
            "system_prompt": self.system_prompt,
            "options": self.options,
        }


@dataclass(frozen=True)
class WorktreeDescriptor:
    """Normalized workspace / git worktree information."""

    worktree_id: str
    instance_id: str
    repo_id: str
    path: Path
    branch: str
    head: str
    display_name: str
    is_main: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "worktree_id": self.worktree_id,
            "instance_id": self.instance_id,
            "repo_id": self.repo_id,
            "path": str(self.path),
            "branch": self.branch,
            "head": self.head,
            "display_name": self.display_name,
            "is_main": self.is_main,
        }


@dataclass(frozen=True)
class ExecutionRequest:
    """Worker and task execution request dispatched from PersonaOS to an execution adapter."""

    task_id: str
    worker_id: str
    profile_version: int
    agent: AgentDescriptor
    target_worktree: str | None = None  # None, worktree ID, path, or 'active'
    command: str | None = None  # Shell or mock command to run (if custom/mock)
    prompt: str | None = None  # Prompt/instruction payload for agent
    timeout_seconds: float = 300.0
    skills: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: f"req-{uuid.uuid4().hex[:12]}")
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "profile_version": self.profile_version,
            "agent": self.agent.to_dict(),
            "target_worktree": self.target_worktree,
            "command": self.command,
            "prompt": self.prompt,
            "timeout_seconds": self.timeout_seconds,
            "skills": self.skills,
            "permissions": self.permissions,
            "environment": self.environment,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class ExecutionHandle:
    """Opaque reference to an actively running or provisioned execution in the host."""

    handle_id: str
    request_id: str
    worktree_id: str
    started_at: datetime
    agent: AgentDescriptor
    native_session_id: str | None = None  # Opaque Orca / CLI session ID
    native_terminal_id: str | None = None  # Opaque Orca terminal ID

    def to_dict(self) -> dict[str, Any]:
        return {
            "handle_id": self.handle_id,
            "request_id": self.request_id,
            "worktree_id": self.worktree_id,
            "started_at": self.started_at.isoformat(),
            "agent": self.agent.to_dict(),
            "native_session_id": self.native_session_id,
            "native_terminal_id": self.native_terminal_id,
        }


@dataclass(frozen=True)
class ExecutionEvent:
    """Normalized execution lifecycle event emitted by the host adapter."""

    event_id: str
    handle_id: str
    task_id: str
    worker_id: str
    status: ExecutionStatus
    timestamp: datetime
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "handle_id": self.handle_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "status": self.status.value,
            "timestamp": self.timestamp.isoformat(),
            "message": self.message,
            "details": self.details,
        }


@dataclass(frozen=True)
class ExecutionResult:
    """Execution outcome, evidence, and status returned to PersonaOS kernel."""

    request_id: str
    handle: ExecutionHandle
    status: ExecutionStatus
    exit_code: int
    started_at: datetime
    completed_at: datetime
    duration_seconds: float
    output_summary: str
    raw_terminal_output: str = ""
    modified_files: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "handle": self.handle.to_dict(),
            "status": self.status.value,
            "exit_code": self.exit_code,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "duration_seconds": self.duration_seconds,
            "output_summary": self.output_summary,
            "raw_terminal_output": self.raw_terminal_output,
            "modified_files": self.modified_files,
            "decisions": self.decisions,
            "error_message": self.error_message,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class OrcaRunDescriptor:
    """External coordination run in Orca Orchestration."""

    run_id: str
    objective: str
    coordinator_handle: str | None = None
    consumer_generation: int = 0
    legacy: bool = False
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "objective": self.objective,
            "coordinator_handle": self.coordinator_handle,
            "consumer_generation": self.consumer_generation,
            "legacy": self.legacy,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class OrcaTaskDescriptor:
    """External task representation within an Orca Orchestration Run."""

    task_id: str
    spec: str
    task_title: str | None = None
    display_name: str | None = None
    status: str = "pending"
    deps: list[str] = field(default_factory=list)
    parent_id: str | None = None
    run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "spec": self.spec,
            "task_title": self.task_title,
            "display_name": self.display_name,
            "status": self.status,
            "deps": self.deps,
            "parent_id": self.parent_id,
            "run_id": self.run_id,
        }


@dataclass(frozen=True)
class OrcaGateDescriptor:
    """Decision gate blocking a task in Orca Orchestration."""

    gate_id: str
    task_id: str
    question: str
    options: list[str] = field(default_factory=list)
    status: str = "pending"
    resolution: str | None = None
    run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "task_id": self.task_id,
            "question": self.question,
            "options": self.options,
            "status": self.status,
            "resolution": self.resolution,
            "run_id": self.run_id,
        }


@dataclass(frozen=True)
class OrcaMessageDescriptor:
    """Inter-agent or coordinator lifecycle message in Orca Orchestration."""

    message_id: str
    msg_type: str
    subject: str
    from_handle: str | None = None
    to_handle: str | None = None
    body: str | None = None
    task_id: str | None = None
    dispatch_id: str | None = None
    outcome: str | None = None
    files_modified: list[str] = field(default_factory=list)
    phase: str | None = None
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "msg_type": self.msg_type,
            "subject": self.subject,
            "from_handle": self.from_handle,
            "to_handle": self.to_handle,
            "body": self.body,
            "task_id": self.task_id,
            "dispatch_id": self.dispatch_id,
            "outcome": self.outcome,
            "files_modified": self.files_modified,
            "phase": self.phase,
            "created_at": self.created_at,
        }
