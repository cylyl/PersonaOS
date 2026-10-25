"""Unit tests for the Versioned Skill Registry (Vikunja #110, ADR 0027)."""

from pathlib import Path

import pytest

from personaos.registry.skill import (
    SkillManifest,
    SkillNotFoundError,
    SkillRegistry,
    SkillSecurityError,
    SkillStatus,
    SkillTrustLevel,
    SkillValidationError,
    SkillVersionExistsError,
)


def test_manifest_validation_and_serialization() -> None:
    manifest = SkillManifest(
        name="code-reviewer",
        version="1.2.0",
        description="Automated code review skill",
        author="PersonaOS Team",
        entrypoint="scripts/run.py",
        required_tools=["git", "ast-grep"],
        capabilities=["code-analysis", "security-audit"],
        tags=["review", "python"],
        trust_level=SkillTrustLevel.INTERNAL,
        status=SkillStatus.ACTIVE,
    )

    assert manifest.identifier == "code-reviewer@1.2.0"
    digest = manifest.calculate_digest()
    assert len(digest) == 64

    # Dict roundtrip
    data = manifest.to_dict()
    assert data["name"] == "code-reviewer"
    assert data["version"] == "1.2.0"

    rebuilt = SkillManifest.from_dict(data)
    assert rebuilt.identifier == manifest.identifier
    assert rebuilt.required_tools == ["git", "ast-grep"]

    # YAML roundtrip
    yaml_str = manifest.to_yaml()
    assert "code-reviewer" in yaml_str
    rebuilt_yaml = SkillManifest.from_yaml(yaml_str)
    assert rebuilt_yaml.identifier == manifest.identifier


def test_manifest_invalid_fields() -> None:
    # Invalid name
    with pytest.raises(SkillValidationError, match="invalid"):
        SkillManifest(name="bad name with spaces!", version="1.0.0")

    # Invalid version
    with pytest.raises(SkillValidationError, match="Invalid semantic version"):
        SkillManifest(name="valid-name", version="not-a-semver")

    # Dangerous entrypoint (path traversal)
    with pytest.raises(SkillValidationError, match="parent traversals"):
        SkillManifest(name="valid-name", version="1.0.0", entrypoint="../../etc/passwd")

    with pytest.raises(SkillValidationError, match="cannot be absolute"):
        SkillManifest(name="valid-name", version="1.0.0", entrypoint="/bin/sh")


def test_multi_version_coexistence_and_immutability(tmp_path: Path) -> None:
    registry = SkillRegistry(skills_dir=tmp_path)

    v1 = SkillManifest(name="data-cleaner", version="1.0.0", description="v1")
    v2 = SkillManifest(name="data-cleaner", version="1.1.0", description="v2")
    v3 = SkillManifest(name="data-cleaner", version="2.0.0", description="v3")

    registry.register(v1)
    registry.register(v2)
    registry.register(v3)

    # All 3 versions coexist
    assert registry.get_skill("data-cleaner", "1.0.0").description == "v1"
    assert registry.get_skill("data-cleaner", "1.1.0").description == "v2"
    assert registry.get_skill("data-cleaner", "2.0.0").description == "v3"

    # Default/latest resolves to 2.0.0
    latest = registry.get_skill("data-cleaner")
    assert latest.version == "2.0.0"

    latest_explicit = registry.get_skill("data-cleaner", "latest")
    assert latest_explicit.version == "2.0.0"

    # name@version notation
    pinned = registry.get_skill("data-cleaner@1.1.0")
    assert pinned.version == "1.1.0"

    # Immutability: duplicate registration without overwrite fails
    dup = SkillManifest(name="data-cleaner", version="1.0.0", description="altered")
    with pytest.raises(SkillVersionExistsError):
        registry.register(dup, overwrite=False)

    # With overwrite=True, updates succeed
    registry.register(dup, overwrite=True)
    assert registry.get_skill("data-cleaner", "1.0.0").description == "altered"


def test_bundle_files_persistence_and_sandbox(tmp_path: Path) -> None:
    registry = SkillRegistry(skills_dir=tmp_path)

    manifest = SkillManifest(
        name="formatter",
        version="1.0.0",
        entrypoint="main.py",
    )
    files = {
        "main.py": "print('Formatting...')\n",
        "config.json": '{"indent": 4}',
    }
    registered = registry.register(manifest, bundle_files=files)
    assert registered.bundle_path is not None
    assert (registered.bundle_path / "skill.yaml").exists()
    assert (registered.bundle_path / "main.py").read_text() == "print('Formatting...')\n"
    assert (registered.bundle_path / "config.json").read_text() == '{"indent": 4}'

    # Path traversal in bundle file name must be blocked
    bad_files = {"../escape.txt": "evil"}
    bad_manifest = SkillManifest(name="formatter", version="2.0.0")
    with pytest.raises(SkillValidationError, match="escapes target skill directory"):
        registry.register(bad_manifest, bundle_files=bad_files)


