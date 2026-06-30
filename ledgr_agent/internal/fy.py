"""Financial-year label from document date and client FYE month."""

from __future__ import annotations

import calendar
from datetime import date


def last_day_of_month(year: int, month: int) -> date:
    """Return the date of the last day of the given month."""
    return date(year, month, calendar.monthrange(year, month)[1])


def fy_for_date(d: date, fye_month: int) -> int:
    """FY label for document date *d* given the client's FYE month (1–12)."""
    if not 1 <= fye_month <= 12:
        raise ValueError(f"fye_month must be between 1 and 12, got {fye_month!r}")
    fye_this_year = last_day_of_month(d.year, fye_month)
    return d.year if d <= fye_this_year else d.year + 1
