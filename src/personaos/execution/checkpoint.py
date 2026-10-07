"""Checkpoint — durable state snapshot per task.

v0.1 scope: write/read checkpoint for resumable tasks. The runner calls
checkpoint.load(task.checkpoint_ref) BEFORE adapter.execute() and
checkpoint.write(task, result.state) AFTER.

Storage choice (v0.1): a `checkpoints` table in Postgres (jsonb state).
Object storage (S3/MinIO) is a v0.2 option when state blobs get large.

TODO(v0.1): implement
  - write(task_id, state) -> checkpoint_ref
  - load(checkpoint_ref) -> State
  - delete(checkpoint_ref)

The checkpoint_ref is opaque to the caller (UUID).
"""
