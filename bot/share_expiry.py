"""Share Expiry helpers: the choices the Admin picks from and how an Expiry
is stored and shown. The `share_expiries` table keeps `expires_at` as UTC
text ("%Y-%m-%d %H:%M:%S"), so a job can poll it with a plain comparison."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from const import db_time, parse_db_time

# The Expiry choices, in days. 0 stands for Never (or Clear on a Share view).
EXPIRY_DAYS = (7, 30, 90)


def expires_at_text(days: int, now: Optional[datetime] = None) -> str:
    """The UTC text stored for an Expiry `days` days from `now`."""
    now = now or datetime.now(timezone.utc)
    return db_time(now + timedelta(days=days))


def expiry_date_text(expires_at: str) -> str:
    """"Dec 14, 2026" for a stored Expiry (its UTC date)."""
    when = parse_db_time(expires_at)
    return f"{when:%b} {when.day}, {when.year}"
