"""Tests for ledgr_slack.export.fy — financial-year model.

All cases are from §3 of the design spec
(docs/superpowers/specs/2026-06-12-ledgr-client-onboarding-fy-routing-design.md).
"""

import pytest
from datetime import date

from ledgr_slack.export.fy import last_day_of_month, fy_for_date


# --------------------------------------------------------------------------- #
# last_day_of_month
# --------------------------------------------------------------------------- #
class TestLastDayOfMonth:
    def test_leap_year_february(self):
        assert last_day_of_month(2024, 2) == date(2024, 2, 29)

    def test_non_leap_year_february(self):
        assert last_day_of_month(2025, 2) == date(2025, 2, 28)

    def test_march_31(self):
        assert last_day_of_month(2025, 3) == date(2025, 3, 31)

    def test_december_31(self):
        assert last_day_of_month(2025, 12) == date(2025, 12, 31)

    def test_april_30(self):
        assert last_day_of_month(2025, 4) == date(2025, 4, 30)


# --------------------------------------------------------------------------- #
# fy_for_date — fye_month = 3 (March FYE)
# --------------------------------------------------------------------------- #
class TestFyForDateMarchFye:
    def test_mid_march_same_fy(self):
        # 2025-03-15: before FYE (31 Mar 2025) → FY2025
        assert fy_for_date(date(2025, 3, 15), 3) == 2025

    def test_fye_day_itself_same_fy(self):
        # 2025-03-31: the FYE day itself → FY2025
        assert fy_for_date(date(2025, 3, 31), 3) == 2025

    def test_day_after_fye_next_fy(self):
        # 2025-04-01: one day after FYE → FY2026
        assert fy_for_date(date(2025, 4, 1), 3) == 2026

    def test_april_2_next_fy(self):
        # 2025-04-02 → FY2026
        assert fy_for_date(date(2025, 4, 2), 3) == 2026


# --------------------------------------------------------------------------- #
# fy_for_date — fye_month = 12 (calendar year FYE)
# --------------------------------------------------------------------------- #
class TestFyForDateDecemberFye:
    def test_mid_year_same_fy(self):
        # 2025-06-01 → FY2025
        assert fy_for_date(date(2025, 6, 1), 12) == 2025

    def test_last_day_of_year_same_fy(self):
        # 2025-12-31: the FYE day itself → FY2025
        assert fy_for_date(date(2025, 12, 31), 12) == 2025

    def test_new_year_next_fy(self):
        # 2026-01-01: next calendar year → FY2026
        assert fy_for_date(date(2026, 1, 1), 12) == 2026

    def test_late_arriving_prior_year_doc(self):
        # Late-arriving: 2024-12-20 processed later still belongs to FY2024
        assert fy_for_date(date(2024, 12, 20), 12) == 2024


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
class TestFyValidation:
    def test_fye_month_0_raises(self):
        with pytest.raises(ValueError):
            fy_for_date(date(2025, 6, 1), 0)

    def test_fye_month_13_raises(self):
        with pytest.raises(ValueError):
            fy_for_date(date(2025, 6, 1), 13)
