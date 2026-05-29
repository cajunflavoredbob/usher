"""Result of a multi-step auto-fix / mark-failed orchestration.

Status:
  ok      — all steps succeeded
  partial — some work happened (e.g. blocklist + delete) but search failed,
            or vice versa. Caller should still surface what worked and
            enqueue the autofix poller if `should_poll` is True.
  failed  — first step failed; nothing happened that the user needs to know
            about beyond the message.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

Status = Literal["ok", "partial", "failed"]


@dataclass
class FixResult:
    status: Status
    message: str
    steps_done: list[str] = field(default_factory=list)
    poll_info: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def should_poll(self) -> bool:
        """True iff a fresh search was triggered (so a new file is incoming
        and the autofix poller can detect its arrival)."""
        return "search" in self.steps_done

    @classmethod
    def success(cls, message: str, steps_done: list[str],
                poll_info: Optional[dict] = None) -> "FixResult":
        return cls(status="ok", message=message, steps_done=list(steps_done),
                   poll_info=poll_info)

    @classmethod
    def partial(cls, message: str, steps_done: list[str],
                poll_info: Optional[dict] = None) -> "FixResult":
        return cls(status="partial", message=message, steps_done=list(steps_done),
                   poll_info=poll_info)

    @classmethod
    def failed(cls, message: str, steps_done: Optional[list[str]] = None) -> "FixResult":
        return cls(status="failed", message=message,
                   steps_done=list(steps_done) if steps_done else [])
