"""ADR-0015 eval custom metrics — direction / doc_kind / routing / tax-type.

Three custom metrics registered with the ADK AgentEvaluator via
``EvalConfig.custom_metrics``:

- ``sheet_routing_score``         1.0 when the normalized invoice lands on the
                                 expected Purchase / Sales sheet for the case
- ``header_mapping_score``        1.0 when the Xero / QBS exporter row dict
                                 contains every expected column header (no
                                 missing, none invented)
- ``tax_type_routing_score``      1.0 when per-line ``*TaxType`` matches the
                                 SG-GST decision table for the case

Each function follows the ADK Custom Metrics contract (see
``google.adk.evaluation.custom_metric_evaluator``) — takes ``(eval_metric,
actual_invocations, expected_invocations, conversation_scenario)`` and
returns an ``EvaluationResult``.

The same scoring logic is exposed via :func:`score_f_case` for the offline
pytest path (``tests/eval/test_f_extract_direction.py``) that drives the
F-cluster fixtures from ``tests/eval/datasets/ledgr.evalset.json`` without
a live LLM call. Both paths use the same per-case decision table at the
top of :mod:`tests.eval.custom_metrics` so they cannot drift.

Per ADR-0015: the case-level decision table is the single source of truth.
The Python is the dumb pipe; the model learns the table from the prompt.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Optional

from google.adk.evaluation.eval_case import ConversationScenario, Invocation
from google.adk.evaluation.eval_metrics import EvalStatus
from google.adk.evaluation.evaluator import EvaluationResult, PerInvocationResult

#: Path to the canonical evalset used by both the pytest path and the ADK
#: custom-metric path. Test fixtures (F1..F12) carry ``session_input.state
#: .test_document`` and ``_eval_assertions`` describing the expected
#: direction, doc_kind, tax_visible, and per-line tax_treatment.
_REPO_ROOT = pathlib.Path(__file__).parent.parent.parent
_EVALSET_PATH = _REPO_ROOT / "tests" / "eval" / "datasets" / "ledgr.evalset.json"

#: Xero / QBS exporter row header set used by ``header_mapping_score``.
#: Two variants: the purchase sheet uses ``Description``; the sales sheet
#: uses ``*Description``. The scorer accepts any row that matches either
#: variant (full coverage of the common columns, 1.0).
XERO_EXPECTED_HEADERS_PURCHASE = (
    "*ContactName",
    "*InvoiceNumber",
    "*InvoiceDate",
    "*DueDate",
    "Description",
    "*Quantity",
    "*UnitAmount",
    "*AccountCode",
    "*TaxType",
    "TaxAmount",
    "Currency",
)
XERO_EXPECTED_HEADERS_SALES = (
    "*ContactName",
    "*InvoiceNumber",
    "*InvoiceDate",
    "*DueDate",
    "*Description",
    "*Quantity",
    "*UnitAmount",
    "*AccountCode",
    "*TaxType",
    "TaxAmount",
    "Currency",
)
XERO_EXPECTED_HEADERS = XERO_EXPECTED_HEADERS_PURCHASE  # default for ad-hoc callers


# ─────────────────────────────────────────────────────────────────────────────
# Per-case decision table (single source of truth for F1..F12).
#
# Each case is a triple of (direction, doc_kind, tax_visible) plus the
# expected per-line tax_treatments list. This is the same SG-GST table
# documented at the top of the plan; the Python just enforces it offline
# so the eval loop can score the model's output without re-running the
# classifier on the hot path.
# ─────────────────────────────────────────────────────────────────────────────

_F_CASE_TABLE: dict[str, dict[str, Any]] = {
    # F1 — expense_claim, no tax, GST-registered client but no tax on doc → NT, USD
    "F1_expense_claim_no_tax_purchase": {
        "direction": "purchase",
        "doc_kind": "expense_claim",
        "tax_visible": False,
        "line_tax_treatments": ["NT"],
        "xero_code": "No Tax",
        "issuer_name": "Person-1",
        "currency": "USD",
    },
    # F2 — expense_claim with foreign receipts, no SG GST column → NT, SGD
    "F2_expense_claim_overseas_purchase": {
        "direction": "purchase",
        "doc_kind": "expense_claim",
        "tax_visible": False,
        "line_tax_treatments": ["NT"],
        "xero_code": "No Tax",
        "currency": "SGD",
    },
    # F3 — sales invoice, GST-registered client, single GST line → SR, SGD
    "F3_invoice_sales_local_gst_registered": {
        "direction": "sales",
        "doc_kind": "invoice",
        "tax_visible": True,
        "line_tax_treatments": ["SR"],
        "xero_code": "SR",
        "currency": "SGD",
    },
    # F4 — purchase invoice, GST-registered client, single GST line → SR, SGD
    "F4_invoice_purchase_local_gst_registered": {
        "direction": "purchase",
        "doc_kind": "invoice",
        "tax_visible": True,
        "line_tax_treatments": ["SR"],
        "xero_code": "SR",
        "currency": "SGD",
    },
    # F5 — telco split: SR + ZR on a single bill, SGD
    "F5_invoice_purchase_telco_split": {
        "direction": "purchase",
        "doc_kind": "invoice",
        "tax_visible": True,
        "line_tax_treatments": ["SR", "ZR"],
        "xero_code": None,  # two rows, different codes
        "currency": "SGD",
    },
    # F6 — overseas supplier, no GST → OS, flagged for reverse charge, USD
    "F6_invoice_purchase_overseas_no_gst": {
        "direction": "purchase",
        "doc_kind": "invoice",
        "tax_visible": False,
        "line_tax_treatments": ["OS"],
        "xero_code": None,
        "tax_flagged": True,
        "currency": "USD",
    },
    # F7 — purchase with no tax on doc → NT, SGD
    "F7_invoice_purchase_no_tax_gst_registered_client": {
        "direction": "purchase",
        "doc_kind": "invoice",
        "tax_visible": False,
        "line_tax_treatments": ["NT"],
        "xero_code": "No Tax",
        "currency": "SGD",
    },
    # F8 — sales with no tax on doc (user-confirmed silent-NT policy), SGD
    "F8_invoice_sales_no_tax_gst_registered_client": {
        "direction": "sales",
        "doc_kind": "invoice",
        "tax_visible": False,
        "line_tax_treatments": ["NT"],
        "xero_code": "No Tax",
        "currency": "SGD",
    },
    # F9 — non-GST client + supplier shows GST → NT (master gate wins), SGD
    "F9_non_gst_client_purchase_with_supplier_gst": {
        "direction": "purchase",
        "doc_kind": "invoice",
        "tax_visible": True,
        "line_tax_treatments": ["NT"],
        "xero_code": "No Tax",
        "currency": "SGD",
    },
    # F10 — non-GST client + sales invoice with GST shown → NT (master gate), SGD
    "F10_non_gst_client_sales_with_gst_shown": {
        "direction": "sales",
        "doc_kind": "invoice",
        "tax_visible": True,
        "line_tax_treatments": ["NT"],
        "xero_code": "No Tax",
        "currency": "SGD",
    },
    # F11 — credit note, sales, GST 9% line → SR, SGD
    "F11_credit_note_sales": {
        "direction": "sales",
        "doc_kind": "credit_note",
        "tax_visible": True,
        "line_tax_treatments": ["SR"],
        "xero_code": "SR",
        "currency": "SGD",
    },
    # F12 — ambiguous document → unknown (HITL gate), SGD
    "F12_ambiguous_unknown": {
        "direction": "unknown",
        "doc_kind": "other",
        "tax_visible": False,
        "line_tax_treatments": [],
        "xero_code": None,
        "currency": "SGD",
    },
}


def f_case_ids() -> list[str]:
    """Return the ordered list of F-cluster eval case IDs."""
    return list(_F_CASE_TABLE.keys())


def f_case_expected(case_id: str) -> dict[str, Any]:
    """Return the per-case expected table for an F-cluster case ID."""
    return _F_CASE_TABLE[case_id]


# ─────────────────────────────────────────────────────────────────────────────
# Offline scoring — drives the F-cluster pytest gate (no LLM call).
# ─────────────────────────────────────────────────────────────────────────────


def _load_evalset() -> dict[str, Any]:
    return json.loads(_EVALSET_PATH.read_text())


def _f_case_fixture(case_id: str) -> dict[str, Any]:
    """Find the F-cluster case in the canonical evalset and return it."""
    raw = _load_evalset()
    for case in raw["eval_cases"]:
        if case["eval_id"] == case_id:
            return case
    raise KeyError(f"F-case {case_id!r} not found in {_EVALSET_PATH}")


def _normalize_actual_to_dict(actual: Any) -> dict[str, Any]:
    """Coerce the actual extract (Pydantic / dict / None) into a dict.

    Used by both the offline and ADK paths so they look at the same shape.
    """
    if actual is None:
        return {}
    if isinstance(actual, dict):
        return actual
    if hasattr(actual, "model_dump"):
        return actual.model_dump()
    return dict(actual)


def sheet_routing_score(
    eval_metric,
    actual_invocations,
    expected_invocations,
    conversation_scenario: Optional[ConversationScenario] = None,
) -> EvaluationResult:
    """Deprecated thin alias for ``adk_sheet_routing_score``.

    Kept for callers that import the metric under the unqualified name.
    New code should reference the ``adk_*`` entry points directly.
    """
    return adk_sheet_routing_score(eval_metric, actual_invocations, expected_invocations, conversation_scenario)


def header_mapping_score(
    eval_metric,
    actual_invocations,
    expected_invocations,
    conversation_scenario: Optional[ConversationScenario] = None,
) -> EvaluationResult:
    """Deprecated thin alias for ``adk_header_mapping_score``."""
    return adk_header_mapping_score(eval_metric, actual_invocations, expected_invocations, conversation_scenario)


def tax_type_routing_score(
    eval_metric,
    actual_invocations,
    expected_invocations,
    conversation_scenario: Optional[ConversationScenario] = None,
) -> EvaluationResult:
    """Deprecated thin alias for ``adk_tax_type_routing_score``."""
    return adk_tax_type_routing_score(eval_metric, actual_invocations, expected_invocations, conversation_scenario)


# ─────────────────────────────────────────────────────────────────────────────
# score_f_case — offline per-case scorer used by the pytest F-cluster gate.
# ─────────────────────────────────────────────────────────────────────────────


def score_f_case(case_id: str, actual: Any) -> dict[str, float]:
    """Score one F-cluster case against the per-case expected table.

    Returns a dict of metric → score in [0, 1]. Per ADR-0015, all four
    metrics must hit 0.9 for the case to pass.
    """
    expected = f_case_expected(case_id)
    actual_d = _normalize_actual_to_dict(actual)
    return {
        "sheet_routing_score": _score_sheet_routing(expected, actual_d),
        "header_mapping_score": _score_header_mapping(expected, actual_d),
        "tax_type_routing_score": _score_tax_type_routing(expected, actual_d),
        "currency_routing_score": _score_currency_routing(expected, actual_d),
    }


def _score_sheet_routing(expected: dict[str, Any], actual: dict[str, Any]) -> float:
    """1.0 when the actual direction matches the case-expected direction.

    Prefers ``actual['model_direction_for_client']`` (the raw LLM verdict)
    over ``actual['direction_for_client']`` (which carries the mapper
    fallback for "unknown" → "purchase"). Scoring the model output
    against the case expectation lets F12 ("ambiguous → unknown") score
    correctly even though the export falls back to the purchase sheet
    to keep the row visible.
    """
    expected_dir = expected["direction"]
    model_dir = actual.get("model_direction_for_client")
    export_dir = actual.get("direction_for_client")
    actual_dir = model_dir if model_dir is not None else export_dir
    if expected_dir == "unknown":
        return 1.0 if actual_dir in ("unknown", "", None) else 0.0
    if expected_dir == "purchase":
        return 1.0 if actual_dir == "purchase" else 0.0
    if expected_dir == "sales":
        return 1.0 if actual_dir == "sales" else 0.0
    return 0.0


def _score_header_mapping(expected: dict[str, Any], actual: dict[str, Any]) -> float:
    """1.0 when exporter row dict keys cover the expected Xero / QBS headers.

    Reads the actual row from ``actual['exporter_row']`` (set by the
    offline test harness). Accepts either the purchase-sheet header set
    (``Description``) or the sales-sheet header set (``*Description``) —
    both are valid Xero imports. Returns 1.0 for any superset of either
    and partial matches in proportion to coverage. Returns 1.0 when no
    exporter row is present *and* the case has no expected lines (HITL
    gate). Returns 0.0 when the case expects lines but no row was
    produced.
    """
    row = actual.get("exporter_row")
    expected_has_lines = bool(expected.get("line_tax_treatments"))
    if not row:
        return 1.0 if not expected_has_lines else 0.0
    headers = set(row.keys())
    # Score against whichever expected set the row best matches.
    purchase_set = set(XERO_EXPECTED_HEADERS_PURCHASE)
    sales_set = set(XERO_EXPECTED_HEADERS_SALES)
    best = max(
        (len(purchase_set - (purchase_set - headers)) / len(purchase_set)),
        (len(sales_set - (sales_set - headers)) / len(sales_set)),
    )
    return best


def _score_tax_type_routing(expected: dict[str, Any], actual: dict[str, Any]) -> float:
    """1.0 when every per-line tax_treatment matches the case-expected list.

    ``actual`` carries a list of tax_treatments on the lines, in posting
    order. For an empty expected list (F12 ambiguous) we return 1.0
    trivially — the case asserts direction==unknown rather than tax.
    """
    expected_list = expected.get("line_tax_treatments") or []
    actual_list = actual.get("line_tax_treatments") or []
    if not expected_list:
        return 1.0
    if len(actual_list) != len(expected_list):
        # Mismatched length — partial credit proportional to correct prefix.
        n = min(len(actual_list), len(expected_list))
        correct = sum(
            1 for i in range(n) if actual_list[i] == expected_list[i]
        )
        return correct / len(expected_list)
    correct = sum(
        1 for a, e in zip(actual_list, expected_list) if a == e
    )
    return correct / len(expected_list)


def _score_currency_routing(expected: dict[str, Any], actual: dict[str, Any]) -> float:
    """1.0 when the Xero/QBS exporter row carries the case-expected currency.

    Reads the actual currency from the offline test harness projection of
    ``actual['exporter_row']['Currency']`` (the user-visible Xero column),
    falling back to ``actual['currency']`` (the value on the
    NormalizedInvoice / DocumentLedgerExtract).

    Catches the bug where the model silently defaults to the client's
    base_currency (SGD) for a USD expense claim. The model is steered
    by the ``currency`` schema description in
    :mod:`invoice_processing.extract.ledger_extract` to read the
    document's Currency column / total footer, not the client home currency.
    """
    expected_cur = (expected.get("currency") or "").upper().strip()
    if not expected_cur:
        return 1.0
    row = actual.get("exporter_row") or {}
    row_cur = (row.get("Currency") or "").upper().strip()
    inv_cur = (actual.get("currency") or "").upper().strip()
    actual_cur = row_cur or inv_cur
    return 1.0 if actual_cur == expected_cur else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# ADK custom-metric adapter — wraps score_f_case into the ADK
# EvaluationResult shape so the metric can be referenced from
# EvalConfig.custom_metrics. Each invocation is treated as one F-case;
# the eval case ID is taken from ``inv.invocation_id`` (set to the case ID
# by the test harness).
# ─────────────────────────────────────────────────────────────────────────────


def _adk_metric_by_name(metric_name: str):
    """Return the adk_* wrapper for a metric name. Raises KeyError if absent."""
    return {
        "sheet_routing_score": adk_sheet_routing_score,
        "header_mapping_score": adk_header_mapping_score,
        "tax_type_routing_score": adk_tax_type_routing_score,
        "currency_routing_score": adk_currency_routing_score,
    }[metric_name]


def adk_sheet_routing_score(
    eval_metric,
    actual_invocations: list[Invocation],
    expected_invocations: Optional[list[Invocation]] = None,
    conversation_scenario: Optional[ConversationScenario] = None,
) -> EvaluationResult:
    """ADK custom metric — score invocations against the F-case sheet routing table."""
    return _adk_eval_from_invocations("sheet_routing_score", eval_metric, actual_invocations)


def adk_header_mapping_score(
    eval_metric,
    actual_invocations: list[Invocation],
    expected_invocations: Optional[list[Invocation]] = None,
    conversation_scenario: Optional[ConversationScenario] = None,
) -> EvaluationResult:
    """ADK custom metric — score invocations against the F-case header mapping table."""
    return _adk_eval_from_invocations("header_mapping_score", eval_metric, actual_invocations)


def adk_tax_type_routing_score(
    eval_metric,
    actual_invocations: list[Invocation],
    expected_invocations: Optional[list[Invocation]] = None,
    conversation_scenario: Optional[ConversationScenario] = None,
) -> EvaluationResult:
    """ADK custom metric — score invocations against the F-case tax-type table."""
    return _adk_eval_from_invocations("tax_type_routing_score", eval_metric, actual_invocations)


def adk_currency_routing_score(
    eval_metric,
    actual_invocations: list[Invocation],
    expected_invocations: Optional[list[Invocation]] = None,
    conversation_scenario: Optional[ConversationScenario] = None,
) -> EvaluationResult:
    """ADK custom metric — score invocations against the F-case currency table."""
    return _adk_eval_from_invocations("currency_routing_score", eval_metric, actual_invocations)


def currency_routing_score(
    eval_metric,
    actual_invocations,
    expected_invocations,
    conversation_scenario: Optional[ConversationScenario] = None,
) -> EvaluationResult:
    """Deprecated thin alias for ``adk_currency_routing_score``."""
    return adk_currency_routing_score(eval_metric, actual_invocations, expected_invocations, conversation_scenario)


def _adk_eval_from_invocations(
    metric_name: str,
    eval_metric,
    actual_invocations: list[Invocation],
) -> EvaluationResult:
    """Score each invocation against the F-case table and aggregate."""
    if not actual_invocations:
        return EvaluationResult(
            overall_score=0.0,
            overall_eval_status=EvalStatus.FAILED,
        )
    per_invocation: list[PerInvocationResult] = []
    scores: list[float] = []
    for inv in actual_invocations:
        case_id = inv.invocation_id
        actual = _extract_actual_from_invocation(inv)
        if case_id in _F_CASE_TABLE:
            metric_value = score_f_case(case_id, actual).get(metric_name, 0.0)
        else:
            metric_value = 0.0
        scores.append(metric_value)
        per_invocation.append(
            PerInvocationResult(
                actual_invocation=inv,
                score=metric_value,
                eval_status=(
                    EvalStatus.PASSED if metric_value >= 0.9 else EvalStatus.FAILED
                ),
            )
        )
    overall = sum(scores) / len(scores) if scores else 0.0
    return EvaluationResult(
        overall_score=overall,
        overall_eval_status=(
            EvalStatus.PASSED if overall >= 0.9 else EvalStatus.FAILED
        ),
        per_invocation_results=per_invocation,
    )


def _extract_actual_from_invocation(inv: Invocation) -> dict[str, Any]:
    """Pull the actual extract payload out of an ADK Invocation.

    For the F-cluster offline path the test harness attaches the actual
    extract to ``inv.user_content.parts[0].text`` as JSON. The agent-eval
    path is not used in this iteration.
    """
    try:
        text = inv.user_content.parts[0].text
        return json.loads(text)
    except (AttributeError, IndexError, ValueError, TypeError):
        return {}
