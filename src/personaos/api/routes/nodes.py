"""Node routes — register, heartbeat, drain.

TODO(v0.1):
  GET    /nodes
  POST   /nodes                        # worker process registers its node
  POST   /nodes/{id}/heartbeat         # worker reports liveness
  POST   /nodes/{id}/drain             # graceful drain (no new assignments)
"""
