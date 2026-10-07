"""OpenClaw adapter — thin shim around the OpenClaw runtime.

Per ADR 0005: only translates between OpenClaw's API and PersonaOS's domain
types. No domain logic, no policy logic, no scheduler logic.

TODO(v0.1): implement the OpenClaw adapter:
  - Call OpenClaw's HTTP /run endpoint with task payload + checkpoint
  - Parse the response into Result
  - Configure via OPENCLAW_API_URL in .env

If OPENCLAW_API_URL is unset, raise a clear config error on first use.
The adapter is the only place that knows OpenClaw's wire format.
"""
