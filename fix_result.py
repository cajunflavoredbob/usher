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


@dataclass
class QueueProgress:
    """Aggregate download progress for the queue records of one movie or
    series, from the arr /queue/details endpoints. Backs the morphing
    request/auto-fix cards and the SABnzbd priority boost."""
    percent: int              # 0-100 across all records (size-weighted)
    timeleft: str             # arr's "00:14:32" string from the largest record, "" unknown
    download_ids: list        # download-client ids (SABnzbd nzo ids) seen
    count: int                # queue records aggregated


def parse_queue_records(records: list) -> "QueueProgress | None":
    """Fold arr queue/details records into one QueueProgress; None when the
    queue holds nothing for this media (finished, or not grabbed yet)."""
    total = left = 0.0
    download_ids: list = []
    timeleft = ""
    biggest = -1.0
    count = 0
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        count += 1
        try:
            size = float(rec.get("size") or 0)
            sizeleft = float(rec.get("sizeleft") or 0)
        except (TypeError, ValueError):
            size = sizeleft = 0.0
        total += size
        left += sizeleft
        if size > biggest:
            biggest = size
            timeleft = str(rec.get("timeleft") or "")
        did = rec.get("downloadId")
        if isinstance(did, str) and did:
            download_ids.append(did)
    if count == 0:
        return None
    percent = int(round((total - left) / total * 100)) if total > 0 else 0
    return QueueProgress(percent=max(0, min(100, percent)), timeleft=timeleft,
                         download_ids=download_ids, count=count)
