"""Financial-year utilities for Ledgr.

The financial year is FYE-month driven: the FYE is the last day of ``fye_month``
(e.g. ``fye_month=3`` → 31 March each year).  A document dated ``d`` belongs to
the FY that ends on the first FYE on/after ``d``; the FY label is the calendar
year of that FYE.

Profile metadata (including ``fye_month``) comes from the per-channel Firestore
profile created at onboarding — not from a spreadsheet.
"""

from __future__ import annotations

import calendar
from datetime import date


def last_day_of_month(year: int, month: int) -> date:
    """Return the date of the last day of the given month."""
    return date(year, month, calendar.monthrange(year, month)[1])


def fy_for_date(d: date, fye_month: int) -> int:
    """Financial-year label for a document dated ``d``, given the client's FYE month.

    FYE = last day of ``fye_month``.  A document belongs to the FY that ends on
    the first FYE on/after ``d``; the FY label is the calendar year of that FYE.

    Args:
        d: The document date.
        fye_month: The client's financial-year-end month (1–12).

    Returns:
        The integer FY label (calendar year of the applicable FYE).

    Raises:
        ValueError: If ``fye_month`` is not in the range 1–12.
    """
    if not 1 <= fye_month <= 12:
        raise ValueError(f"fye_month must be between 1 and 12, got {fye_month!r}")
    fye_this_year = last_day_of_month(d.year, fye_month)
    return d.year if d <= fye_this_year else d.year + 1
