"""Worker routes — CRUD + quarantine.

TODO(v0.1):
  GET    /workers
  POST   /workers                      # register a new worker from a profile
  GET    /workers/{id}
  PATCH  /workers/{id}                 # update profile (bumps version)
  POST   /workers/{id}/quarantine      # security/quarantine.quarantine()
  POST   /workers/{id}/unquarantine
"""
