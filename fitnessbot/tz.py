"""Timezone helpers — single source of truth for user-local date/time."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fitnessbot import db

DEFAULT_TZ = "America/Toronto"


def _tz(tz_str: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz_str or DEFAULT_TZ)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def user_now(user_id: int | None = None, *, tz_str: str | None = None) -> datetime:
    """Return the current datetime in the user's timezone."""
    if tz_str is None and user_id is not None:
        user = db.get_user_by_id(user_id)
        tz_str = user.get("timezone", DEFAULT_TZ) if user else DEFAULT_TZ
    return datetime.now(timezone.utc).astimezone(_tz(tz_str))


def user_today(user_id: int | None = None, *, tz_str: str | None = None) -> str:
    """Return today's date string (YYYY-MM-DD) in the user's timezone."""
    return user_now(user_id, tz_str=tz_str).strftime("%Y-%m-%d")


def user_date_fmt(user_id: int | None = None, *, tz_str: str | None = None, fmt: str = "%A, %b %d") -> str:
    """Return a formatted date string in the user's timezone."""
    return user_now(user_id, tz_str=tz_str).strftime(fmt)


def user_hour(user_id: int | None = None, *, tz_str: str | None = None) -> int:
    """Return the current hour (0-23) in the user's timezone."""
    return user_now(user_id, tz_str=tz_str).hour


def utc_offset_hours(user_id: int | None = None, *, tz_str: str | None = None) -> float:
    """Return the current UTC offset in hours for the user's timezone."""
    now = user_now(user_id, tz_str=tz_str)
    return now.utcoffset().total_seconds() / 3600


def day_utc_range(date_str: str, user_id: int | None = None, *, tz_str: str | None = None) -> tuple[str, str]:
    """Convert a user-local date (YYYY-MM-DD) to a UTC start/end range.

    Returns (start_utc, end_utc) as ISO strings suitable for
    ``WHERE logged_at >= ? AND logged_at < ?``.
    """
    if tz_str is None and user_id is not None:
        user = db.get_user_by_id(user_id)
        tz_str = user.get("timezone", DEFAULT_TZ) if user else DEFAULT_TZ
    zi = _tz(tz_str)
    local_start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=zi)
    utc_start = local_start.astimezone(timezone.utc)
    utc_end = utc_start + timedelta(days=1)
    return (
        utc_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        utc_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
