"""
timezone_helper.py
==================
Single source of truth for all timestamp generation across the project.

Import this module everywhere instead of calling datetime.now(),
datetime.utcnow(), or SQL NOW() directly.

Usage:
    from timezone_helper import IST, now_ist, today_ist
"""

from datetime import datetime, timedelta, timezone, date

# ── IST constant ──────────────────────────────────────────────────────────────
# UTC+05:30  —  Asia/Kolkata
IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    """
    Return the current moment as a timezone-aware datetime in IST (UTC+05:30).

    Always use this instead of:
        - datetime.now()          ← naive, no tz info
        - datetime.utcnow()       ← UTC, not IST
        - SQL NOW()               ← server timezone, may differ
    """
    return datetime.now(IST)


def today_ist() -> date:
    """
    Return today's date in IST.

    Always use this for date-only comparisons (quota resets, log grouping)
    instead of date.today() which uses the OS/server local timezone.
    """
    return now_ist().date()


def ist_from_utc(utc_dt: datetime) -> datetime:
    """
    Convert a UTC datetime (naive or aware) to a timezone-aware IST datetime.

    Useful when reading TIMESTAMPTZ values back from Supabase, which returns
    them in UTC by default.
    """
    if utc_dt is None:
        return None
    if utc_dt.tzinfo is None:
        # Treat naive datetime as UTC
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(IST)
