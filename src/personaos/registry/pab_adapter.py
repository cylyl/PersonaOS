"""PersonaOS External Agent Bridge Adapter (PAB).

Provides loosely coupled subprocess invocation for synchronizing PersonaOS-owned
worker profiles and skills to external agent platforms (OpenClaw, agy, Codex, Claude, MiniMax).

Invariants:
- PersonaOS remains the single source of truth for worker identity, profiles, and state.
- PAB is invoked strictly out-of-process via subprocess execution (no direct `import pab`).
- Shell string interpolation is forbidden (validated argument arrays only).
- Credentials and private session keys are never passed through CLI parameters.
- Synchronization failures are non-blocking: profile activation never rolls back on sync error.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SAFE_ID_REGEX = re.compile(r"^[a-zA-Z0-9_\-]+$")
ALLOWED_PROVIDERS = frozenset(["openclaw", "agy", "codex", "claude", "minimax", "all"])


class SyncStatus(StrEnum):
    """Synchronization lifecycle outcome status."""

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class TargetSyncResult:
    """Outcome for an individual target provider."""

    provider: str
    status: SyncStatus
    action: str = "none"  # e.g., 'created', 'updated', 'skipped', 'failed', 'planned'
    details: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class SyncOperationResult:
    """Consolidated synchronization receipt for a worker profile or skill."""

    operation_id: str
    worker_id: str
    profile_version: int
    requested_at: datetime
    completed_at: datetime
    overall_status: SyncStatus
    dry_run: bool
    targets: dict[str, TargetSyncResult]
    pab_command: list[str]
    exit_code: int
    stdout_summary: str = ""
    stderr_summary: str = ""
    audit_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize result to a JSON-compatible dictionary."""
        return {
            "operation_id": self.operation_id,
            "worker_id": self.worker_id,
            "profile_version": self.profile_version,
            "requested_at": self.requested_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "overall_status": self.overall_status.value,
            "dry_run": self.dry_run,
            "targets": {
                k: {
                    "provider": v.provider,
                    "status": v.status.value,
                    "action": v.action,
                    "details": v.details,
                    "warnings": v.warnings,
                    "error": v.error,
                }
                for k, v in self.targets.items()
            },
            "pab_command": self.pab_command,
            "exit_code": self.exit_code,
            "stdout_summary": self.stdout_summary,
            "stderr_summary": self.stderr_summary,
            "audit_metadata": self.audit_metadata,
        }


