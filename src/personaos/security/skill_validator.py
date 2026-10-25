"""Skill validation and security scanning engine.

Per ADR 0028 and docs/specs/skill-validation-and-security-scanning.md:
- Multi-stage deterministic inspection (sandbox/traversal, manifest policy, Python AST).
- Resilient external adapter for NVIDIA SkillSpector.
- Fail-closed admission control producing APPROVE, CAUTION, or REJECT verdicts.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from personaos.registry.skill import (
    SkillManifest,
    SkillRegistry,
    SkillSecurityError,
)

logger = logging.getLogger(__name__)

DEFAULT_FORBIDDEN_TOOLS = {
    "rm_rf",
    "raw_socket",
    "root_exec",
    "eval",
    "kernel_module",
}

DANGEROUS_IMPORTS = {
    "ctypes",
    "pty",
}

SENSITIVE_PATTERNS = [
    "/etc/shadow",
    "/etc/sudoers",
    "id_rsa",
    "id_ed25519",
]


class ScanVerdict(StrEnum):
    """Overall security admission verdict for a skill."""

    APPROVE = "approve"
    CAUTION = "caution"
    REJECT = "reject"


class FindingSeverity(StrEnum):
    """Severity classification of a detected security finding."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


@dataclass(frozen=True)
class SecurityFinding:
    """Detailed record of a specific security finding."""

    rule_id: str
    severity: FindingSeverity
    message: str
    file_path: str | None = None
    line_number: int | None = None
    source: str = "native"

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity.value,
            "message": self.message,
            "file_path": self.file_path,
            "line_number": self.line_number,
            "source": self.source,
        }


@dataclass
class SkillScanReport:
    """Comprehensive security evaluation report for a skill bundle."""

    skill_name: str
    version: str
    verdict: ScanVerdict
    risk_score: float
    findings: list[SecurityFinding] = field(default_factory=list)
    scanner_metadata: dict[str, Any] = field(default_factory=dict)
    scanned_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_approved(self) -> bool:
        return self.verdict == ScanVerdict.APPROVE

    @property
    def has_critical(self) -> bool:
        return any(f.severity == FindingSeverity.CRITICAL for f in self.findings)

    @property
    def has_high(self) -> bool:
        return any(f.severity == FindingSeverity.HIGH for f in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "version": self.version,
            "verdict": self.verdict.value,
            "risk_score": self.risk_score,
            "findings": [f.to_dict() for f in self.findings],
            "scanner_metadata": dict(self.scanner_metadata),
            "scanned_at": self.scanned_at.isoformat(),
        }


