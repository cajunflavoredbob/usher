"""Result of a multi-step auto-fix / mark-failed orchestration.

Status:
  ok      : all steps succeeded
  partial : some work happened (e.g. blocklist + delete) but search failed,
            or vice versa. Caller should still surface what worked and
            enqueue the autofix poller if `should_poll` is True.
  failed  : first step failed; nothing happened that the user needs to know
            about beyond the message.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal, Optional

Status = Literal["ok", "partial", "failed"]


@dataclass
class FixResult:
    status: Status
    message: str
    steps_done: list[str] = field(default_factory=list)
    poll_info: Optional[dict] = None
    # What the file looked like before the Fix deleted it (empty when the
    # read failed or there was no file); stored on the pending fix row.
    before: "ReleaseFingerprint" = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.before is None:
            self.before = ReleaseFingerprint()

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
                poll_info: Optional[dict] = None,
                before: "Optional[ReleaseFingerprint]" = None) -> "FixResult":
        return cls(status="ok", message=message, steps_done=list(steps_done),
                   poll_info=poll_info, before=before)

    @classmethod
    def partial(cls, message: str, steps_done: list[str],
                poll_info: Optional[dict] = None,
                before: "Optional[ReleaseFingerprint]" = None) -> "FixResult":
        return cls(status="partial", message=message, steps_done=list(steps_done),
                   poll_info=poll_info, before=before)

    @classmethod
    def failed(cls, message: str, steps_done: Optional[list[str]] = None) -> "FixResult":
        return cls(status="failed", message=message,
                   steps_done=list(steps_done) if steps_done else [])


@dataclass
class QueueProgress:
    """Aggregate download progress for the queue records of one movie or
    series, from the arr /queue/details endpoints. Backs the Progress
    cards (Request and Fix) and the SABnzbd priority boost."""
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


def find_grab_for_import(records: list[dict]) -> Optional[dict]:
    """The "grabbed" history record that produced the file now on disk.

    `records` is newest-first history for one item. The newest
    "downloadFolderImported" event names the release by its `downloadId`;
    the answer is the "grabbed" record sharing it. Returns None when there
    is no import event, it has no downloadId, or no grab matches, so the
    caller skips the blocklist instead of burying a guessed release.
    """
    imported = next(
        (rec for rec in records if rec.get("eventType") == "downloadFolderImported"),
        None,
    )
    download_id = imported.get("downloadId") if imported else None
    if not download_id:
        return None
    return next(
        (rec for rec in records
         if rec.get("eventType") == "grabbed" and rec.get("downloadId") == download_id),
        None,
    )


@dataclass(frozen=True)
class ReleaseFingerprint:
    """What is known about one release: release name (the file's scene name,
    or the grab's source title), release group, quality name, size in bytes,
    and the grab's downloadId. Every field is optional.

    `download_id` only records which grab was found; it is never compared
    (usenet download ids are new on every grab).
    """
    release_name: Optional[str] = None
    group: Optional[str] = None
    quality: Optional[str] = None
    size: Optional[int] = None
    download_id: Optional[str] = None

    def to_json(self) -> str:
        """JSON for the pending fix row; "" when nothing is known."""
        data = {k: v for k, v in (
            ("release_name", self.release_name), ("group", self.group),
            ("quality", self.quality), ("size", self.size),
            ("download_id", self.download_id)) if v is not None}
        return json.dumps(data) if data else ""

    @classmethod
    def from_json(cls, raw: Optional[str]) -> "ReleaseFingerprint":
        """Parse a stored value; "", None and anything unreadable give the
        empty fingerprint (an old row, or one that failed to read)."""
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except ValueError:
            return cls()
        if not isinstance(data, dict):
            return cls()
        size = data.get("size")
        return cls(
            release_name=_clean_str(data.get("release_name")),
            group=_clean_str(data.get("group")),
            quality=_clean_str(data.get("quality")),
            size=_clean_size(size),
            download_id=_clean_str(data.get("download_id")),
        )

    @property
    def known(self) -> bool:
        return any((self.release_name, self.group, self.quality, self.size))


def _clean_size(value) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _clean_str(value) -> Optional[str]:
    return value.strip() or None if isinstance(value, str) else None


def build_fingerprint(file_record: Optional[dict],
                      history: Optional[list]) -> ReleaseFingerprint:
    """Fingerprint of the file on disk from the arr's file record and the
    item's newest-first history. The grab is found as Mark Failed finds it
    (find_grab_for_import); its source title and group stand in for fields
    the file record lacks."""
    file_record = file_record if isinstance(file_record, dict) else {}
    grab = find_grab_for_import(history or [])
    grab_data = grab.get("data") if grab else None
    grab_data = grab_data if isinstance(grab_data, dict) else {}

    def quality_name(rec) -> Optional[str]:
        q = rec.get("quality") if isinstance(rec, dict) else None
        q = q.get("quality") if isinstance(q, dict) else None
        return _clean_str(q.get("name")) if isinstance(q, dict) else None

    size = file_record.get("size")
    return ReleaseFingerprint(
        release_name=(_clean_str(file_record.get("sceneName"))
                      or (_clean_str(grab.get("sourceTitle")) if grab else None)),
        group=(_clean_str(file_record.get("releaseGroup"))
               or _clean_str(grab_data.get("releaseGroup"))),
        quality=quality_name(file_record) or (quality_name(grab) if grab else None),
        size=_clean_size(size),
        download_id=_clean_str(grab.get("downloadId")) if grab else None,
    )


def compare_fingerprints(before: ReleaseFingerprint,
                         after: ReleaseFingerprint) -> str:
    """"same", "different" or "unknown" for a release and its replacement.

    same when both release names are known and equal ignoring case; else
    same when group, quality and size are known on both sides and all
    equal; different when any field known on both sides differs; unknown
    otherwise. The downloadId is never compared.
    """
    def eq(a, b) -> bool:
        return a.casefold() == b.casefold() if isinstance(a, str) else a == b

    pairs = [(before.release_name, after.release_name),
             (before.group, after.group),
             (before.quality, after.quality),
             (before.size, after.size)]
    if pairs[0][0] and pairs[0][1] and eq(*pairs[0]):
        return "same"
    if all(a and b for a, b in pairs[1:]) and all(eq(a, b) for a, b in pairs[1:]):
        return "same"
    if any(a and b and not eq(a, b) for a, b in pairs):
        return "different"
    return "unknown"