class PABSyncAdapter:
    """Asynchronous subprocess wrapper governing external PAB CLI operations."""

    def __init__(
        self,
        pab_binary: str | None = None,
        pab_home: Path | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        resolved_bin = pab_binary or os.environ.get("PAB_BIN")
        if not resolved_bin:
            resolved_bin = shutil.which("pab") or "/root/.local/bin/pab"
        self._pab_binary = resolved_bin

        home_env = os.environ.get("PAB_HOME")
        if pab_home:
            self._pab_home = pab_home.expanduser().resolve()
        elif home_env:
            self._pab_home = Path(home_env).expanduser().resolve()
        else:
            self._pab_home = (Path.home() / ".pab").resolve()

        self._timeout = timeout_seconds

    @property
    def pab_binary(self) -> str:
        """Configured path to PAB executable."""
        return self._pab_binary

    @property
    def pab_home(self) -> Path:
        """Configured PAB home directory."""
        return self._pab_home

    def is_available(self) -> bool:
        """Verify whether PAB binary exists and is executable."""
        p = Path(self._pab_binary)
        return p.is_file() and os.access(p, os.X_OK)

    def _sanitize_env(self) -> dict[str, str]:
        """Construct sanitized environment variables without leaking credentials."""
        safe_keys = {
            "PATH",
            "HOME",
            "USER",
            "LOGNAME",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TERM",
            "SHELL",
            "OPENCLAW_HOME",
            "CODEX_HOME",
            "CLAUDE_HOME",
            "AGY_HOME",
            "MINIMAX_HOME",
        }
        env = {k: v for k, v in os.environ.items() if k in safe_keys}
        env["PAB_HOME"] = str(self._pab_home)

        # Explicitly scrub any token or secret that might be in the environment
        for k in list(env.keys()):
            upper_k = k.upper()
            if any(sec in upper_k for sec in ("KEY", "SECRET", "TOKEN", "PASS", "AUTH")):
                env.pop(k, None)

        return env

    async def _execute_subprocess(
        self,
        cmd: list[str],
        timeout: float | None = None,
    ) -> tuple[int, str, str]:
        """Execute a command array without shell interpolation.

        Returns (exit_code, stdout, stderr).
        """
        timeout_to_use = timeout or self._timeout
        env = self._sanitize_env()

        logger.debug("Executing PAB command: %s", " ".join(cmd))
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_to_use,
            )
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            exit_code = proc.returncode if proc.returncode is not None else 1
            return exit_code, stdout, stderr
        except asyncio.TimeoutError:
            logger.error("PAB command timed out after %s seconds: %s", timeout_to_use, cmd)
            if proc is not None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            return -1, "", f"Operation timed out after {timeout_to_use}s"
        except FileNotFoundError as exc:
            logger.error("PAB binary not found: %s", self._pab_binary)
            return 127, "", f"Binary not found: {exc}"
        except Exception as exc:
            logger.error("Unexpected error executing PAB command: %s", exc)
            return 1, "", str(exc)

    async def get_provider_readiness(self) -> dict[str, Any]:
        """Query `pab providers list --json` to detect provider health."""
        if not self.is_available():
            return {
                "available": False,
                "error": f"PAB binary not found at '{self._pab_binary}'",
                "providers": {},
            }

        cmd = [self._pab_binary, "providers", "list", "--json"]
        code, stdout, stderr = await self._execute_subprocess(cmd)

        if code == 0:
            try:
                data = json.loads(stdout)
                return {"available": True, "providers": data}
            except json.JSONDecodeError:
                pass

        # Fallback to text parsing if needed
        return {
            "available": True,
            "raw_output": stdout,
            "error": stderr if code != 0 else None,
            "providers": {},
        }

    def _validate_inputs(
        self,
        worker_id: str,
        profile_path: Path,
        profile_version: int,
        target_providers: list[str] | None,
    ) -> list[str]:
        """Validate safety constraints on worker profile arguments."""
        errors: list[str] = []
        if not SAFE_ID_REGEX.match(worker_id):
            errors.append(f"Invalid worker_id '{worker_id}': must match {SAFE_ID_REGEX.pattern}")

        if profile_version < 1:
            errors.append(f"Invalid profile_version {profile_version}: must be >= 1")

        if not profile_path.is_file():
            errors.append(f"Profile path does not exist or is not a file: {profile_path}")

        if target_providers:
            for t in target_providers:
                if t.lower() not in ALLOWED_PROVIDERS:
                    errors.append(
                        f"Unsupported target provider '{t}'. Allowed: {sorted(ALLOWED_PROVIDERS)}"
                    )

        return errors

    async def sync_worker_profile(
        self,
        worker_id: str,
        profile_path: Path,
        profile_version: int,
        target_providers: list[str] | None = None,
        dry_run: bool = False,
        actor_id: str = "system",
        skills: list[str] | None = None,
    ) -> SyncOperationResult:
        """Validate, stage, and synchronize a worker profile to external providers.

        Lifecycle:
        1. Input validation & security checks.
        2. Dry-run plan or live deployment.
        3. If live:
           a. Import profile into PAB registry (`pab personaos import <profile_path>`)
           b. Deploy agent to target providers (`pab agent deploy <worker_id> --to <targets> --json`)
           c. Auto-provision declared skills (`pab skill deploy <skill> --to <targets> --json`)
        4. Consolidated structured result generation & audit record creation.
        """
        op_id = f"sync-{uuid.uuid4().hex[:12]}"
        requested_at = datetime.now(UTC)
        p_path = profile_path.expanduser().resolve()

        # Check availability
        if not self.is_available():
            return SyncOperationResult(
                operation_id=op_id,
                worker_id=worker_id,
                profile_version=profile_version,
                requested_at=requested_at,
                completed_at=datetime.now(UTC),
                overall_status=SyncStatus.UNAVAILABLE,
                dry_run=dry_run,
                targets={},
                pab_command=[],
                exit_code=127,
                stderr_summary=f"PAB binary '{self._pab_binary}' is unavailable or not executable.",
            )

        # Validate inputs
        val_errors = self._validate_inputs(worker_id, p_path, profile_version, target_providers)
        if val_errors:
            return SyncOperationResult(
                operation_id=op_id,
                worker_id=worker_id,
                profile_version=profile_version,
                requested_at=requested_at,
                completed_at=datetime.now(UTC),
                overall_status=SyncStatus.FAILED,
                dry_run=dry_run,
                targets={},
                pab_command=[],
                exit_code=1,
                stderr_summary="; ".join(val_errors),
            )

        targets_arg = ",".join(target_providers) if target_providers else "all"

        # --- Dry Run Mode ---
        if dry_run:
            plan_cmd = [self._pab_binary, "sync", "plan", "--to", targets_arg, "--json"]
            code, stdout, stderr = await self._execute_subprocess(plan_cmd)
            targets_result: dict[str, TargetSyncResult] = {}

            if code == 0:
                try:
                    plan_data = json.loads(stdout)
                    for op in plan_data.get("operations", []):
                        if op.get("resource_id") == worker_id:
                            p_name = op.get("target_provider", "unknown")
                            targets_result[p_name] = TargetSyncResult(
                                provider=p_name,
                                status=SyncStatus.SUCCESS,
                                action="planned",
                                details=op,
                            )
                except Exception as ex:
                    logger.warning("Failed to parse sync plan JSON: %s", ex)

            return SyncOperationResult(
                operation_id=op_id,
                worker_id=worker_id,
                profile_version=profile_version,
                requested_at=requested_at,
                completed_at=datetime.now(UTC),
                overall_status=SyncStatus.SUCCESS if code == 0 else SyncStatus.FAILED,
                dry_run=True,
                targets=targets_result,
                pab_command=plan_cmd,
                exit_code=code,
                stdout_summary=stdout[:500],
                stderr_summary=stderr[:500],
                audit_metadata={"actor_id": actor_id, "mode": "dry_run"},
            )

        # --- Live Sync Mode ---
        # Step 1: Import profile into canonical registry
        import_cmd = [self._pab_binary, "personaos", "import", str(p_path)]
        import_code, import_out, import_err = await self._execute_subprocess(import_cmd)
        if import_code != 0:
            return SyncOperationResult(
                operation_id=op_id,
                worker_id=worker_id,
                profile_version=profile_version,
                requested_at=requested_at,
                completed_at=datetime.now(UTC),
                overall_status=SyncStatus.FAILED,
                dry_run=False,
                targets={},
                pab_command=import_cmd,
                exit_code=import_code,
                stdout_summary=import_out[:500],
                stderr_summary=import_err[:500] or "Failed to import profile into PAB",
                audit_metadata={"actor_id": actor_id, "stage": "import"},
            )

        # Step 2: Deploy agent to target providers
        deploy_cmd = [
            self._pab_binary,
            "agent",
            "deploy",
            worker_id,
            "--to",
            targets_arg,
            "--json",
        ]
        deploy_code, deploy_out, deploy_err = await self._execute_subprocess(deploy_cmd)

        targets_map: dict[str, TargetSyncResult] = {}
        has_success = False
        has_failure = False

        if deploy_code == 0:
            try:
                deploy_data = json.loads(deploy_out)
                for prov_name, prov_res in deploy_data.items():
                    succ = prov_res.get("success", False)
                    if succ:
                        has_success = True
                        targets_map[prov_name] = TargetSyncResult(
                            provider=prov_name,
                            status=SyncStatus.SUCCESS,
                            action="deployed",
                            details=prov_res.get("result", {}),
                            warnings=prov_res.get("warnings", []),
                        )
                    else:
                        has_failure = True
                        targets_map[prov_name] = TargetSyncResult(
                            provider=prov_name,
                            status=SyncStatus.FAILED,
                            action="failed",
                            details=prov_res.get("result", {}),
                            warnings=prov_res.get("warnings", []),
                            error=prov_res.get("error", "Deployment failed"),
                        )
            except Exception as ex:
                logger.error("Failed to parse deploy JSON: %s", ex)
                has_failure = True
        else:
            has_failure = True

        # Step 3: Auto-deploy declared skills
        skills_synced: list[str] = []
        if skills:
            for skill_name in skills:
                if not SAFE_ID_REGEX.match(skill_name):
                    continue
                skill_cmd = [
                    self._pab_binary,
                    "skill",
                    "deploy",
                    skill_name,
                    "--to",
                    targets_arg,
                    "--json",
                ]
                s_code, s_out, _ = await self._execute_subprocess(skill_cmd)
                if s_code == 0:
                    skills_synced.append(skill_name)

        # Determine overall status
        if has_success and not has_failure:
            overall = SyncStatus.SUCCESS
        elif has_success and has_failure:
            overall = SyncStatus.PARTIAL
        else:
            overall = SyncStatus.FAILED

        return SyncOperationResult(
            operation_id=op_id,
            worker_id=worker_id,
            profile_version=profile_version,
            requested_at=requested_at,
            completed_at=datetime.now(UTC),
            overall_status=overall,
            dry_run=False,
            targets=targets_map,
            pab_command=deploy_cmd,
            exit_code=deploy_code,
            stdout_summary=deploy_out[:500],
            stderr_summary=deploy_err[:500],
            audit_metadata={
                "actor_id": actor_id,
                "profile_path": str(p_path),
                "skills_synced": skills_synced,
            },
        )

    async def sync_skill(
        self,
        skill_name: str,
        target_providers: list[str] | None = None,
        dry_run: bool = False,
        actor_id: str = "system",
    ) -> SyncOperationResult:
        """Deploy an approved PersonaOS skill manifest to external providers."""
        op_id = f"sync-skill-{uuid.uuid4().hex[:12]}"
        requested_at = datetime.now(UTC)

        if not self.is_available():
            return SyncOperationResult(
                operation_id=op_id,
                worker_id="",
                profile_version=0,
                requested_at=requested_at,
                completed_at=datetime.now(UTC),
                overall_status=SyncStatus.UNAVAILABLE,
                dry_run=dry_run,
                targets={},
                pab_command=[],
                exit_code=127,
                stderr_summary=f"PAB binary '{self._pab_binary}' unavailable.",
            )

        if not SAFE_ID_REGEX.match(skill_name):
            return SyncOperationResult(
                operation_id=op_id,
                worker_id="",
                profile_version=0,
                requested_at=requested_at,
                completed_at=datetime.now(UTC),
                overall_status=SyncStatus.FAILED,
                dry_run=dry_run,
                targets={},
                pab_command=[],
                exit_code=1,
                stderr_summary=f"Invalid skill_name '{skill_name}'",
            )

        targets_arg = ",".join(target_providers) if target_providers else "all"
        cmd = [self._pab_binary, "skill", "deploy", skill_name, "--to", targets_arg, "--json"]
        code, stdout, stderr = await self._execute_subprocess(cmd)

        targets_map: dict[str, TargetSyncResult] = {}
        if code == 0:
            try:
                data = json.loads(stdout)
                for prov_name, prov_res in data.items():
                    succ = prov_res.get("success", False)
                    targets_map[prov_name] = TargetSyncResult(
                        provider=prov_name,
                        status=SyncStatus.SUCCESS if succ else SyncStatus.FAILED,
                        action="deployed" if succ else "failed",
                        details=prov_res,
                        error=None if succ else "Skill deployment failed",
                    )
            except Exception:
                pass

        return SyncOperationResult(
            operation_id=op_id,
            worker_id="",
            profile_version=0,
            requested_at=requested_at,
            completed_at=datetime.now(UTC),
            overall_status=SyncStatus.SUCCESS if code == 0 else SyncStatus.FAILED,
            dry_run=dry_run,
            targets=targets_map,
            pab_command=cmd,
            exit_code=code,
            stdout_summary=stdout[:500],
            stderr_summary=stderr[:500],
            audit_metadata={"actor_id": actor_id, "skill_name": skill_name},
        )
