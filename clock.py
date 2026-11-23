"""
Single source of "now" for the whole bot.

Every timestamp in this codebase is stored as naive UTC and compared against
the current time. This returns exactly that: a naive datetime in UTC, the same
value the deprecated utcnow call produced (deprecated since Python 3.12),
without changing any stored value or comparison.
"""

from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
