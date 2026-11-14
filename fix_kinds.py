"""Named values for a Fix event's kind and outcome, as stored in
autofix_events and pending_autofixes. The strings are the stored values:
changing one needs a data migration."""
from __future__ import annotations

from typing import Final

# What Fix ran.
KIND_AUTO_FIX: Final = "auto-fix"
KIND_REDOWNLOAD: Final = "redownload"
KIND_MARK_FAILED: Final = "mark failed"

# How it ended.
OUTCOME_PENDING: Final = "pending"
OUTCOME_COMPLETE: Final = "complete"
OUTCOME_REPEAT: Final = "repeat"
OUTCOME_TIMEOUT: Final = "timeout"
OUTCOME_FAILED: Final = "failed"
