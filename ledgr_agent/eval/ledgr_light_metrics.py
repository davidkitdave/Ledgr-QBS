"""Reference-free invoice extraction metrics for ``ledgr_agent`` eval.

No hand-coded golden values — arithmetic self-consistency, classification accuracy,
and an LLM judge that reads the source PDF at grade time.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import pathlib
import statistics
from functools import lru_cache
from typing import Any, Optional

from google.adk.evaluation.conversation_scenarios import ConversationScenario
from google.adk.evaluation.eval_case import Invocation
from google.adk.evaluation.eval_metrics import EvalMetric, EvalStatus
from google.adk.evaluation.evaluator import EvaluationResult, PerInvocationResult
from google.genai import types

from ledgr_agent.internal.gemini import default_llm_config, make_client, std_model
from ledgr_agent.internal.schemas import ReadDocumentBundle

_log = logging.getLogger(__name__)

_EVALSET_PATH = pathlib.Path(__file__).resolve().parent / "datasets" / "ledgr_light.evalset.json"

_JUDGE_MODEL = "gemini-2.5-flash"
_REL_TOL = 0.01
_ABS_TOL = 0.02


def _money_close(got: float | None, want: float | None) -> bool:
    if got is None or want is None:
        return False
    tol = max(_ABS_TOL, abs(want) * _REL_TOL)
    return abs(got - want) <= tol


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def score_self_consistency_on_extraction(bundle: dict[str, Any]) -> dict[str, float]:
    """Score internal arithmetic for one ``read_doc`` bundle (0.0–1.0 per check)."""
    if not bundle:
        return {"overall": 0.0, "has_payload": 0.0}

    file_kind = bundle.get("file_kind") or ""
    if file_kind == "bank_statement":
        accounts = bundle.get("accounts") or []
        if not accounts:
            return {"overall": 0.0, "has_payload": 0.0}
        return {"overall": 1.0, "has_payload": 1.0, "bank_skipped": 1.0}

    documents = bundle.get("documents") or []
    if not documents:
        return {"overall": 0.0, "has_payload": 0.0}

    doc_scores: list[float] = []
    details: dict[str, float] = {"has_payload": 1.0}

    for idx, doc in enumerate(documents):
        prefix = f"doc{idx}_"
        lines = doc.get("lines") or []
        nets = [_as_float(line.get("net_amount")) for line in lines]
        nets = [n for n in nets if n is not None]
        subtotal = _as_float(doc.get("subtotal"))
        tax_total = _as_float(doc.get("tax_total"))
        grand_total = _as_float(doc.get("grand_total"))

        checks: list[float] = []
        if nets and subtotal is not None:
            line_sum = sum(nets)
            checks.append(1.0 if _money_close(line_sum, subtotal) else 0.0)
            details[f"{prefix}lines_sum_subtotal"] = checks[-1]
        elif subtotal is not None and not lines:
            checks.append(1.0)
        elif lines and subtotal is None:
            checks.append(0.0)
            details[f"{prefix}lines_sum_subtotal"] = 0.0

        if subtotal is not None and tax_total is not None and grand_total is not None:
            ok = _money_close(subtotal + tax_total, grand_total)
            checks.append(1.0 if ok else 0.0)
            details[f"{prefix}subtotal_tax_grand"] = checks[-1]

        if grand_total is not None:
            checks.append(1.0)
            details[f"{prefix}has_grand_total"] = 1.0
        else:
            checks.append(0.0)
            details[f"{prefix}has_grand_total"] = 0.0

        vendor = (doc.get("vendor_name") or doc.get("vendor") or "").strip()
        reference = (doc.get("invoice_number") or doc.get("reference") or "").strip()
        if vendor:
            checks.append(1.0)
            details[f"{prefix}has_vendor"] = 1.0
        else:
            checks.append(0.0)
            details[f"{prefix}has_vendor"] = 0.0
        if reference:
            checks.append(1.0)
            details[f"{prefix}has_reference"] = 1.0
        else:
            checks.append(0.0)
            details[f"{prefix}has_reference"] = 0.0

        breakdown = doc.get("tax_breakdown") or []
        if breakdown:
            tax_parts = [_as_float(comp.get("tax_amount")) for comp in breakdown]
            tax_parts = [t for t in tax_parts if t is not None]
            taxable_parts = [_as_float(comp.get("taxable_amount")) for comp in breakdown]
            taxable_parts = [t for t in taxable_parts if t is not None]
            if tax_parts and tax_total is not None:
                ok_tax = _money_close(sum(tax_parts), tax_total)
                checks.append(1.0 if ok_tax else 0.0)
                details[f"{prefix}breakdown_sum_tax"] = checks[-1]
            if taxable_parts and subtotal is not None:
                ok_taxable = _money_close(sum(taxable_parts), subtotal)
                checks.append(1.0 if ok_taxable else 0.0)
                details[f"{prefix}breakdown_sum_taxable"] = checks[-1]

        doc_scores.append(statistics.mean(checks) if checks else 0.0)

    details["overall"] = statistics.mean(doc_scores) if doc_scores else 0.0
    return details


def _pdf_bytes_from_user_content(user_content: Any) -> bytes | None:
    parts = getattr(user_content, "parts", None) or []
    for part in parts:
        inline = getattr(part, "inline_data", None)
        if inline is None:
            continue
        data = getattr(inline, "data", None)
        mime = getattr(inline, "mime_type", "") or ""
        if data and "pdf" in mime.lower():
            if isinstance(data, (bytes, bytearray)):
                return bytes(data)
            if isinstance(data, str):
                return base64.b64decode(data)
    return None


_HIERARCHY_MAX_LINES = 15


@lru_cache(maxsize=1)
def _case_expectations() -> dict[str, dict[str, str | int | bool | None]]:
    if not _EVALSET_PATH.is_file():
        return {}
    raw = json.loads(_EVALSET_PATH.read_text(encoding="utf-8"))
    out: dict[str, dict[str, str | int | bool | None]] = {}
    for case in raw.get("eval_cases") or []:
        case_id = case.get("eval_id")
        if not case_id:
            continue
        out[str(case_id)] = {
            "expected_file_kind": case.get("expected_file_kind"),
            "expected_document_kind": case.get("expected_document_kind"),
            "expected_document_kinds": case.get("expected_document_kinds"),
            "expected_document_count": case.get("expected_document_count"),
            "expect_hierarchy_scope": case.get("expect_hierarchy_scope"),
            "max_bookable_lines": case.get("max_bookable_lines"),
            "expect_itemized_lines": case.get("expect_itemized_lines"),
            "min_bookable_lines": case.get("min_bookable_lines"),
            "forbid_document_kinds": case.get("forbid_document_kinds"),
            "expect_tax_buckets": case.get("expect_tax_buckets"),
        }
    return out


@lru_cache(maxsize=1)
def _pdf_fingerprint_to_case_id() -> dict[str, str]:
    if not _EVALSET_PATH.is_file():
        return {}
    raw = json.loads(_EVALSET_PATH.read_text(encoding="utf-8"))
    mapping: dict[str, str] = {}
    for case in raw.get("eval_cases") or []:
        case_id = case.get("eval_id")
        if not case_id:
            continue
        for inv_raw in case.get("conversation") or []:
            for part in inv_raw.get("user_content", {}).get("parts") or []:
                inline = part.get("inline_data") or {}
                data = inline.get("data")
                if not data:
                    continue
                blob = base64.b64decode(data) if isinstance(data, str) else data
                fp = hashlib.sha256(blob).hexdigest()
                mapping[fp] = str(case_id)
    return mapping


def _case_id_from_invocation(invocation: Invocation) -> str:
    inv_id = invocation.invocation_id or ""
    if "-inv-" in inv_id:
        return inv_id.split("-inv-", 1)[0]
    expectations = _case_expectations()
    for case_id in expectations:
        if case_id and case_id in inv_id:
            return case_id
    pdf_bytes = _pdf_bytes_from_user_content(invocation.user_content)
    if pdf_bytes:
        fp = hashlib.sha256(pdf_bytes).hexdigest()
        matched = _pdf_fingerprint_to_case_id().get(fp)
        if matched:
            return matched
    return inv_id


def _bundle_from_agent_response(invocation: Invocation) -> dict[str, Any] | None:
    final = getattr(invocation, "final_response", None)
    if final is None:
        return None
    parts = getattr(final, "parts", None) or []
    chunks: list[str] = []
    for part in parts:
        text = getattr(part, "text", None)
        thought = getattr(part, "thought", False)
        if text and not thought:
            chunks.append(text)
    if not chunks:
        return None
    try:
        payload = json.loads("".join(chunks))
        return ReadDocumentBundle.model_validate(payload).model_dump()
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def score_classification_on_extraction(
    bundle: dict[str, Any],
    *,
    expected_file_kind: str | None,
    expected_document_kind: str | None,
    expected_document_kinds: list[str] | None = None,
    expected_document_count: int | None = None,
    forbid_document_kinds: list[str] | None = None,
) -> float:
    if not bundle or not expected_file_kind:
        return 0.0
    got_file_kind = bundle.get("file_kind") or ""
    if got_file_kind != expected_file_kind:
        return 0.0
    if expected_file_kind == "bank_statement":
        return 1.0 if bundle.get("accounts") else 0.0
    documents = bundle.get("documents") or []
    if not documents:
        return 0.0
    if expected_document_count is not None and len(documents) != expected_document_count:
        return 0.0
    kinds = [str(doc.get("document_kind") or "").strip().lower() for doc in documents]
    if not kinds:
        return 0.0
    if forbid_document_kinds:
        forbidden = {str(k).strip().lower() for k in forbid_document_kinds}
        if any(kind in forbidden for kind in kinds):
            return 0.0
    if expected_document_kinds is not None:
        want = sorted(str(k).strip().lower() for k in expected_document_kinds)
        return 1.0 if sorted(kinds) == want else 0.0
    if not expected_document_kind:
        return 0.0
    return 1.0 if all(kind == expected_document_kind for kind in kinds) else 0.0


def _tool_response_payload(invocation: Invocation, tool_name: str) -> dict[str, Any] | None:
    """Last ``function_response`` for ``tool_name`` from ADK invocation events."""
    intermediate = invocation.intermediate_data
    if intermediate is None:
        return None

    found: dict[str, Any] | None = None
    events = getattr(intermediate, "invocation_events", None) or []
    for event in events:
        dump = event.model_dump() if hasattr(event, "model_dump") else {}
        for part in (dump.get("content") or {}).get("parts") or []:
            fr = part.get("function_response") or {}
            if fr.get("name") != tool_name:
                continue
            raw = fr.get("response")
            if isinstance(raw, dict):
                found = raw

    if found is not None:
        return found

    # Legacy shape (older ADK traces).
    responses = getattr(intermediate, "tool_responses", None) or []
    for response in responses:
        if getattr(response, "name", None) != tool_name:
            continue
        raw = getattr(response, "response", None)
        if isinstance(raw, dict):
            return raw
    return None


def _documents_from_workbook(workbook: dict[str, Any]) -> list[dict[str, Any]]:
    """Rebuild document-shaped rows from ``build_sheets`` output for scoring."""
    docs_by_key: dict[str, dict[str, Any]] = {}
    for sheet in workbook.get("sheets") or []:
        for row in sheet.get("rows") or []:
            if not isinstance(row, dict):
                continue
            inv_num = str(row.get("Invoice Number") or row.get("invoice_number") or "")
            key = inv_num or f"sheet:{sheet.get('title')}"
            doc = docs_by_key.setdefault(
                key,
                {
                    "vendor_name": row.get("Vendor Name") or row.get("vendor_name"),
                    "invoice_number": inv_num,
                    "lines": [],
                },
            )
            doc["lines"].append(
                {
                    "description": row.get("Description") or row.get("description"),
                    "net_amount": _as_float(row.get("Source Amount") or row.get("net_amount")),
                }
            )
            subtotal = _as_float(row.get("Sub Total") or row.get("subtotal"))
            tax_total = _as_float(row.get("Tax Amount") or row.get("tax_total"))
            grand_total = _as_float(row.get("Total Amount") or row.get("grand_total"))
            if subtotal is not None:
                doc["subtotal"] = subtotal
            if tax_total is not None:
                doc["tax_total"] = tax_total
            if grand_total is not None:
                doc["grand_total"] = grand_total
    return list(docs_by_key.values())


def _extraction_bundle_from_invocation(invocation: Invocation) -> dict[str, Any] | None:
    """Bundle for scoring: prefer read_doc payload; fall back to workbook or agent JSON."""
    read_summary = _tool_response_payload(invocation, "read_doc")
    workbook = _tool_response_payload(invocation, "build_sheets")
    if read_summary and read_summary.get("status") == "error":
        agent_bundle = _bundle_from_agent_response(invocation)
        return agent_bundle

    file_kind = (
        (read_summary or {}).get("file_kind")
        or (workbook or {}).get("file_kind")
        or "commercial_documents"
    )

    if read_summary and read_summary.get("status") == "success":
        if read_summary.get("documents") or read_summary.get("accounts"):
            return read_summary
        if file_kind == "bank_statement" and read_summary.get("account_count"):
            return read_summary

    if workbook and workbook.get("status") == "success":
        documents = _documents_from_workbook(workbook)
        if documents:
            return {
                "file_kind": file_kind,
                "documents": documents,
                "read_summary": read_summary or {},
                "workbook": workbook,
            }

    agent_bundle = _bundle_from_agent_response(invocation)
    if agent_bundle is not None:
        return agent_bundle

    if read_summary and read_summary.get("status") == "success":
        return read_summary
    return None


def _result_from_scores(
    scores: list[float],
    actual_invocations: list[Invocation],
    threshold: float,
) -> EvaluationResult:
    if not scores:
        return EvaluationResult(overall_score=0.0, overall_eval_status=EvalStatus.FAILED)
    overall = statistics.mean(scores)
    status = EvalStatus.PASSED if overall >= threshold else EvalStatus.FAILED
    per = [
        PerInvocationResult(
            actual_invocation=inv,
            score=score,
            eval_status=EvalStatus.PASSED if score >= threshold else EvalStatus.FAILED,
        )
        for inv, score in zip(actual_invocations, scores)
    ]
    return EvaluationResult(
        overall_score=overall,
        overall_eval_status=status,
        per_invocation_results=per,
    )


def extraction_self_consistency(
    eval_metric: EvalMetric,
    actual_invocations: list[Invocation],
    expected_invocations: Optional[list[Invocation]] = None,
    conversation_scenario: Optional[ConversationScenario] = None,
) -> EvaluationResult:
    """Deterministic arithmetic checks on ``read_doc`` output (reference-free)."""
    threshold = float(getattr(eval_metric.criterion, "threshold", 1.0) or 1.0)
    scores: list[float] = []
    for inv in actual_invocations:
        bundle = _extraction_bundle_from_invocation(inv)
        if bundle is None:
            scores.append(0.0)
            continue
        scored = score_self_consistency_on_extraction(bundle)
        scores.append(float(scored.get("overall", 0.0)))
    return _result_from_scores(scores, actual_invocations, threshold)


def _classification_bundle(invocation: Invocation) -> dict[str, Any] | None:
    """Bundle for classification: use read_doc payload (has file_kind + document_kind)."""
    read_summary = _tool_response_payload(invocation, "read_doc")
    if read_summary and read_summary.get("status") == "success":
        return read_summary
    agent_bundle = _bundle_from_agent_response(invocation)
    if agent_bundle is not None:
        return agent_bundle
    return None


def extraction_classification(
    eval_metric: EvalMetric,
    actual_invocations: list[Invocation],
    expected_invocations: Optional[list[Invocation]] = None,
    conversation_scenario: Optional[ConversationScenario] = None,
) -> EvaluationResult:
    """Score whether the model identified file_kind and document_kind correctly."""
    threshold = float(getattr(eval_metric.criterion, "threshold", 1.0) or 1.0)
    expectations = _case_expectations()
    scores: list[float] = []
    for inv in actual_invocations:
        bundle = _classification_bundle(inv)
        case_id = _case_id_from_invocation(inv)
        expected = expectations.get(case_id, {})
        score = score_classification_on_extraction(
            bundle or {},
            expected_file_kind=expected.get("expected_file_kind"),
            expected_document_kind=expected.get("expected_document_kind"),
            expected_document_kinds=(
                list(expected["expected_document_kinds"])
                if expected.get("expected_document_kinds")
                else None
            ),
            expected_document_count=expected.get("expected_document_count"),
            forbid_document_kinds=(
                list(expected["forbid_document_kinds"])
                if expected.get("forbid_document_kinds")
                else None
            ),
        )
        scores.append(score)
    return _result_from_scores(scores, actual_invocations, threshold)


def score_bookable_granularity_on_extraction(
    bundle: dict[str, Any],
    *,
    expect_hierarchy_scope: bool,
    max_bookable_lines: int | None = None,
) -> float:
    """Penalize over-extraction when a case expects summary-level bookable rows only."""
    if not expect_hierarchy_scope:
        return 1.0
    if not bundle:
        return 0.0
    if bundle.get("file_kind") == "bank_statement":
        return 1.0
    cap = max_bookable_lines if max_bookable_lines is not None else _HIERARCHY_MAX_LINES
    documents = bundle.get("documents") or []
    if not documents:
        return 0.0
    for doc in documents:
        line_count = len(doc.get("lines") or [])
        if line_count > cap:
            return 0.0
    return 1.0


def extraction_bookable_granularity(
    eval_metric: EvalMetric,
    actual_invocations: list[Invocation],
    expected_invocations: Optional[list[Invocation]] = None,
    conversation_scenario: Optional[ConversationScenario] = None,
) -> EvaluationResult:
    """Fail hierarchy-scope cases when read_doc returns too many bookable lines."""
    threshold = float(getattr(eval_metric.criterion, "threshold", 1.0) or 1.0)
    expectations = _case_expectations()
    scores: list[float] = []
    for inv in actual_invocations:
        bundle = _extraction_bundle_from_invocation(inv)
        case_id = _case_id_from_invocation(inv)
        expected = expectations.get(case_id, {})
        score = score_bookable_granularity_on_extraction(
            bundle or {},
            expect_hierarchy_scope=bool(expected.get("expect_hierarchy_scope")),
            max_bookable_lines=(
                int(expected["max_bookable_lines"])
                if expected.get("max_bookable_lines") is not None
                else None
            ),
        )
        scores.append(score)
    return _result_from_scores(scores, actual_invocations, threshold)


def score_itemized_fidelity_on_extraction(
    bundle: dict[str, Any],
    *,
    expect_itemized_lines: bool,
    min_bookable_lines: int | None = None,
) -> float:
    """Fail when itemized cases return too few bookable lines (collapsed extraction)."""
    if not expect_itemized_lines:
        return 1.0
    if not bundle:
        return 0.0
    if bundle.get("file_kind") == "bank_statement":
        return 1.0
    floor = min_bookable_lines if min_bookable_lines is not None else 1
    documents = bundle.get("documents") or []
    if not documents:
        return 0.0
    for doc in documents:
        kind = str(doc.get("document_kind") or "").strip().lower()
        if kind == "statement_of_account":
            continue
        line_count = len(doc.get("lines") or [])
        if line_count < floor:
            return 0.0
    return 1.0


def extraction_itemized_fidelity(
    eval_metric: EvalMetric,
    actual_invocations: list[Invocation],
    expected_invocations: Optional[list[Invocation]] = None,
    conversation_scenario: Optional[ConversationScenario] = None,
) -> EvaluationResult:
    """Fail itemized-scope cases when read_doc returns too few bookable lines."""
    threshold = float(getattr(eval_metric.criterion, "threshold", 1.0) or 1.0)
    expectations = _case_expectations()
    scores: list[float] = []
    for inv in actual_invocations:
        bundle = _extraction_bundle_from_invocation(inv)
        case_id = _case_id_from_invocation(inv)
        expected = expectations.get(case_id, {})
        score = score_itemized_fidelity_on_extraction(
            bundle or {},
            expect_itemized_lines=bool(expected.get("expect_itemized_lines")),
            min_bookable_lines=(
                int(expected["min_bookable_lines"])
                if expected.get("min_bookable_lines") is not None
                else None
            ),
        )
        scores.append(score)
    return _result_from_scores(scores, actual_invocations, threshold)


def score_tax_bucket_fidelity_on_extraction(
    bundle: dict[str, Any],
    *,
    expect_tax_buckets: bool,
) -> float:
    """Score SR+ZR tax-bucket summary extraction when a case expects tax buckets."""
    if not expect_tax_buckets:
        return 1.0
    if not bundle:
        return 0.0
    if bundle.get("file_kind") == "bank_statement":
        return 1.0
    documents = bundle.get("documents") or []
    if not documents:
        return 0.0
    for doc in documents:
        breakdown = doc.get("tax_breakdown") or []
        if len(breakdown) < 2:
            return 0.0
        lines = doc.get("lines") or []
        if len(lines) < 2:
            return 0.0
        treatments = {
            str(line.get("tax_treatment") or "").strip()
            for line in lines
            if str(line.get("tax_treatment") or "").strip()
        }
        if len(treatments) < 2:
            return 0.0
        tax_amounts = [_as_float(line.get("tax_amount")) for line in lines]
        tax_amounts = [amount for amount in tax_amounts if amount is not None]
        if not tax_amounts:
            return 0.0
        if not any(amount > 0 for amount in tax_amounts):
            return 0.0
        if not any(amount == 0 for amount in tax_amounts):
            return 0.0
        descriptions = [str(line.get("description") or "").upper() for line in lines]
        if not any("(SR)" in desc for desc in descriptions):
            return 0.0
        if not any("(ZR)" in desc for desc in descriptions):
            return 0.0
        nets = [_as_float(line.get("net_amount")) for line in lines]
        nets = [amount for amount in nets if amount is not None]
        subtotal = _as_float(doc.get("subtotal"))
        if nets and subtotal is not None and not _money_close(sum(nets), subtotal):
            return 0.0
    return 1.0


def extraction_tax_bucket_fidelity(
    eval_metric: EvalMetric,
    actual_invocations: list[Invocation],
    expected_invocations: Optional[list[Invocation]] = None,
    conversation_scenario: Optional[ConversationScenario] = None,
) -> EvaluationResult:
    """Fail tax-bucket cases when read_doc misses SR+ZR bucket rows."""
    threshold = float(getattr(eval_metric.criterion, "threshold", 1.0) or 1.0)
    expectations = _case_expectations()
    scores: list[float] = []
    for inv in actual_invocations:
        bundle = _extraction_bundle_from_invocation(inv)
        case_id = _case_id_from_invocation(inv)
        expected = expectations.get(case_id, {})
        score = score_tax_bucket_fidelity_on_extraction(
            bundle or {},
            expect_tax_buckets=bool(expected.get("expect_tax_buckets")),
        )
        scores.append(score)
    return _result_from_scores(scores, actual_invocations, threshold)


_FAITHFULNESS_PROMPT = """You grade financial document extraction for ledger posting.

