"""Event entity — append-only event log entry.

Per ADR 0006: state tables remain the source of truth; events are
append-only. The type column is constrained to the 17 v0.1 event types.

TODO(v0.1): define Event dataclass + EventTaxonomy enum (17 values).
Source of truth: schemas/event.yaml.
"""
