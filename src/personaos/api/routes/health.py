"""Health route — DB connectivity + migration version.

TODO(v0.1): implement GET /health that pings the DB and returns
{
  "status": "ok" | "degraded" | "down",
  "db": "ok" | "down",
  "last_migration": "<revision>",
  "version": "<personaos version>"
}
"""
