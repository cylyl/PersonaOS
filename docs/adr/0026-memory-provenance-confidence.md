# ADR 0026: Memory Provenance, Confidence Scoring, and Decay Policies

**Status:** Accepted (Vikunja #107 Phase 2 Milestone)
**Date:** 2026-10-24
**Deciders:** PersonaOS Architecture Team
**Consulted:** Context Assembler, Memory Service, Security Engine
**Informed:** Operators, Platform Engineers

---

## Context & Problem Statement

PersonaOS operates on the core thesis: **“Compute is replaceable; the worker is persistent.”**
In ADR 0007 and ADR 0025, PersonaOS defined and implemented the foundational `MemoryAdapter` protocol and five memory scopes (`persona`, `project`, `worker`, `task`, `knowledge`).

However, while `MemoryRef` accommodated unstructured `metadata: dict[str, Any]`, memory entries lacked first-class, typed domain representations for:
1. **Provenance:** Where a memory originated (source task ID, execution attempt, human operator, external document, or inference).
2. **Confidence:** A normalized, bounded numeric score `[0.0, 1.0]` representing reliability and certainty.
3. **Decay Policies:** Deterministic models governing how confidence diminishes over time (e.g. exponential half-life or linear decay), preventing outdated observations from permanently polluting worker context.
4. **Confidence-Threshold Filtering:** The ability to filter out low-confidence or decayed memories during retrieval and execution-context assembly.

Without enforceable provenance and confidence, workers cannot assess whether recalled memories are fresh empirical observations, unverified inferences, or stale historical artifacts.

---

## Decision

We will extend the PersonaOS memory subsystem with typed provenance, validated confidence, deterministic decay, and threshold-based retrieval:

### 1. Typed Provenance Model (`MemoryProvenance`)
In `src/personaos/memory/scopes.py`:
- `source_type: str`: Explicit category (`"task"`, `"execution"`, `"operator"`, `"document"`, `"system"`, `"unknown"`). Never fabricated; if unavailable, represented explicitly as `"unknown"`.
- `source_id: str | None`: Identifier of the source entity (e.g., task UUID, event ID, document URL).
- `activity: str | None`: Action that generated the memory (e.g., `"task_completion"`, `"manual_override"`, `"inference"`).
- `created_by: str | None`: Identifier of the worker or operator who recorded the memory.

### 2. Validated Confidence Range
- `confidence: float`: Strictly enforced in the closed interval `[0.0, 1.0]`. Values outside this range raise `ValueError` upon instantiation. Defaults to `1.0` if omitted.

### 3. Deterministic Decay Policies (`DecayPolicy`)
In `src/personaos/memory/scopes.py`:
- `DecayStrategy`: Enum with values `NONE = "none"`, `EXPONENTIAL = "exponential"`, `LINEAR = "linear"`.
- `DecayPolicy`:
  - `strategy: DecayStrategy`: Chosen decay curve.
  - `half_life_days: float | None`: For exponential decay: $C(t) = C_0 \cdot 2^{-\Delta t / \text{half\_life}}$.
  - `linear_rate_per_day: float | None`: For linear decay: $C(t) = \max(C_{min}, C_0 - \text{rate} \cdot \Delta t)$.
  - `min_confidence: float`: Floor below which confidence does not drop (default: `0.0`).
  - `calculate_effective_confidence(initial: float, created_at: datetime, at_time: datetime | None = None) -> float`: Pure, deterministic calculation.

### 4. Adapter and Service Integration
- `MemoryRef` exposes `effective_confidence(at_time: datetime | None = None) -> float`.
- `MemoryAdapter` methods (`recall`, `list`) accept `min_confidence: float | None = None`.
- `MemoryService` and `ExecutionContextAssembler` filter recalled memories whose `effective_confidence` falls below the configured threshold (default configurable via `settings.memory_min_confidence`).
- `Mem0Adapter` and `InMemoryMemoryAdapter` serialize and deserialize provenance, confidence, and decay parameters through metadata without data loss, gracefully falling back to defaults for legacy records.

### 5. Preserved Security Invariants
- Recalled memory content remains strictly untrusted external context. Even high-confidence memories cannot bypass `PolicyEngine` rules, modify tool execution constraints, or alter task state.
- Cross-worker isolation boundaries for `Scope.WORKER` remain strictly enforced.

---

## Consequences

### Positive
- Enables workers to distinguish authoritative, fresh task outcomes from decayed or low-confidence inferences.
- Full backward compatibility with existing memory adapters and legacy records lacking provenance metadata.
- Fully deterministic and testable without external network dependencies.

### Negative / Trade-offs
- Slight additional serialization footprint in memory metadata.
