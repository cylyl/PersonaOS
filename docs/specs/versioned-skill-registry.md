# Specification: Versioned Skill Registry

**Reference:** ADR 0027, Vikunja #110 (Phase 3)
**Module:** `personaos.registry.skill`

---

## 1. Domain Entities & Schemas

### 1.1 Enums

```python
class SkillStatus(str, Enum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    QUARANTINED = "quarantined"
    DISABLED = "disabled"

class SkillTrustLevel(str, Enum):
    VERIFIED = "verified"
    INTERNAL = "internal"
    COMMUNITY = "community"
    UNTRUSTED = "untrusted"
```

### 1.2 `SkillManifest` Dataclass

```python
@dataclass
class SkillManifest:
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
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
```

Validation invariants:
- `name`: Matches `^[a-zA-Z0-9_\-]+$` and length between 1 and 128 characters.
- `version`: Valid SemVer (`major.minor.patch` with optional prerelease).
- `entrypoint`: Must not contain path traversal (`..` or absolute paths outside the bundle).

---

## 2. Directory Structure & Layout

Default root: `skills/` (configurable via `PERSONAOS_SKILLS_DIR`).

```
skills/
  ├── github-pr-review/
  │   ├── 1.0.0/
  │   │   ├── skill.yaml
  │   │   └── review.py
  │   └── 1.1.0/
  │       ├── skill.yaml
  │       └── review.py
  └── code-linter/
      └── 2.0.0/
          ├── SKILL.md
          └── linter.sh
```

---

## 3. SkillRegistry Interface

```python
class SkillRegistry:
    def __init__(self, skills_dir: Path | str | None = None, auto_discover: bool = False):
        ...

    def register(
        self,
        manifest: SkillManifest,
        bundle_files: dict[str, str | bytes] | None = None,
        overwrite: bool = False,
    ) -> SkillManifest:
        """Register a new skill version. Writes manifest and files to disk if skills_dir is configured."""

    def get_skill(self, name: str, version: str | None = None) -> SkillManifest:
        """Retrieve skill manifest. Resolves highest semver if version is None or 'latest'."""

    def list_skills(
        self,
        name: str | None = None,
        tag: str | None = None,
        status: SkillStatus | None = None,
        include_deprecated: bool = False,
    ) -> list[SkillManifest]:
        """List and filter skills."""

    def deprecate(self, name: str, version: str, reason: str = "") -> SkillManifest:
        """Mark a skill version as deprecated."""

    def quarantine(self, name: str, version: str, reason: str) -> SkillManifest:
        """Quarantine a skill version due to security or operational incident."""

    def discover(self, dir_path: Path | str) -> int:
        """Recursively discover skill manifests in directory. Returns count of discovered skills."""

    def resolve_worker_skills(self, worker_skills: list[str]) -> list[SkillManifest]:
        """Map worker skill references (e.g. 'code-review@1.0.0' or 'linter') to registered manifests."""
```

---

## 4. Error Handling

- `SkillNotFoundError`: Raised when requested skill name or version is not registered.
- `SkillVersionExistsError`: Raised when attempting to register an existing version without `overwrite=True`.
- `SkillValidationError`: Raised when manifest fails schema or path traversal validation.
- `SkillSecurityError`: Raised when attempting to use or activate a quarantined or disabled skill.
