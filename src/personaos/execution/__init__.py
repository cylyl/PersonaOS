"""Execution — worker runtime.

Per ADR 0005, runtime adapters (execution/adapters/) are thin shims. The
runner (execution/runner.py) owns the kernel main loop:

    pull task → load worker → evaluate policy → execute via adapter
    → write checkpoint → emit event
"""
