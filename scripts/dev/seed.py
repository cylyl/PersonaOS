"""Seed example workers into the dev database.

Run via: make seed
Loads:
  examples/devops-worker.yaml
  examples/developer-worker.yaml
  examples/qa-worker.yaml

TODO(v0.1): for each example YAML, validate against schemas/worker.yaml,
insert into the workers table (idempotent — skip if id already exists),
and print a summary.

This is a dev convenience. In production, workers register themselves via
api/routes/nodes.py and api/routes/workers.py.
"""
