"""Hermetic extraction tournament tests (no Gemini)."""

from invoice_processing.extract.extraction_spine import ExtractionVariant
from eval.extraction_tournament import run_tournament


def test_tournament_hermetic_runs_all_variants():
    report = run_tournament(
        variants=[ExtractionVariant.V0, ExtractionVariant.V1, ExtractionVariant.V2],
        hermetic=True,
    )
    assert report["hermetic"] is True
    assert len(report["ranking"]) == 3
    assert report["winner"] in ("V0", "V1", "V2", "V3")
    assert len(report["results"]) == 9  # 3 fixtures × 3 variants


def test_tournament_hermetic_v0_v1_both_score():
    report = run_tournament(
        variants=[ExtractionVariant.V0, ExtractionVariant.V1],
        hermetic=True,
    )
    assert len(report["results"]) == 6
    for row in report["ranking"]:
        assert row["avg_score"] >= 0.0
