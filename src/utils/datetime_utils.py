"""Datetime utilities for timezone-aware operations."""

from datetime import UTC, datetime, timedelta
from typing import Optional


def utc_now() -> datetime:
    """Get current UTC time as timezone-aware datetime."""
    return datetime.now(UTC)


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensure datetime is timezone-aware (UTC).

    Converts naive datetime to UTC-aware by assuming it's already in UTC.
    Returns None if input is None.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def time_since(dt: Optional[datetime]) -> Optional[timedelta]:
    """Calculate time elapsed since given datetime.

    Handles both naive and aware datetimes.
    Returns None if input is None.
    """
    if dt is None:
        return None
    return utc_now() - ensure_utc(dt)


def is_expired(dt: Optional[datetime], timeout: timedelta) -> bool:
    """Check if datetime is older than timeout.

    Returns True if dt is None (considered expired).
    Handles both naive and aware datetimes.
    """
    if dt is None:
        return True
    elapsed = time_since(dt)
    return elapsed is not None and elapsed > timeout


def is_past(dt: Optional[datetime]) -> bool:
    """Check if datetime is in the past.

    Returns False if dt is None.
    Handles both naive and aware datetimes.
    """
    if dt is None:
        return False
    return utc_now() > ensure_utc(dt)
