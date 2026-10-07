"""Persona Registry — worker and node registration.

v0.1 scope:
  - register / update / retire workers
  - register / heartbeat / drain / lose nodes
  - assign workers to nodes (transient binding)

Per ADR 0003: Worker and Node are separate entities. The lease system
(in workload/) binds a worker to a node for the duration of a single task.
"""
