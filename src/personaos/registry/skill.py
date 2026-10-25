"""Versioned Skill Registry — manages the lifecycle of AI agent skills.

Per ADR 0027 and docs/specs/versioned-skill-registry.md:
- Supports immutable, versioned skill manifests.
- Coexistence of multiple versions of the same skill.
- Filesystem-backed storage with discovery of skill.yaml, manifest.json, and SKILL.md.
- Integration with worker profiles and workload scheduling.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)

# Default skills directory; can be overridden via env var or constructor arg.
DEFAULT_SKILLS_DIR = Path(os.environ.get("PERSONAOS_SKILLS_DIR", "skills"))

NAME_REGEX = re.compile(r"^[a-zA-Z0-9_\-]+$")


class SkillStatus(StrEnum):
    """Lifecycle status of a skill version."""

    ACTIVE = "active"
    DEPRECATED = "deprecated"
    QUARANTINED = "quarantined"
    DISABLED = "disabled"


class SkillTrustLevel(StrEnum):
    """Trust classification of a skill."""

    VERIFIED = "verified"
    INTERNAL = "internal"
    COMMUNITY = "community"
    UNTRUSTED = "untrusted"


class SkillError(Exception):
    """Base exception for skill registry operations."""


class SkillNotFoundError(SkillError):
    """Raised when a requested skill name or version is not found."""


class SkillVersionExistsError(SkillError):
    """Raised when attempting to overwrite an existing immutable skill version."""


class SkillValidationError(SkillError):
    """Raised when a skill manifest fails validation."""


class SkillSecurityError(SkillError):
    """Raised when attempting to use or resolve an unsafe or quarantined skill."""


def parse_semver(v_str: str) -> Version:
    """Parse a semantic version string, normalizing common prefixes like 'v'."""
    clean_v = v_str.strip().lstrip("v")
    try:
        return Version(clean_v)
    except InvalidVersion as exc:
        raise SkillValidationError(f"Invalid semantic version: {v_str}") from exc


@dataclass
class SkillManifest:
    """Versioned metadata manifest for an agent skill."""

    name: str
    version: str
    description: str = ""
    author: str = ""
    entrypoint: str | None = None
    required_tools: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    trust_level: SkillTrustLevel = SkillTrustLevel.INTERNAL
    status: SkillStatus = SkillStatus.ACTIVE
    metadata: dict[str, Any] = field(default_factory=dict)
    digest: str = ""
    bundle_path: Path | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        self.name = self.name.strip()
        if not self.name or not NAME_REGEX.match(self.name):
            raise SkillValidationError(
                f"Skill name '{self.name}' is invalid. "
                "Must be non-empty and match ^[a-zA-Z0-9_\\-]+$"
            )
        # Validate version
        self.version = self.version.strip()
        _ = parse_semver(self.version)

        if isinstance(self.trust_level, str):
            self.trust_level = SkillTrustLevel(self.trust_level)
        if isinstance(self.status, str):
            self.status = SkillStatus(self.status)

        # Validate entrypoint safety
        if self.entrypoint is not None:
            ep = self.entrypoint.strip()
            if not ep:
                self.entrypoint = None
            else:
                ep_path = Path(ep)
                if ep_path.is_absolute() or ".." in ep_path.parts:
                    raise SkillValidationError(
                        f"Entrypoint '{ep}' cannot be absolute or contain parent traversals ('..')"
                    )
                self.entrypoint = ep

    @property
    def identifier(self) -> str:
        """Fully qualified identifier: {name}@{version}."""
        return f"{self.name}@{self.version}"

    @property
    def parsed_version(self) -> Version:
        """Parsed packaging Version for sorting."""
        return parse_semver(self.version)

    def calculate_digest(self) -> str:
        """Compute SHA-256 digest of normalized manifest content."""
        payload = {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "entrypoint": self.entrypoint,
            "required_tools": sorted(self.required_tools),
            "capabilities": sorted(self.capabilities),
            "tags": sorted(self.tags),
            "trust_level": self.trust_level.value,
        }
        encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Convert manifest to serializable dictionary."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "entrypoint": self.entrypoint,
            "required_tools": list(self.required_tools),
            "capabilities": list(self.capabilities),
            "tags": list(self.tags),
            "trust_level": self.trust_level.value,
            "status": self.status.value,
            "metadata": dict(self.metadata),
            "digest": self.digest or self.calculate_digest(),
            "bundle_path": str(self.bundle_path) if self.bundle_path else None,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], bundle_path: Path | None = None) -> SkillManifest:
        """Construct manifest from dictionary representation."""
        created_at_raw = data.get("created_at")
        if isinstance(created_at_raw, str):
            created_at = datetime.fromisoformat(created_at_raw)
        elif isinstance(created_at_raw, datetime):
            created_at = created_at_raw
        else:
            created_at = datetime.now(UTC)

        b_path = bundle_path or (Path(data["bundle_path"]) if data.get("bundle_path") else None)

        return cls(
            name=data["name"],
            version=str(data.get("version", "1.0.0")),
            description=data.get("description", ""),
            author=data.get("author", ""),
            entrypoint=data.get("entrypoint"),
            required_tools=list(data.get("required_tools") or []),
            capabilities=list(data.get("capabilities") or []),
            tags=list(data.get("tags") or []),
            trust_level=SkillTrustLevel(data.get("trust_level", SkillTrustLevel.INTERNAL.value)),
            status=SkillStatus(data.get("status", SkillStatus.ACTIVE.value)),
            metadata=dict(data.get("metadata") or {}),
            digest=data.get("digest", ""),
            bundle_path=b_path,
            created_at=created_at,
        )

    def to_yaml(self) -> str:
        """Serialize manifest to YAML string."""
        return str(yaml.safe_dump(self.to_dict(), sort_keys=False))

    @classmethod
    def from_yaml(cls, content: str, bundle_path: Path | None = None) -> SkillManifest:
        """Parse manifest from YAML string."""
        data = yaml.safe_load(content)
        if not isinstance(data, dict):
            raise SkillValidationError("Manifest YAML must deserialize to a dictionary")
        return cls.from_dict(data, bundle_path=bundle_path)


class SkillRegistry:
    """Thread-safe versioned registry for managing agent skills."""

    def __init__(
        self,
        skills_dir: Path | str | None = None,
        auto_discover: bool = False,
    ) -> None:
        self.skills_dir: Path | None = Path(skills_dir) if skills_dir is not None else None
        # In-memory storage: name -> {version_str: SkillManifest}
        self._skills: dict[str, dict[str, SkillManifest]] = {}
        self._lock = threading.RLock()

        if self.skills_dir is not None and auto_discover and self.skills_dir.exists():
            self.discover(self.skills_dir)

    def register(
        self,
        manifest: SkillManifest,
        bundle_files: dict[str, str | bytes] | None = None,
        overwrite: bool = False,
    ) -> SkillManifest:
        """Register a new skill version.

        If `skills_dir` is configured, writes `skill.yaml` and optional bundle files to disk
        under `{skills_dir}/{name}/{version}/`.
        """
        with self._lock:
            name_versions = self._skills.setdefault(manifest.name, {})
            if manifest.version in name_versions and not overwrite:
                raise SkillVersionExistsError(
                    f"Skill '{manifest.name}' version '{manifest.version}' is already registered."
                )

            # Assign digest if missing
            if not manifest.digest:
                manifest.digest = manifest.calculate_digest()

            # Persist to disk if directory configured
            if self.skills_dir is not None:
                skill_dir = self.skills_dir / manifest.name / manifest.version
                skill_dir.mkdir(parents=True, exist_ok=True)
                manifest.bundle_path = skill_dir

                manifest_file = skill_dir / "skill.yaml"
                manifest_file.write_text(manifest.to_yaml(), encoding="utf-8")

                if bundle_files:
                    for filename, file_content in bundle_files.items():
                        target_path = (skill_dir / filename).resolve()
                        # Path traversal guard
                        if not str(target_path).startswith(str(skill_dir.resolve())):
                            raise SkillValidationError(
                                f"Invalid bundle path '{filename}' escapes target skill directory"
                            )
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        if isinstance(file_content, bytes):
                            target_path.write_bytes(file_content)
                        else:
                            target_path.write_text(file_content, encoding="utf-8")

            name_versions[manifest.version] = manifest
            logger.info("Registered skill %s", manifest.identifier)
            return manifest

    def get_skill(self, name: str, version: str | None = None) -> SkillManifest:
        """Retrieve a registered skill by name and version.

        If version is None or 'latest', resolves the highest semantic version among active skills.
        """
        # Support name@version format in name argument
        if "@" in name and version is None:
            name, version = name.split("@", 1)

        with self._lock:
            versions = self._skills.get(name)
            if not versions:
                raise SkillNotFoundError(f"Skill '{name}' is not registered.")

            if version is not None and version != "latest":
                norm_ver = version.strip().lstrip("v")
                for v_str, manifest in versions.items():
                    if v_str == version or v_str.lstrip("v") == norm_ver:
                        return manifest
                raise SkillNotFoundError(f"Skill '{name}' version '{version}' not found.")

            # Resolve latest semantic version
            sorted_manifests = sorted(
                versions.values(),
                key=lambda m: m.parsed_version,
                reverse=True,
            )
            if not sorted_manifests:
                raise SkillNotFoundError(f"Skill '{name}' has no available versions.")

            return sorted_manifests[0]

    def list_skills(
        self,
        name: str | None = None,
        tag: str | None = None,
        status: SkillStatus | None = None,
        include_deprecated: bool = True,
    ) -> list[SkillManifest]:
        """List registered skills matching filter criteria."""
        results: list[SkillManifest] = []
        with self._lock:
            for s_name, versions in self._skills.items():
                if name is not None and s_name != name:
                    continue
                for manifest in versions.values():
                    if status is not None and manifest.status != status:
                        continue
                    if not include_deprecated and manifest.status == SkillStatus.DEPRECATED:
                        continue
                    if tag is not None and tag not in manifest.tags:
                        continue
                    results.append(manifest)

        # Sort by name, then version descending
        results.sort(key=lambda m: (m.name, m.parsed_version), reverse=True)
        return results

    def deprecate(self, name: str, version: str, reason: str = "") -> SkillManifest:
        """Mark a skill version as deprecated."""
        with self._lock:
            manifest = self.get_skill(name, version)
            manifest.status = SkillStatus.DEPRECATED
            if reason:
                manifest.metadata["deprecation_reason"] = reason
            if self.skills_dir is not None and manifest.bundle_path:
                manifest_file = manifest.bundle_path / "skill.yaml"
                if manifest_file.exists():
                    manifest_file.write_text(manifest.to_yaml(), encoding="utf-8")
            return manifest

    def quarantine(self, name: str, version: str, reason: str) -> SkillManifest:
        """Quarantine a skill version due to security or operational incident."""
        with self._lock:
            manifest = self.get_skill(name, version)
            manifest.status = SkillStatus.QUARANTINED
            manifest.metadata["quarantine_reason"] = reason
            manifest.metadata["quarantined_at"] = datetime.now(UTC).isoformat()
            if self.skills_dir is not None and manifest.bundle_path:
                manifest_file = manifest.bundle_path / "skill.yaml"
                if manifest_file.exists():
                    manifest_file.write_text(manifest.to_yaml(), encoding="utf-8")
            logger.warning("Quarantined skill %s: %s", manifest.identifier, reason)
            return manifest

    def discover(self, dir_path: Path | str) -> int:
        """Recursively scan a directory tree for skill manifests.

        Supports:
        - skill.yaml / skill.yml
        - manifest.json
        - SKILL.md with YAML frontmatter
        Returns count of successfully registered skills.
        """
        scan_root = Path(dir_path)
        if not scan_root.is_dir():
            return 0

        count = 0
        for root_str, _, files in os.walk(scan_root):
            root = Path(root_str)
            manifest: SkillManifest | None = None

            if "skill.yaml" in files:
                manifest = self._parse_file(root / "skill.yaml", root)
            elif "skill.yml" in files:
                manifest = self._parse_file(root / "skill.yml", root)
            elif "manifest.json" in files:
                manifest = self._parse_file(root / "manifest.json", root)
            elif "SKILL.md" in files:
                manifest = self._parse_skill_md(root / "SKILL.md", root)

            if manifest is not None:
                try:
                    self.register(manifest, overwrite=True)
                    count += 1
                except Exception as exc:
                    logger.warning("Failed to register discovered skill at %s: %s", root, exc)

        return count

    def resolve_worker_skills(self, worker_skills: list[str]) -> list[SkillManifest]:
        """Resolve a worker's declared skill strings (e.g. 'git@1.0.0' or 'python') to manifests.

        Raises:
            SkillNotFoundError: If any skill or version is not found in the registry.
            SkillSecurityError: If any resolved skill is quarantined or disabled.
        """
        resolved: list[SkillManifest] = []
        for s_spec in worker_skills:
            manifest = self.get_skill(s_spec)
            if manifest.status in (SkillStatus.QUARANTINED, SkillStatus.DISABLED):
                raise SkillSecurityError(
                    f"Worker references skill '{manifest.identifier}' "
                    f"which is {manifest.status.value}"
                )
            resolved.append(manifest)
        return resolved

    def _parse_file(self, file_path: Path, bundle_dir: Path) -> SkillManifest | None:
        try:
            content = file_path.read_text(encoding="utf-8")
            if file_path.suffix.lower() == ".json":
                data = json.loads(content)
                return SkillManifest.from_dict(data, bundle_path=bundle_dir)
            return SkillManifest.from_yaml(content, bundle_path=bundle_dir)
        except Exception as exc:
            logger.warning("Could not parse skill manifest at %s: %s", file_path, exc)
            return None

    def _parse_skill_md(self, md_path: Path, bundle_dir: Path) -> SkillManifest | None:
        """Parse SKILL.md containing YAML frontmatter bounded by '---'."""
        try:
            content = md_path.read_text(encoding="utf-8")
            if not content.startswith("---"):
                # No frontmatter
                return None
            parts = content.split("---", 2)
            if len(parts) < 3:
                return None
            frontmatter = parts[1].strip()
            data = yaml.safe_load(frontmatter)
            if not isinstance(data, dict) or "name" not in data:
                return None
            if "version" not in data:
                data["version"] = "1.0.0"
            return SkillManifest.from_dict(data, bundle_path=bundle_dir)
        except Exception as exc:
            logger.warning("Could not parse SKILL.md frontmatter at %s: %s", md_path, exc)
            return None
