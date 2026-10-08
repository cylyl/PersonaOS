"""Result — adapter return type.

Per ADR 0010: the adapter returns a Result indicating success or failure.
The kernel handles the Result and persists the appropriate state.

Step 5 (v0.1): only 'completed' and 'failed' statuses. The 'needs_input'
status from the original base.py TODO is deferred — it's not in the
Step 5 lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Result:
    """Adapter return value.

    status: "completed" | "failed"
    output: result payload (None on failure)
    error: failure reason (None on success)
    new_checkpoint: state for resumption (None if no resumption needed)
    """

    status: str
    output: Optional[dict] = None
    error: Optional[str] = None
    new_checkpoint: Optional[dict] = None
