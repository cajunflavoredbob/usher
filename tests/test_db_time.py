"""The stored timestamp helpers shared by the auto-retry and Share expiry code."""
from datetime import datetime, timedelta, timezone

from const import db_time, parse_db_time


def test_a_stamp_is_utc_and_round_trips():
    when = datetime(2026, 11, 14, 22, 0, 5, tzinfo=timezone(timedelta(hours=-6)))
    assert db_time(when) == "2026-11-15 04:00:05"
    assert parse_db_time("2026-11-15 04:00:05") == datetime(
        2026, 11, 15, 4, 0, 5, tzinfo=timezone.utc)
    assert parse_db_time(db_time(when)) == when
