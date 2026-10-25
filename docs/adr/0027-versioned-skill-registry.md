# ADR 0027: Versioned Skill Registry

**Status:** Accepted
**Date:** 2026-10-24
**Deciders:** PersonaOS Architecture Team
**Consulted:** Control Plane, Workload Execution, Reliability, Security
**Informed:** Operators, Runtime Adapters

---

## Context & Problem Statement

In PersonaOS, a worker is defined as a persistent digital employee possessing identity, authority, memory, and skills.
During Phases 1 and 2:
- Worker profiles declared skills as flat strings (`WorkerProfile.skills: list[str]`).
- Task requirements specified required skills (`Task.required_skills: list[str]`).
- The workload scheduler evaluated skill set containment (`req_skill NOT IN (SELECT skill FROM worker_skills)`).

However, skills lacked a first-class, versioned package model and registry:
1. **No Versioning or Coexistence:** Flat string names like `"python"` or `"kubernetes"` cannot distinguish breaking API or prompt changes between versions (e.g. `v1.0.0` vs `v2.0.0`). Multiple versions could not coexist or be pinned.
2. **No Metadata or Entrypoint Specification:** Skills could not declare required tools, entrypoint scripts, provided capabilities, trust tiers, or documentation.
3. **No Filesystem Discovery or Immutability:** Unlike worker profiles (which are versioned, immutable YAML snapshots under `profiles/{worker_id}/v{N}.yaml` per ADR 0008 Addendum), skills lacked an authoritative, immutable filesystem backing store.

Vikunja Roadmap item **#110 (Phase 3)** calls for building a versioned Skill Registry.

---

## Decision

We will implement a filesystem-backed, versioned `SkillRegistry` in `src/personaos/registry/skill.py` that mirrors the architectural integrity of `ProfileRegistry`:

### 1. Skill Manifest (`SkillManifest`)
Each skill is described by a strictly typed manifest:
- `name`: slug identifier (alphanumeric, dashes, underscores).
- `version`: semantic version string (e.g. `1.0.0`, `1.2.1`).
- `description`: human-readable description of skill capabilities.
- `author`: author or owning team.
- `entrypoint`: optional relative path to skill entrypoint file/script.
- `required_tools`: list of tools the skill requires (e.g. `["bash", "github_api"]`).
- `capabilities`: list of capabilities conferred by the skill.
- `tags`: list of searchable taxonomy tags.
- `trust_level`: `verified`, `internal`, `community`, `untrusted`.
- `status`: `active`, `deprecated`, `quarantined`, `disabled`.
- `digest`: SHA-256 cryptographic digest of the manifest and bundle.
- `bundle_path`: filesystem path to the skill directory.

### 2. Multi-Version Coexistence & Immutability
- Multiple versions of the same skill (e.g. `git-workflow@1.0.0` and `git-workflow@2.0.0`) coexist independently.
- Registered versions are immutable on disk. Once written, mutating an existing version is rejected with `SkillVersionExistsError`.
- Directory layout:
  `{skills_dir}/{skill_name}/{version}/skill.yaml` (or `SKILL.md` frontmatter or `manifest.json`).

### 3. Version Resolution
- `get_skill(name, version=None)`:
  - If `version` is specified (e.g. `"1.0.0"`), retrieves that exact version.
  - If `version` is omitted or `"latest"`, parses semantic versions across all active versions of that skill and returns the highest semver.
  - Pinned queries support syntax like `name@version`.

### 4. Discovery & Interoperability
- `discover(dir_path)`: Scans directories recursively for skill manifests:
  - Supports standard `skill.yaml` / `skill.json`.
  - Supports markdown-first agent skill formats (`SKILL.md` with YAML frontmatter, as used in modern AI ecosystems and SkillSpector).
- Non-blocking and robust: malformed files in scanned directories are logged and skipped rather than halting the entire system.

### 5. Worker Profile Integration
- `resolve_worker_skills(skills)`: Maps worker skill references to validated manifests, ensuring workers only operate with registered, active skills.

---

## Consequences

### Positive
- Strict version pinning and coexistence of multiple skill versions without race conditions.
- Zero database migrations required; files are stored immutably on disk with in-memory caching.
- Interoperable with modern agent formats (`SKILL.md` frontmatter and `skill.yaml`).
- Clean prerequisite foundation for Skill Validation and Security Scanning (Vikunja #111).

### Negative / Trade-offs
- File system access is required for persistent disk storage (in-memory mode supported for ephemeral testing).
