"""Registry services — own the lifecycle of workers, profiles, skills, and tasks.

Per docs/specs/worker-profile.md + ADR 0008 Addendum and ADR 0027:
The registry is responsible for atomic activation of profile versions
and versioned skill package lifecycle.

Modules:
  - profile: ProfileRegistry (versioned worker profiles + immutable YAML)
  - skill: SkillRegistry (versioned skill packages + multi-version coexistence)
"""

from personaos.registry.skill import (
    SkillError,
    SkillManifest,
    SkillNotFoundError,
    SkillRegistry,
    SkillSecurityError,
    SkillStatus,
    SkillTrustLevel,
    SkillValidationError,
    SkillVersionExistsError,
)

__all__ = [
    "SkillError",
    "SkillManifest",
    "SkillNotFoundError",
    "SkillRegistry",
    "SkillSecurityError",
    "SkillStatus",
    "SkillTrustLevel",
    "SkillValidationError",
    "SkillVersionExistsError",
]
