"""FastAPI control plane.

The API is the control plane. It does NOT execute tasks — that's the worker's
job. Endpoints expose the registry, workload, and event log for operators
and external systems.
"""
