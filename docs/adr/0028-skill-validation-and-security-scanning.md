# ADR 0028: Skill Validation and Security Scanning

**Status:** Accepted
**Date:** 2026-10-24
**Deciders:** PersonaOS Architecture Team
**Consulted:** Security, Runtime, Workload Execution, Control Plane
**Informed:** Operators, Runtime Adapters

---

## Context & Problem Statement

AI agent skills represent executable packages and prompts that extend worker capabilities. Because skills can contain arbitrary code, instructions, and tool invocations, allowing untrusted skills into a persistent workforce introduces substantial attack vectors:
1. **Supply Chain & Malicious Code Injection:** Skills may contain malicious Python payloads (e.g., using `eval()`, `exec()`, `ctypes`, or hidden shell commands) to compromise the host node or worker environment.
2. **Filesystem Traversal & Symlink Attacks:** Malformed skill bundles could deploy symlinks escaping the skill sandbox to read sensitive host files (e.g. `/root/.ssh`, credentials, or other worker profiles).
3. **Over-Permissioning & Tool Abuse:** Skills may request dangerous tools or bypass worker execution policies.
4. **Third-Party Scanner Resilience:** While NVIDIA SkillSpector provides static scanning capabilities, external dependencies may not always be present in all deployment environments. The security architecture must fail closed, defend against attacks natively, and gracefully leverage SkillSpector when installed.

Vikunja Roadmap item **#111 (Phase 3)** calls for skill validation and security scanning.

---

## Decision

We will implement a defense-in-depth skill validation and security scanning engine in `src/personaos/security/skill_validator.py`:

### 1. Multi-Stage Scanning Architecture

The `SkillValidator` runs four consecutive, deterministic stages:

1. **Filesystem Sandbox & Path Traversal Check:**
   - Recursively inspects every path in the skill bundle.
   - Rejects any symlink whose resolved target escapes the bundle root.
   - Rejects any file path containing directory traversal tokens (`..`), null bytes, or dangerous control sequences.
   - Any path traversal immediately halts with `ScanVerdict.REJECT` and a `CRITICAL` finding.

2. **Manifest & Permission Policy Inspection:**
   - Enforces manifest schema validity (name slug, semver, trust level).
   - Validates declared entrypoints exist within the bundle and are not executable binaries or dangerous extensions.
   - Verifies declared required tools against organizational forbidden tool lists (e.g., raw sockets, destructive file commands).

3. **Native Static AST Analysis:**
   - Scans all Python source files in the bundle using `ast.parse`.
   - Detects dangerous execution constructs: `exec()`, `eval()`, `__import__()`, `compile()`.
   - Detects unvetted process execution (`subprocess`, `os.system`, `os.popen`, `pty`, `commands`).
   - Detects low-level memory tampering (`ctypes`, `mmap`).
   - Detects hardcoded secret tokens and credential paths.
   - Emits structured `SecurityFinding` records detailing file, line number, rule, and severity.

4. **NVIDIA SkillSpector Adapter:**
   - If the `skillspector` CLI or library is present, executes `skillspector scan "$TARGET" --no-llm --format json` in a sandboxed subprocess.
   - Parses the JSON report and maps SkillSpector findings to `SecurityFinding` records.
   - If `skillspector` is absent or encounters runtime errors, logs diagnostic metadata while maintaining full native static enforcement.

### 2. Admission Verdicts & Fail-Closed Enforcement

- **`APPROVE`:** Zero CRITICAL or HIGH findings, risk score < 20.0. The skill is admitted for registration and execution.
- **`CAUTION`:** Minor warnings or low-severity findings (risk score 20.0 - 59.9). Admitted with warnings attached to manifest metadata; can be flagged for human review.
- **`REJECT`:** Any CRITICAL or HIGH finding, or risk score >= 60.0. Refused admission.
- **Registry Integration:** Attempting to register or activate a rejected skill raises `SkillSecurityError`.

---

## Consequences

### Positive
- Strict fail-closed security preventing untrusted or compromised skills from reaching worker execution kernels.
- Native static analysis protects the system even when external scanning tools are not installed.
- Full compatibility with NVIDIA SkillSpector when installed.
- Symlink and path traversal defense guarantees worker profile and host boundary integrity.

### Negative / Trade-offs
- Static analysis cannot detect 100% of dynamic runtime obfuscation (mitigated at runtime by the existing AI Action Firewall and `SecurityPolicyEngine`).
