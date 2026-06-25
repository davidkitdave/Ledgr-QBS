"""G-cluster extraction golden metrics — WS-0.2 (ADR-0023 document lane).

Custom ADK metrics for multi-document extraction fidelity. The evalset
(:file:`datasets/ledgr.evalset.json`, G* cases) is the single source of
truth; this module is the scoring pipe.

Local PDFs live under ``~/Desktop/LocalTest/TestDoc/MYDoc/`` on the
developer machine only — never committed. Scenario keys in the evalset map
to relative paths via :data:`SCENARIO_PDF_RELATIVE`.

Metrics (each returns 0.0–1.0):

- ``doc_count_score`` — HARD gate: extracted doc count == expected
- ``extraction_totals_score`` — per-doc grand totals within tolerance
- ``page_coverage_score`` — page ranges cover all non-skipped pages (WS-2)

Both the pytest gate (:func:`score_g_case`) and the ADK custom-metric
adapters (:func:`adk_doc_count_score`, etc.) read the same tables so they
cannot drift.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Optional

from google.adk.evaluation.eval_case import ConversationScenario, Invocation
from google.adk.evaluation.eval_metrics import EvalStatus
from google.adk.evaluation.evaluator import EvaluationResult, PerInvocationResult

from invoice_processing.extract.segmentation_gates import validate_page_ranges

_REPO_ROOT = pathlib.Path(__file__).parent.parent.parent
_EVALSET_PATH = _REPO_ROOT / "tests" / "eval" / "datasets" / "ledgr.evalset.json"

# Local test PDF root — structural path only; no client data in repo.
_CLIENT_DATA_ROOT = pathlib.Path.home() / "Desktop/LocalTest/TestDoc/MYDoc"
_CLIENT_PDF_ROOT = _CLIENT_DATA_ROOT / "Acme Auto Enterprise"

# Scenario key → relative PDF under _CLIENT_PDF_ROOT (local machine only).
SCENARIO_PDF_RELATIVE: dict[str, str] = {
    "multi_invoice_regression": "Purchase/Prime Euro Parts - DEC 2025.pdf",
    "soa_embedded_eleven": "Purchase/Bolt Auto Supply - DEC 2025_.pdf",
    "soa_skip_cover_six": "Purchase/Gearbox Lab - DEC 2025_.pdf",
    "single_summary_invoice": "Purchase/Swift Courier - DEC 2025 - SWFT0000000.pdf",
    "non_english_doc": "中国订货 - DEC 2025_.pdf",
}

# Per-case decision table — keyed by eval_id (G1..G4 active; G5/G6 pending).
_G_CASE_TABLE: dict[str, dict[str, Any]] = {
    "G1_multi_invoice_doc_count": {
        "scenario_key": "multi_invoice_regression",
        "expected_doc_count": 2,
        "documents": [
            {"grand_total": 200.0, "tolerance": 0.02},
            {"grand_total": 60.0, "tolerance": 0.02},
        ],
        "pending": False,
    },
    "G2_soa_embedded_eleven": {
        "scenario_key": "soa_embedded_eleven",
        "expected_doc_count": 11,
        "documents": [],
        "pending": False,
    },
    "G3_soa_skip_cover_six": {
        "scenario_key": "soa_skip_cover_six",
        "expected_doc_count": 6,
        "documents": [
            {"grand_total": 280.0, "tolerance": 0.02},
            {"grand_total": 705.0, "tolerance": 0.02},
            {"grand_total": 168.0, "tolerance": 0.02},
            {"grand_total": 735.0, "tolerance": 0.02},
            {"grand_total": 2545.0, "tolerance": 0.02},
            {"grand_total": 1350.0, "tolerance": 0.02},
        ],
        "pending": False,
    },
    "G4_single_summary_invoice": {
        "scenario_key": "single_summary_invoice",
        "expected_doc_count": 1,
        "documents": [{"grand_total": 75.55, "tolerance": 0.02}],
        "pending": False,
    },
    "G5_non_english_doc": {
        "scenario_key": "non_english_doc",
        "expected_doc_count": 1,
        "documents": [],
        "pending": True,
    },
    "G6_segmentation_stress": {
        "scenario_key": "segmentation_stress",
        "expected_doc_count": 3,
        "documents": [],
        "pending": True,
    },
}


def g_case_ids(*, active_only: bool = True) -> list[str]:
    """Return ordered G-cluster eval case IDs."""
    if not active_only:
        return list(_G_CASE_TABLE.keys())
    return [cid for cid, row in _G_CASE_TABLE.items() if not row.get("pending")]


def g_case_expected(case_id: str) -> dict[str, Any]:
    """Return the per-case expected table for a G-cluster case ID."""
    return _G_CASE_TABLE[case_id]


def scenario_pdf_path(scenario_key: str) -> pathlib.Path:
    """Resolve a scenario key to the local PDF path."""
    rel = SCENARIO_PDF_RELATIVE[scenario_key]
    return _CLIENT_PDF_ROOT / rel


def pdf_available(case_id: str) -> bool:
    """True when the local PDF for this G-case exists on disk."""
    expected = g_case_expected(case_id)
    key = expected.get("scenario_key")
    if not key or key not in SCENARIO_PDF_RELATIVE:
        return False
    return scenario_pdf_path(key).exists()


def _grand_totals_within_tolerance(
    actual: list[float],
    expected_docs: list[dict],
) -> tuple[bool, str]:
    if not expected_docs:
        return True, "no per-doc totals configured"
    if len(actual) != len(expected_docs):
        return False, f"count mismatch: got {len(actual)} totals, expected {len(expected_docs)}"

    actual_sorted = sorted(actual)
    expected_sorted = sorted(expected_docs, key=lambda r: r["grand_total"])
    for got, row in zip(actual_sorted, expected_sorted):
        tol = float(row.get("tolerance", 0.02))
        want = float(row["grand_total"])
        if abs(got - want) > tol:
            return False, f"total {got} != {want} (tol={tol})"
    return True, "ok"


def score_g_case(case_id: str, actual: dict[str, Any]) -> dict[str, float]:
    """Score one G-cluster case against the per-case expected table."""
    expected = g_case_expected(case_id)
    doc_count = int(actual.get("doc_count") or 0)
    want_count = int(expected["expected_doc_count"])
    doc_count_score = 1.0 if doc_count == want_count else 0.0

    totals = actual.get("grand_totals") or []
    ok, _ = _grand_totals_within_tolerance(totals, expected.get("documents") or [])
    extraction_totals_score = 1.0 if ok else 0.0

    page_ok = actual.get("page_coverage_ok")
    if page_ok is None:
        page_coverage_score = 1.0
    else:
        page_coverage_score = 1.0 if page_ok else 0.0

    return {
        "doc_count_score": doc_count_score,
        "extraction_totals_score": extraction_totals_score,
        "page_coverage_score": page_coverage_score,
    }


def _load_evalset() -> dict[str, Any]:
    return json.loads(_EVALSET_PATH.read_text())


def _g_case_fixture(case_id: str) -> dict[str, Any]:
    raw = _load_evalset()
    for case in raw["eval_cases"]:
        if case["eval_id"] == case_id:
            return case
    raise KeyError(f"G-case {case_id!r} not found in {_EVALSET_PATH}")


def _extract_actual_from_invocation(inv: Invocation) -> dict[str, Any]:
    try:
        text = inv.user_content.parts[0].text
        return json.loads(text)
    except (AttributeError, IndexError, ValueError, TypeError):
        return {}


def _adk_eval_from_invocations(
    metric_name: str,
    eval_metric,
    actual_invocations: list[Invocation],
) -> EvaluationResult:
    if not actual_invocations:
        return EvaluationResult(overall_score=0.0, overall_eval_status=EvalStatus.FAILED)

    per_invocation: list[PerInvocationResult] = []
    scores: list[float] = []
    for inv in actual_invocations:
        case_id = inv.invocation_id
        actual = _extract_actual_from_invocation(inv)
        if case_id in _G_CASE_TABLE:
            metric_value = score_g_case(case_id, actual).get(metric_name, 0.0)
        else:
            metric_value = 0.0
        scores.append(metric_value)
        per_invocation.append(
            PerInvocationResult(
                actual_invocation=inv,
                score=metric_value,
                eval_status=(
                    EvalStatus.PASSED if metric_value >= 1.0 else EvalStatus.FAILED
                ),
            )
        )
    overall = sum(scores) / len(scores) if scores else 0.0
    return EvaluationResult(
        overall_score=overall,
        overall_eval_status=EvalStatus.PASSED if overall >= 1.0 else EvalStatus.FAILED,
        per_invocation_results=per_invocation,
    )


def adk_doc_count_score(
    eval_metric,
    actual_invocations: list[Invocation],
    expected_invocations: Optional[list[Invocation]] = None,
    conversation_scenario: Optional[ConversationScenario] = None,
) -> EvaluationResult:
    return _adk_eval_from_invocations("doc_count_score", eval_metric, actual_invocations)


def adk_extraction_totals_score(
    eval_metric,
    actual_invocations: list[Invocation],
    expected_invocations: Optional[list[Invocation]] = None,
    conversation_scenario: Optional[ConversationScenario] = None,
) -> EvaluationResult:
    return _adk_eval_from_invocations(
        "extraction_totals_score", eval_metric, actual_invocations
    )


def adk_page_coverage_score(
    eval_metric,
    actual_invocations: list[Invocation],
    expected_invocations: Optional[list[Invocation]] = None,
    conversation_scenario: Optional[ConversationScenario] = None,
) -> EvaluationResult:
    return _adk_eval_from_invocations(
        "page_coverage_score", eval_metric, actual_invocations
    )