You receive:
1) The source document PDF
2) The agent's extracted JSON from read_doc

Score whether the extraction is appropriate for bookkeeping:
- Reward bookable summary rows (charge categories, tax buckets, section totals) that reconcile to header subtotal/tax/grand total.
- Penalize copying supporting detail rows (usage logs, call lists, package add-ons under an already-summarized category, appendix pages) when a higher-level summary breakdown exists on the document.
- Penalize collapsing a product or service table into one or two summary rows when each printed line is a distinct charge with qty or unit price and no higher summary section exists.
- Penalize fabricated lines, duplicated noise, or totals that do not appear on the PDF.
- Do not penalize a genuinely itemized invoice where each printed row is a distinct charge with no higher summary section.

Return ONLY valid JSON:
{
  "score": <float 0.0 to 1.0>,
  "explanation": "<one short paragraph>"
}
"""


async def extraction_faithfulness(
    eval_metric: EvalMetric,
    actual_invocations: list[Invocation],
    expected_invocations: Optional[list[Invocation]] = None,
    conversation_scenario: Optional[ConversationScenario] = None,
) -> EvaluationResult:
    """LLM judge reads the source PDF + extraction (reference-free)."""
    threshold = float(getattr(eval_metric.criterion, "threshold", 0.8) or 0.8)
    client = make_client()
    model = std_model()
    scores: list[float] = []

    for inv in actual_invocations:
        pdf_bytes = _pdf_bytes_from_user_content(inv.user_content)
        bundle = _extraction_bundle_from_invocation(inv)
        if pdf_bytes is None or bundle is None:
            scores.append(0.0)
            continue
        extraction_json = json.dumps(bundle, indent=2, default=str)
        try:
            resp = await client.aio.models.generate_content(
                model=model,
                contents=[
                    types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                    types.Part(text=_FAITHFULNESS_PROMPT),
                    types.Part(text=f"Extracted JSON:\n{extraction_json}"),
                ],
                config=default_llm_config(
                    temperature=0,
                    response_mime_type="application/json",
                ),
            )
            parsed = json.loads(resp.text or "{}")
            score = float(parsed.get("score", 0.0))
            scores.append(max(0.0, min(1.0, score)))
        except Exception:
            _log.exception("faithfulness judge failed for invocation %s", inv.invocation_id)
            scores.append(0.0)

    return _result_from_scores(scores, actual_invocations, threshold)
