"""
Shared time utilities for the bot.
Single source of truth for timezone and current-time helpers.
All cogs should import EASTERN and time helpers from here.
"""

from zoneinfo import ZoneInfo
from datetime import datetime

EASTERN = ZoneInfo("America/New_York")


def now_eastern() -> datetime:
    """Return the current time in Eastern timezone."""
    return datetime.now(EASTERN)


def format_time_eastern(dt: datetime, fmt: str = "%A, %B %d at %I:%M %p ET") -> str:
    """
    Format a datetime in Eastern time using the standard bot format.
    Converts to Eastern first if the datetime is timezone-aware.

    Args:
        dt: datetime object (aware or naive)
        fmt: strftime format string

    Returns:
        Formatted string, e.g. "Thursday, September 5 at 8:15 PM ET"
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(EASTERN)
    return dt.strftime(fmt)
