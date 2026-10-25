# Specification: Skill Validation and Security Scanning

**Reference:** ADR 0028, Vikunja #111 (Phase 3)
**Module:** `personaos.security.skill_validator`

---

## 1. Domain Entities & Schemas

### 1.1 Enums

```python
class ScanVerdict(str, Enum):
    APPROVE = "approve"
    CAUTION = "caution"
    REJECT = "reject"

class FindingSeverity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"
```

### 1.2 Data Structures

```python
@dataclass(frozen=True)
class SecurityFinding:
    rule_id: str
    severity: FindingSeverity
    message: str
    file_path: str | None = None
    line_number: int | None = None
    source: str = "native"

@dataclass
class SkillScanReport:
    skill_name: str
    version: str
    verdict: ScanVerdict
    risk_score: float
    findings: list[SecurityFinding] = field(default_factory=list)
    scanner_metadata: dict[str, Any] = field(default_factory=dict)
    scanned_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_approved(self) -> bool:
        return self.verdict == ScanVerdict.APPROVE

    @property
    def has_critical(self) -> bool:
        return any(f.severity == FindingSeverity.CRITICAL for f in self.findings)
```

---

## 2. Validator Interface

```python
class SkillValidator:
    def __init__(
        self,
        forbidden_tools: set[str] | None = None,
        max_allowed_risk: float = 60.0,
        skillspector_bin: str | None = None,
    ) -> None:
        ...

    def scan_bundle(
        self,
        bundle_path: Path,
        manifest: SkillManifest | None = None,
    ) -> SkillScanReport:
        """Execute full security inspection of the skill bundle directory and manifest."""

    def validate_manifest(self, manifest: SkillManifest) -> list[SecurityFinding]:
        """Perform static manifest policy validation."""

    def scan_python_ast(self, py_file: Path, base_dir: Path) -> list[SecurityFinding]:
        """Scan a Python file for dangerous AST nodes."""
```

---

## 3. Security Rules & Rule IDs

| Rule ID | Severity | Description |
|---|---|---|
| `SEC-PATH-TRAVERSAL` | CRITICAL | Path contains `..` or escapes bundle root |
| `SEC-SYMLINK-ESCAPE` | CRITICAL | Symlink points outside the bundle root |
| `SEC-DYNAMIC-EXEC` | HIGH | Call to `eval()`, `exec()`, or `compile()` |
| `SEC-DANGEROUS-IMPORT` | HIGH | Import of `ctypes`, `pty`, or unauthorized `subprocess` |
| `SEC-CREDENTIAL-LEAK` | HIGH | Hardcoded secret pattern or sensitive system path |
| `SEC-FORBIDDEN-TOOL` | HIGH | Declared tool violates organizational forbidden list |
| `SEC-ENTRYPOINT-MISSING`| MEDIUM | Declared entrypoint file does not exist in bundle |
| `SEC-OVERPERM-TOOL` | LOW | Declared tool requires elevated permissions |
