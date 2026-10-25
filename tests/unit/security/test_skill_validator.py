"""Unit tests for Skill Validation and Security Scanning (Vikunja #111, ADR 0028)."""

from pathlib import Path

import pytest

from personaos.registry.skill import (
    SkillManifest,
    SkillRegistry,
    SkillSecurityError,
)
from personaos.security.skill_validator import (
    ScanVerdict,
    SkillValidator,
)


def test_clean_skill_bundle_approved(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "clean_skill"
    bundle_dir.mkdir()
    (bundle_dir / "main.py").write_text(
        "def run():\n    return 'Hello, World!'\n",
        encoding="utf-8",
    )

    manifest = SkillManifest(
        name="clean-greeter",
        version="1.0.0",
        entrypoint="main.py",
        required_tools=["git"],
    )

    validator = SkillValidator()
    report = validator.scan_bundle(bundle_dir, manifest=manifest)

    assert report.verdict == ScanVerdict.APPROVE
    assert report.risk_score == 0.0
    assert report.is_approved is True
    assert len(report.findings) == 0


def test_ast_dynamic_code_exec_rejected(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "eval_skill"
    bundle_dir.mkdir()
    (bundle_dir / "bad.py").write_text(
        "def execute_user_code(user_input):\n"
        "    eval(user_input)\n"
        "    exec('import sys; sys.exit(0)')\n",
        encoding="utf-8",
    )

    manifest = SkillManifest(name="bad-runner", version="1.0.0")
    validator = SkillValidator()
    report = validator.scan_bundle(bundle_dir, manifest=manifest)

    assert report.verdict == ScanVerdict.REJECT
    assert report.has_high is True
    rules = [f.rule_id for f in report.findings]
    assert "SEC-DYNAMIC-EXEC" in rules


def test_ast_dangerous_imports_rejected(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "ctypes_skill"
    bundle_dir.mkdir()
    (bundle_dir / "native.py").write_text(
        "import ctypes\nimport pty\n",
        encoding="utf-8",
    )

    validator = SkillValidator()
    report = validator.scan_bundle(bundle_dir)

    assert report.verdict == ScanVerdict.REJECT
    rules = [f.rule_id for f in report.findings]
    assert "SEC-DANGEROUS-IMPORT" in rules


def test_ast_sensitive_path_reference(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "leak_skill"
    bundle_dir.mkdir()
    (bundle_dir / "stealer.py").write_text(
        "def read_system():\n    with open('/etc/shadow', 'r') as f:\n        return f.read()\n",
        encoding="utf-8",
    )

    validator = SkillValidator()
    report = validator.scan_bundle(bundle_dir)

    assert report.has_high is True
    rules = [f.rule_id for f in report.findings]
    assert "SEC-CREDENTIAL-LEAK" in rules


def test_symlink_escape_critical_rejection(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "secret.txt"
    outside_file.write_text("SUPER_SECRET_TOKEN")

    # Create symlink pointing outside bundle
    symlink_target = bundle_dir / "escaped_link"
    symlink_target.symlink_to(outside_file)

    validator = SkillValidator()
    report = validator.scan_bundle(bundle_dir)

    assert report.verdict == ScanVerdict.REJECT
    assert report.has_critical is True
    assert report.risk_score == 100.0
    rules = [f.rule_id for f in report.findings]
    assert "SEC-SYMLINK-ESCAPE" in rules


def test_forbidden_tools_in_manifest(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "tool_skill"
    bundle_dir.mkdir()

    manifest = SkillManifest(
        name="root-cleaner",
        version="1.0.0",
        required_tools=["git", "rm_rf", "root_exec"],
    )

    validator = SkillValidator(forbidden_tools={"rm_rf", "root_exec"})
    report = validator.scan_bundle(bundle_dir, manifest=manifest)

    assert report.verdict == ScanVerdict.REJECT
    forbidden_rules = [f for f in report.findings if f.rule_id == "SEC-FORBIDDEN-TOOL"]
    assert len(forbidden_rules) == 2


def test_missing_entrypoint_warning(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "missing_ep_skill"
    bundle_dir.mkdir()

    manifest = SkillManifest(
        name="broken-entry",
        version="1.0.0",
        entrypoint="scripts/non_existent.py",
    )

    validator = SkillValidator()
    report = validator.scan_bundle(bundle_dir, manifest=manifest)

    # Missing entrypoint alone is MEDIUM severity -> CAUTION
    assert report.verdict == ScanVerdict.CAUTION
    assert any(f.rule_id == "SEC-ENTRYPOINT-MISSING" for f in report.findings)


def test_fail_closed_registry_integration(tmp_path: Path) -> None:
    registry_dir = tmp_path / "registry"
    skills_dir = tmp_path / "source_skills"
    skills_dir.mkdir()

    bad_bundle = skills_dir / "bad_skill"
    bad_bundle.mkdir()
    (bad_bundle / "exploit.py").write_text("import ctypes\nctypes.CDLL(None)\n")

    manifest = SkillManifest(name="exploit-pack", version="1.0.0")

    registry = SkillRegistry(skills_dir=registry_dir)
    validator = SkillValidator()

    # Attempting to validate and register malicious skill raises SkillSecurityError
    with pytest.raises(SkillSecurityError, match="failed security admission"):
        validator.validate_and_register(registry, manifest, bad_bundle)

    # Clean skill registers successfully with audit metadata
    clean_bundle = skills_dir / "good_skill"
    clean_bundle.mkdir()
    (clean_bundle / "worker.py").write_text("def do_work(): pass\n")
    clean_manifest = SkillManifest(name="clean-pack", version="1.0.0", entrypoint="worker.py")

    registered, rep = validator.validate_and_register(registry, clean_manifest, clean_bundle)
    assert rep.verdict == ScanVerdict.APPROVE
    assert registered.metadata["security_verdict"] == "approve"
    assert "scanned_at" in registered.metadata
    assert registry.get_skill("clean-pack").name == "clean-pack"


def test_skillspector_mock_cli_integration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle_dir = tmp_path / "test_bundle"
    bundle_dir.mkdir()
    (bundle_dir / "dummy.py").write_text("pass\n")

    mock_json_output = (
        '{"findings": [{"rule_id": "YAML-INJECTION", '
        '"severity": "HIGH", "description": "Unsafe YAML loader"}]}'
    )

    class MockCompletedProcess:
        returncode = 0
        stdout = mock_json_output
        stderr = ""

    def mock_subprocess_run(*args, **kwargs):
        return MockCompletedProcess()

    monkeypatch.setattr("subprocess.run", mock_subprocess_run)

    validator = SkillValidator(skillspector_bin="/usr/local/bin/skillspector")
    report = validator.scan_bundle(bundle_dir)

    assert report.scanner_metadata["skillspector"] == "executed"
    finding_sources = [f.source for f in report.findings]
    assert "skillspector" in finding_sources
    assert any("SKILLSPECTOR-YAML-INJECTION" in f.rule_id for f in report.findings)
