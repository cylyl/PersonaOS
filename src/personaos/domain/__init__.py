"""Domain entities — pure data classes, zero I/O.

Per ADR 0001 (modular monolith) and ADR 0002 (schema-first), the domain
package holds the in-memory representations of the entities declared in
schemas/*.yaml. Repositories (in registry/, workload/, db/) handle persistence.
"""