def test_discovery_formats(tmp_path: Path) -> None:
    # 1. skill.yaml format
    dir_yaml = tmp_path / "s1" / "1.0.0"
    dir_yaml.mkdir(parents=True)
    (dir_yaml / "skill.yaml").write_text(
        "name: skill-one\nversion: '1.0.0'\ntags: ['nlp']\n", encoding="utf-8"
    )

    # 2. manifest.json format
    dir_json = tmp_path / "s2" / "1.5.0"
    dir_json.mkdir(parents=True)
    (dir_json / "manifest.json").write_text(
        '{"name": "skill-two", "version": "1.5.0", "tags": ["db"]}', encoding="utf-8"
    )

    # 3. SKILL.md with YAML frontmatter
    dir_md = tmp_path / "s3"
    dir_md.mkdir(parents=True)
    (dir_md / "SKILL.md").write_text(
        "---\nname: skill-three\nversion: '2.0.1'\ntags: ['security']\n---\n# Docs\n",
        encoding="utf-8",
    )

    # 4. Invalid file ignored without raising
    dir_bad = tmp_path / "bad"
    dir_bad.mkdir()
    (dir_bad / "skill.yaml").write_text("invalid: : yaml", encoding="utf-8")

    registry = SkillRegistry()
    discovered_count = registry.discover(tmp_path)
    assert discovered_count == 3

    assert registry.get_skill("skill-one").version == "1.0.0"
    assert registry.get_skill("skill-two").version == "1.5.0"
    assert registry.get_skill("skill-three").version == "2.0.1"


def test_listing_and_filtering(tmp_path: Path) -> None:
    registry = SkillRegistry(skills_dir=tmp_path)
    registry.register(SkillManifest(name="a-skill", version="1.0.0", tags=["web"]))
    registry.register(SkillManifest(name="a-skill", version="2.0.0", tags=["web", "v2"]))
    registry.register(SkillManifest(name="b-skill", version="1.0.0", tags=["db"]))

    all_skills = registry.list_skills()
    assert len(all_skills) == 3

    by_name = registry.list_skills(name="a-skill")
    assert len(by_name) == 2

    by_tag = registry.list_skills(tag="web")
    assert len(by_tag) == 2

    by_v2 = registry.list_skills(tag="v2")
    assert len(by_v2) == 1
    assert by_v2[0].version == "2.0.0"


def test_lifecycle_deprecate_and_quarantine(tmp_path: Path) -> None:
    registry = SkillRegistry(skills_dir=tmp_path)
    manifest = SkillManifest(name="legacy-tool", version="0.9.0")
    registry.register(manifest)

    # Deprecate
    deprecated = registry.deprecate("legacy-tool", "0.9.0", reason="Superseded by 1.0.0")
    assert deprecated.status == SkillStatus.DEPRECATED
    assert deprecated.metadata["deprecation_reason"] == "Superseded by 1.0.0"

    # Quarantine
    quarantined = registry.quarantine(
        "legacy-tool", "0.9.0", reason="Security vulnerability CVE-1234"
    )
    assert quarantined.status == SkillStatus.QUARANTINED
    assert "quarantined_at" in quarantined.metadata


def test_resolve_worker_skills(tmp_path: Path) -> None:
    registry = SkillRegistry(skills_dir=tmp_path)
    registry.register(SkillManifest(name="python-runtime", version="1.0.0"))
    registry.register(SkillManifest(name="docker-deploy", version="2.1.0"))
    registry.register(SkillManifest(name="malicious-skill", version="1.0.0"))
    registry.quarantine("malicious-skill", "1.0.0", reason="Malware detected")

    # Happy path resolution
    resolved = registry.resolve_worker_skills(["python-runtime", "docker-deploy@2.1.0"])
    assert len(resolved) == 2
    assert resolved[0].name == "python-runtime"
    assert resolved[1].name == "docker-deploy"

    # Unknown skill
    with pytest.raises(SkillNotFoundError, match="Skill 'non-existent' is not registered"):
        registry.resolve_worker_skills(["python-runtime", "non-existent"])

    # Quarantined skill raises SkillSecurityError
    with pytest.raises(SkillSecurityError, match="quarantined"):
        registry.resolve_worker_skills(["malicious-skill"])
