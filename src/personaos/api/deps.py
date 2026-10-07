"""Dependency injection helpers for FastAPI.

TODO(v0.1): implement:
  - get_session()            — async DB session
  - get_current_worker()     — header-based identity for v0.1 (no full OAuth yet)
  - get_event_store()        — events append/list helper

In v0.1, "current worker" is identified by `X-Worker-Id` header (set by
the worker process when calling the API). Real auth is post-v0.1.
"""
