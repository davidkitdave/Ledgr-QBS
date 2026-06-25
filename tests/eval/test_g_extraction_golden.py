"""G-cluster extraction golden eval — WS-0.2 (ADR-0023 document lane).

For each G* case in ``tests/eval/datasets/ledgr.evalset.json``:

1. Load ``_eval_assertions`` from ``session_input.state``.
2. When the local PDF + Gemini creds are present, run
   ``extract_document_ledger`` (default understand path, WS-2.1).
3. Score via :func:`tests.eval.extraction_metrics.score_g_case` — the same
   functions registered in ``eval_config_extraction.yaml``.

This follows the F-cluster offline pattern (evalset + custom metrics) but
fires a live extraction call when credentials and local PDFs exist.

**Not** a standalone hardcoded pytest — the evalset is the contract.

Pre-WS-2 baseline: live cases were ``xfail`` until the faithful array schema
landed (WS-2.1).

Run::

    uv run pytest tests/eval/test_g_extraction_golden.py -m eval -v

Plan: ``docs/superpowers/plans/2026-06-21-intelligent-extraction-implementation.md``
"""

from __future__ import annotations

import os

import pytest

from invoice_processing.extract.segmentation_gates import (
    count_input_pages,
    validate_bundle_page_coverage,
    validate_page_ranges,
)

from tests.eval.extraction_metrics import (
    g_case_expected,
    g_case_ids,
    pdf_available,
    score_g_case,
)

_HAS_CREDS = bool(os.environ.get("GOOGLE_API_KEY") or os.environ.get("GOOGLE_CLOUD_PROJECT"))
_SKIP_NO_CREDS = "GOOGLE_API_KEY / GOOGLE_CLOUD_PROJECT not set"
_SKIP_NO_PDF = "Local golden PDF not on this machine"

# doc_count is a HARD gate — must be 1.0 when WS-2 lands.
GATE_THRESHOLD = 1.0
_G2_CASE = "G2_soa_embedded_eleven"


def _g_live_case_params() -> list:
    """Active G cases; G2 stays xfail until SOA segmentation stabilises (WS-2.5)."""
    params: list = []
    for case_id in g_case_ids(active_only=True):
        if case_id == _G2_CASE:
            params.append(
                pytest.param(
                    case_id,
                    marks=pytest.mark.xfail(
                        reason=(
                            "SOA embedded doc count can flake 11 vs 12 on live Gemini — "
                            "WS-2.5 partial-failure semantics pending"
                        ),
                        strict=False,
                    ),
                )
            )
        else:
            params.append(case_id)
    return params


def _run_live_extraction(case_id: str) -> dict:
    """Extract from the local PDF via the default understand path."""
    from invoice_processing.extract.ledger_extract import extract_document_ledger
    from invoice_processing.extract.invoice_extractor import mime_for

    from tests.eval.extraction_metrics import g_case_expected, scenario_pdf_path

    expected = g_case_expected(case_id)
    pdf = scenario_pdf_path(expected["scenario_key"])
    bundle = extract_document_ledger(pdf.read_bytes(), mime_for(pdf))
    totals = [float(doc.grand_total) for doc in bundle.documents]

    total_pages = count_input_pages(pdf.read_bytes(), mime_for(pdf))
    page_coverage_ok, _ = validate_bundle_page_coverage(bundle, total_pages=total_pages)

    return {
        "doc_count": len(bundle.documents),
        "grand_totals": totals,
        "page_coverage_ok": page_coverage_ok,
        "skipped_pages": bundle.skipped_pages,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Hermetic — page coverage helper (always runs, no LLM)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.eval
class TestPageCoverageHelpers:
    def test_full_coverage_with_skipped_cover(self):
        ok, detail = validate_page_ranges(
            [(2, 3), (4, 5)],
            total_pages=5,
            skipped_pages=[1],
        )
        assert ok, detail

    def test_detects_gap(self):
        ok, detail = validate_page_ranges([(1, 2), (4, 4)], total_pages=4)
        assert not ok
        assert "gaps" in detail


# ─────────────────────────────────────────────────────────────────────────────
# Live extraction gate — evalset-driven, custom-metric scored
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.eval
@pytest.mark.parametrize("case_id", _g_live_case_params())
def test_g_case_live_extraction(case_id: str) -> None:
    """G-cluster gate: doc_count + totals must score 1.0 after WS-2."""
    if g_case_expected(case_id).get("pending"):
        pytest.skip("case pending local doc or annotation")
    if not pdf_available(case_id):
        pytest.skip(_SKIP_NO_PDF)
    if not _HAS_CREDS:
        pytest.skip(_SKIP_NO_CREDS)

    actual = _run_live_extraction(case_id)
    scores = score_g_case(case_id, actual)

    for metric, score in scores.items():
        threshold = GATE_THRESHOLD if metric == "doc_count_score" else GATE_THRESHOLD
        assert score >= threshold, (
            f"{case_id}: {metric} = {score:.3f} < {threshold}; actual={actual!r}"
        )


@pytest.mark.eval
def test_g_cluster_evalset_has_no_vendor_strings_in_assertions() -> None:
    """Privacy gate: G-case _eval_assertions must not name real vendors."""
    from tests.eval.extraction_metrics import _g_case_fixture

    banned = ["JBI PLUS", "ATOM AUTO", "YAU LEE", "M PREMIUM", "IA-", "CNA-"]
    for case_id in g_case_ids(active_only=False):
        case = _g_case_fixture(case_id)
        assertions = (case.get("session_input") or {}).get("state", {}).get(
            "_eval_assertions", {}
        )
        blob = str(assertions)
        hits = [t for t in banned if t in blob]
        assert not hits, f"{case_id} assertions contain banned tokens: {hits}"
