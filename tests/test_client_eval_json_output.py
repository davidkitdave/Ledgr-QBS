"""Hermetic unit tests for the JSON baseline + regression diff in client_eval.

The live client_eval hits the Gemini API; these tests exercise the new
``--output`` and ``--compare-to`` plumbing without any network calls, so CI
can verify the regression-gate logic deterministically.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.client_eval import (
    ClientReport,
    _compare_reports,
    _report_to_dict,
)


def _make_report(
    *,
    client_id: str = "Acme",
    direction_total: int = 10,
    direction_correct: int = 9,
    recon_pass: int = 8,
    recon_eligible: int = 10,
) -> ClientReport:
    return ClientReport(
        client_id=client_id,
        setup_path=str(Path("/tmp") / f"{client_id} Client Setup.xlsx"),
        n_docs=direction_total,
        direction_total=direction_total,
        direction_correct=direction_correct,
        classify_ok=direction_total,
        recon_eligible=recon_eligible,
        recon_pass=recon_pass,
        errors=0,
    )


def test_report_to_dict_round_trip_is_json_safe():
    """A ClientReport serializes to a JSON-safe dict with the expected shape."""
    report = _make_report()
    out = _report_to_dict(report)
    # Sanity: every value is JSON-serializable.
    serialized = json.dumps(out, default=str)
    assert json.loads(serialized) == out
    # Direction rate is stored as a fraction (0–1) for clean math downstream.
    assert out["direction_rate"] == pytest.approx(0.9, abs=0.01)
    assert out["direction_total"] == 10
    assert out["direction_correct"] == 9


def test_compare_reports_flags_regression_when_direction_drops_5pp():
    """A drop of >5 percentage points in direction rate flags ``regressed=True``."""
    baseline = _report_to_dict(_make_report(direction_total=10, direction_correct=9))
    # 9/10 = 90% baseline; new run 7/10 = 70% → 20pp drop → REGRESSED.
    current = _report_to_dict(_make_report(direction_total=10, direction_correct=7))
    diff = _compare_reports(baseline, current)
    assert diff["regressed"] is True
    assert diff["metrics"]["direction_rate"]["delta"] == pytest.approx(-0.2, abs=0.001)
    assert diff["metrics"]["direction_rate"]["baseline"] == pytest.approx(0.9, abs=0.001)
    assert diff["metrics"]["direction_rate"]["current"] == pytest.approx(0.7, abs=0.001)


def test_compare_reports_passes_when_direction_within_5pp():
    """A 3pp drop is within the regression tolerance and does NOT flag regression."""
    baseline = _report_to_dict(_make_report(direction_total=30, direction_correct=27))
    # 26/30 = 86.67% → -3.33pp vs 90% → within tolerance, not regressed.
    current = _report_to_dict(_make_report(direction_total=30, direction_correct=26))
    diff = _compare_reports(baseline, current)
    assert diff["regressed"] is False
    assert diff["metrics"]["direction_rate"]["delta"] < 0  # direction did drop
    assert diff["metrics"]["direction_rate"]["delta"] > -0.05  # but within tolerance


def test_compare_reports_passes_when_direction_improves():
    """Improvements are welcome, not regressions."""
    baseline = _report_to_dict(_make_report(direction_total=10, direction_correct=9))
    # 10/10 = 100% → +10pp vs 90% → not regressed.
    current = _report_to_dict(_make_report(direction_total=10, direction_correct=10))
    diff = _compare_reports(baseline, current)
    assert diff["regressed"] is False
    assert diff["metrics"]["direction_rate"]["delta"] == pytest.approx(0.1, abs=0.001)


def test_compare_reports_handles_recon_rate_unchanged():
    """Recon rate moves are reported but don't gate the regression flag."""
    baseline = _report_to_dict(
        _make_report(direction_total=10, direction_correct=9, recon_pass=8, recon_eligible=10)
    )
    current = _report_to_dict(
        _make_report(direction_total=10, direction_correct=9, recon_pass=6, recon_eligible=10)
    )
    diff = _compare_reports(baseline, current)
    # Direction is unchanged → no regression.
    assert diff["regressed"] is False
    # But the recon rate drop is reported for context.
    assert diff["metrics"]["recon_rate"]["delta"] == pytest.approx(-0.2, abs=0.001)


def test_compare_reports_handles_mismatched_client_ids():
    """A diff against an unrelated client is a no-op (returns the bare shape)."""
    baseline = _report_to_dict(_make_report(client_id="Acme"))
    current = _report_to_dict(_make_report(client_id="Other"))
    diff = _compare_reports(baseline, current)
    # No assertion on direction delta when client_id mismatches; we just want
    # the function to NOT crash and the regression flag to default to False.
    assert diff["regressed"] is False
