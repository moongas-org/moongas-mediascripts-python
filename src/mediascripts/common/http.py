"""Helpers for interpreting HTTP response headers."""

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def retry_after_seconds(value: str) -> float | None:
    """Parse Retry-After as delay-seconds or an HTTP date."""
    if re.fullmatch(r"[0-9]+", value.strip()):
        return float(value)
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None
