"""Security — surrounds every action in the runner main loop (per ADR 0004).

v0.1 ships the 8-item MVP list:
  policy, permissions, tool_policy, approval, secrets, quarantine.

DLP, AI gateway, prompt-injection detection, shadow-AI detection, SIEM,
anomaly detection, AI red teaming, and compliance controls are explicitly
deferred — each is a startup-grade effort, lives in the README vision only.
"""