class _ASTSecurityVisitor(ast.NodeVisitor):
    """AST visitor searching for dangerous function calls, imports, and string patterns."""

    def __init__(self, relative_path: str, allow_subprocess: bool = False) -> None:
        self.relative_path = relative_path
        self.allow_subprocess = allow_subprocess
        self.findings: list[SecurityFinding] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.name.split(".")[0]
            if name in DANGEROUS_IMPORTS:
                self.findings.append(
                    SecurityFinding(
                        rule_id="SEC-DANGEROUS-IMPORT",
                        severity=FindingSeverity.HIGH,
                        message=f"Dangerous module import '{name}' is disallowed in skill code.",
                        file_path=self.relative_path,
                        line_number=node.lineno,
                    )
                )
            elif name == "subprocess" and not self.allow_subprocess:
                self.findings.append(
                    SecurityFinding(
                        rule_id="SEC-DANGEROUS-IMPORT",
                        severity=FindingSeverity.HIGH,
                        message="Module 'subprocess' imported without explicit authorization.",
                        file_path=self.relative_path,
                        line_number=node.lineno,
                    )
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            name = node.module.split(".")[0]
            if name in DANGEROUS_IMPORTS:
                self.findings.append(
                    SecurityFinding(
                        rule_id="SEC-DANGEROUS-IMPORT",
                        severity=FindingSeverity.HIGH,
                        message=f"Dangerous module import from '{name}' is disallowed.",
                        file_path=self.relative_path,
                        line_number=node.lineno,
                    )
                )
            elif name == "subprocess" and not self.allow_subprocess:
                self.findings.append(
                    SecurityFinding(
                        rule_id="SEC-DANGEROUS-IMPORT",
                        severity=FindingSeverity.HIGH,
                        message="Module 'subprocess' imported without explicit authorization.",
                        file_path=self.relative_path,
                        line_number=node.lineno,
                    )
                )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        # Detect eval(), exec(), compile()
        if isinstance(func, ast.Name) and func.id in ("eval", "exec", "compile", "__import__"):
            self.findings.append(
                SecurityFinding(
                    rule_id="SEC-DYNAMIC-EXEC",
                    severity=FindingSeverity.HIGH,
                    message=f"Dynamic code execution via '{func.id}()' is prohibited.",
                    file_path=self.relative_path,
                    line_number=node.lineno,
                )
            )
        # Detect os.system(), os.popen()
        elif (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "os"
            and func.attr in ("system", "popen", "spawnl", "execv")
        ):
            self.findings.append(
                SecurityFinding(
                    rule_id="SEC-DYNAMIC-EXEC",
                    severity=FindingSeverity.HIGH,
                    message=f"Process execution via 'os.{func.attr}()' is prohibited.",
                    file_path=self.relative_path,
                    line_number=node.lineno,
                )
            )
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            val_lower = node.value.lower()
            for pattern in SENSITIVE_PATTERNS:
                if pattern in val_lower:
                    self.findings.append(
                        SecurityFinding(
                            rule_id="SEC-CREDENTIAL-LEAK",
                            severity=FindingSeverity.HIGH,
                            message=f"Sensitive path pattern '{pattern}' referenced in code.",
                            file_path=self.relative_path,
                            line_number=node.lineno,
                        )
                    )
        self.generic_visit(node)


class SkillValidator:
    """Security validator and scanner for AI agent skills."""

    def __init__(
        self,
        forbidden_tools: set[str] | None = None,
        max_allowed_risk: float = 60.0,
        skillspector_bin: str | None = None,
        allow_subprocess: bool = False,
        reject_on_high: bool = True,
    ) -> None:
        self.forbidden_tools = set(forbidden_tools or DEFAULT_FORBIDDEN_TOOLS)
        self.max_allowed_risk = max_allowed_risk
        self.skillspector_bin = skillspector_bin or shutil.which("skillspector")
        self.allow_subprocess = allow_subprocess
        self.reject_on_high = reject_on_high

    def scan_bundle(
        self,
        bundle_path: Path,
        manifest: SkillManifest | None = None,
    ) -> SkillScanReport:
        """Execute full security inspection of a skill bundle directory."""
        findings: list[SecurityFinding] = []
        scanner_meta: dict[str, Any] = {}
        bundle_path = bundle_path.resolve()

        if not bundle_path.is_dir():
            findings.append(
                SecurityFinding(
                    rule_id="SEC-BUNDLE-MISSING",
                    severity=FindingSeverity.CRITICAL,
                    message=(
                        f"Skill bundle path does not exist or is not a directory: {bundle_path}"
                    ),
                )
            )
            return SkillScanReport(
                skill_name=manifest.name if manifest else "unknown",
                version=manifest.version if manifest else "0.0.0",
                verdict=ScanVerdict.REJECT,
                risk_score=100.0,
                findings=findings,
            )

        # 1. Symlink and Path Traversal Check
        bundle_root_str = str(bundle_path)
        for root_str, _, files in os.walk(bundle_path, followlinks=False):
            root = Path(root_str)
            for f in files:
                file_path = root / f
                # Check for symlink escaping bundle root
                if file_path.is_symlink():
                    try:
                        resolved = file_path.resolve()
                        if not str(resolved).startswith(bundle_root_str):
                            findings.append(
                                SecurityFinding(
                                    rule_id="SEC-SYMLINK-ESCAPE",
                                    severity=FindingSeverity.CRITICAL,
                                    message=(
                                        f"Symlink '{f}' resolves outside bundle root: {resolved}"
                                    ),
                                    file_path=str(file_path.relative_to(bundle_path)),
                                )
                            )
                    except Exception as exc:
                        findings.append(
                            SecurityFinding(
                                rule_id="SEC-SYMLINK-ESCAPE",
                                severity=FindingSeverity.CRITICAL,
                                message=f"Failed to safely resolve symlink '{f}': {exc}",
                                file_path=str(file_path.relative_to(bundle_path)),
                            )
                        )

        # 2. Manifest Policy Validation
        if manifest is not None:
            findings.extend(self.validate_manifest(manifest, bundle_path))

        # 3. Static Python AST Scanning
        for root_str, _, files in os.walk(bundle_path):
            root = Path(root_str)
            for f in files:
                if f.endswith(".py"):
                    py_file = root / f
                    findings.extend(self.scan_python_ast(py_file, bundle_path))

        # 4. NVIDIA SkillSpector Scanner Integration
        if self.skillspector_bin:
            skillspector_findings = self._run_skillspector(bundle_path)
            findings.extend(skillspector_findings)
            scanner_meta["skillspector"] = "executed"
        else:
            scanner_meta["skillspector"] = "cli_unavailable"

        # Calculate aggregate risk score
        risk_score = 0.0
        for finding in findings:
            if finding.severity == FindingSeverity.CRITICAL:
                risk_score += 100.0
            elif finding.severity == FindingSeverity.HIGH:
                risk_score += 35.0
            elif finding.severity == FindingSeverity.MEDIUM:
                risk_score += 15.0
            elif finding.severity == FindingSeverity.LOW:
                risk_score += 5.0
        risk_score = min(100.0, risk_score)

        # Determine verdict
        has_critical = any(f.severity == FindingSeverity.CRITICAL for f in findings)
        has_high = any(f.severity == FindingSeverity.HIGH for f in findings)
        if (
            has_critical
            or (self.reject_on_high and has_high)
            or risk_score >= self.max_allowed_risk
        ):
            verdict = ScanVerdict.REJECT
        elif findings:
            verdict = ScanVerdict.CAUTION
        else:
            verdict = ScanVerdict.APPROVE

        return SkillScanReport(
            skill_name=manifest.name if manifest else bundle_path.name,
            version=manifest.version if manifest else "1.0.0",
            verdict=verdict,
            risk_score=risk_score,
            findings=findings,
            scanner_metadata=scanner_meta,
        )

    def validate_manifest(
        self,
        manifest: SkillManifest,
        bundle_path: Path | None = None,
    ) -> list[SecurityFinding]:
        """Validate manifest against security policies."""
        findings: list[SecurityFinding] = []

        # Check for forbidden tools
        for tool in manifest.required_tools:
            if tool.lower().strip() in self.forbidden_tools:
                findings.append(
                    SecurityFinding(
                        rule_id="SEC-FORBIDDEN-TOOL",
                        severity=FindingSeverity.HIGH,
                        message=f"Required tool '{tool}' is on the organizational forbidden list.",
                    )
                )

        # Check entrypoint existence if bundle_path provided
        if manifest.entrypoint and bundle_path:
            ep_file = bundle_path / manifest.entrypoint
            if not ep_file.exists():
                findings.append(
                    SecurityFinding(
                        rule_id="SEC-ENTRYPOINT-MISSING",
                        severity=FindingSeverity.MEDIUM,
                        message=f"Declared entrypoint '{manifest.entrypoint}' does not exist.",
                    )
                )

        return findings

    def scan_python_ast(self, py_file: Path, base_dir: Path) -> list[SecurityFinding]:
        """Parse and scan a Python source file for dangerous operations."""
        rel_path = str(py_file.relative_to(base_dir))
        try:
            content = py_file.read_text(encoding="utf-8")
            tree = ast.parse(content, filename=str(py_file))
        except SyntaxError as exc:
            return [
                SecurityFinding(
                    rule_id="SEC-SYNTAX-ERROR",
                    severity=FindingSeverity.MEDIUM,
                    message=f"Python syntax error in skill file: {exc}",
                    file_path=rel_path,
                    line_number=exc.lineno,
                )
            ]
        except Exception as exc:
            return [
                SecurityFinding(
                    rule_id="SEC-PARSE-ERROR",
                    severity=FindingSeverity.LOW,
                    message=f"Unable to read file for analysis: {exc}",
                    file_path=rel_path,
                )
            ]

        visitor = _ASTSecurityVisitor(rel_path, allow_subprocess=self.allow_subprocess)
        visitor.visit(tree)
        return visitor.findings

    def _run_skillspector(self, bundle_path: Path) -> list[SecurityFinding]:
        """Invoke external SkillSpector CLI and parse results."""
        if not self.skillspector_bin:
            return []

        try:
            cmd = [
                self.skillspector_bin,
                "scan",
                str(bundle_path),
                "--no-llm",
                "--format",
                "json",
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if proc.returncode not in (0, 1):
                logger.warning(
                    "SkillSpector exited with status %d: %s", proc.returncode, proc.stderr
                )
                return []

            output_json = proc.stdout.strip()
            if not output_json:
                return []

            data = json.loads(output_json)
            findings: list[SecurityFinding] = []
            for item in data.get("findings", []):
                sev_raw = str(item.get("severity", "medium")).lower()
                sev = (
                    FindingSeverity.CRITICAL
                    if sev_raw == "critical"
                    else FindingSeverity.HIGH
                    if sev_raw == "high"
                    else FindingSeverity.LOW
                    if sev_raw == "low"
                    else FindingSeverity.MEDIUM
                )
                findings.append(
                    SecurityFinding(
                        rule_id=f"SKILLSPECTOR-{item.get('rule_id', 'UNKNOWN')}",
                        severity=sev,
                        message=item.get("description", "SkillSpector finding"),
                        file_path=item.get("file_path"),
                        line_number=item.get("line_number"),
                        source="skillspector",
                    )
                )
            return findings
        except Exception as exc:
            logger.warning("Error running SkillSpector: %s", exc)
            return []

    def validate_and_register(
        self,
        registry: SkillRegistry,
        manifest: SkillManifest,
        bundle_path: Path,
        bundle_files: dict[str, str | bytes] | None = None,
        overwrite: bool = False,
    ) -> tuple[SkillManifest, SkillScanReport]:
        """Validate skill bundle before registering into the skill registry.

        Raises:
            SkillSecurityError: If security scan results in REJECT verdict.
        """
        report = self.scan_bundle(bundle_path, manifest=manifest)
        if report.verdict == ScanVerdict.REJECT:
            reasons = "; ".join(f"{f.rule_id}: {f.message}" for f in report.findings)
            raise SkillSecurityError(
                f"Skill '{manifest.identifier}' failed security admission: {reasons}"
            )

        # Attach security audit metadata to manifest
        manifest.metadata["security_verdict"] = report.verdict.value
        manifest.metadata["risk_score"] = report.risk_score
        manifest.metadata["scanned_at"] = report.scanned_at.isoformat()
        if report.findings:
            manifest.metadata["security_warnings"] = [f.to_dict() for f in report.findings]

        registered = registry.register(manifest, bundle_files=bundle_files, overwrite=overwrite)
        return registered, report
