"""Registry services — own the lifecycle of workers, profiles, and tasks.

Per docs/specs/worker-profile.md + ADR 0008 Addendum, the registry is the
ONLY writer to the profiles/ directory and is responsible for atomic
activation of profile versions.

Modules:
  - profile: ProfileRegistry (versioned worker profiles + immutable YAML)
"""
