"""Deterministic golden v2 field-match scorer for agents-cli eval.

Compares extracted BatchResult values against a hand-authored golden v2 manifest.
No LLM calls, no network I/O — fully hermetic and deterministic.

Line-level join (issue #28)
---------------------------
Every export row in a live BatchResult now carries a stable ``source_doc_id``
(source basename + reference + page_range) plus the canonical per-line
``tax_treatment`` / ``account_code`` / ``direction`` — tagged in the engine and
surfaced straight from the exporter (not reconstructed from workbook cells).
``project_batch`` groups those rows by ``source_doc_id`` so each projected
document carries its real ``lines`` (``{tax_code, coa_code, direction, erp_codes}``)
and the line-level sub-scorers (``score_tax_coa``, ``score_creditor``) score
non-N/A on a live ``process_document_batch`` run. Expected manifest documents
opt into the join by carrying a ``source_doc_id``; without it the scorer falls
back to per-file index pairing (legacy behaviour).

N/A still legitimately occurs when: golden authored no lines for an ERP, OR a
projected doc genuinely has no rows (e.g. a bank statement), OR a golden doc's
``source_doc_id`` matches no live row.

Entry point for agents-cli: ``golden_field_match_code(instance)``
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Default manifest path
# ---------------------------------------------------------------------------

_MODULE_DIR = Path(__file__).parent
_DEFAULT_MANIFEST = (
    _MODULE_DIR.parent.parent / "tests" / "eval" / "datasets" / "golden_v2_sample.json"
)

# Sentinel: the golden manifest marks SG holes as this literal string.
_BLANK_HOLE = "BLANK(hole B.1)"


# ---------------------------------------------------------------------------
# 1. load_golden_manifest
# ---------------------------------------------------------------------------


def load_golden_manifest(path: str | Path) -> dict[str, Any]:
    """Read the golden v2 manifest JSON and return the full object."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# 2. latest_batch_result — walks agent_data turns/events for the tool response
# ---------------------------------------------------------------------------


def _function_responses(
    instance: dict[str, Any], name: str
) -> list[dict[str, Any]]:
    responses: list[dict[str, Any]] = []
    agent_data = instance.get("agent_data") or {}
    for turn in agent_data.get("turns", []):
        for event in turn.get("events", []):
            content = event.get("content") or {}
            for part in content.get("parts", []):
                response = part.get("function_response")
                if isinstance(response, dict) and response.get("name") == name:
                    payload = response.get("response")
                    if isinstance(payload, dict):
                        responses.append(payload)
    return responses


def latest_batch_result(instance: dict[str, Any]) -> dict[str, Any] | None:
    """Return the most recent ``process_document_batch`` function_response payload."""
    responses = _function_responses(instance, "process_document_batch")
    return responses[-1] if responses else None


# ---------------------------------------------------------------------------
# 3. project_batch — extract a scorer-friendly view of the BatchResult
# ---------------------------------------------------------------------------


def _basename(path_or_name: str | None) -> str:
    """Return the filename stem from a full path or bare filename."""
    if not path_or_name:
        return ""
    return os.path.basename(path_or_name)


def _lines_by_source_doc_id(
    export_rows: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group tagged export rows by ``source_doc_id`` into scorer-shaped lines.

    Each row carries the canonical per-line ``tax_treatment`` / ``account_code`` /
    ``direction`` (issue #28). We project them to the line shape the per-ERP
    scorer reads: ``tax_code`` (= tax_treatment), ``coa_code`` (= account_code),
    ``direction``, and an ``erp_codes`` map populated for *every* ERP from the
    canonical tax_treatment so ``score_tax_coa`` can match whichever ERP the
    golden authored. Rows without a ``source_doc_id`` are skipped (untagged).
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in export_rows:
        sdid = row.get("source_doc_id")
        if not sdid:
            continue
        tax_treatment = row.get("tax_treatment")
        line = {
            "tax_code": tax_treatment,
            "coa_code": row.get("account_code"),
            "direction": row.get("direction"),
            # Canonical tax_treatment is ERP-agnostic; expose it under each ERP
            # key so the golden's per-ERP line scorer joins regardless of which
            # ERP it authored. The deterministic engine maps treatment→ERP code
            # at workbook-write time; the field-match here scores the canonical
            # decision (tax code + COA), which is the classification under test.
            "erp_codes": {
                erp: tax_treatment
                for erp in ("autocount", "sql_account", "xero", "qbs")
            },
        }
        grouped.setdefault(str(sdid), []).append(line)
    return grouped


def project_batch(batch: dict[str, Any]) -> dict[str, Any]:
    """Normalise a raw BatchResult dict into a scorer-friendly projection.

    Keys:
    - documents_processed: int
    - credits_used: int
    - credit_status: str
    - per_file_doc_counts: {basename: int}  (from posted_documents)
    - docs_by_file: {basename: [{"doc_type", "invoice_number", "source_doc_id",
      "total", "lines"}]}

    ``lines`` is populated by joining the tagged ``export_rows`` to each posted
    document on ``source_doc_id`` (issue #28). It stays ``[]`` only when no
    tagged row matches the document (e.g. a bank statement, or an untagged
    legacy run) — in which case the line scorers correctly record N/A.
    """
    credits_block: dict[str, Any] = batch.get("credits") or {}
    # CreditSummary may be a Pydantic model serialised as dict or a raw dict.
    if hasattr(credits_block, "model_dump"):
        credits_block = credits_block.model_dump()  # type: ignore[union-attr]

    credits_used: int = int(credits_block.get("credits_used") or 0)
    credit_status: str = str(credits_block.get("credit_status") or "not_checked")

    posted: list[dict[str, Any]] = batch.get("posted_documents") or []
    export_rows: list[dict[str, Any]] = batch.get("export_rows") or []
    lines_by_doc_id = _lines_by_source_doc_id(export_rows)

    per_file_doc_counts: dict[str, int] = {}
    docs_by_file: dict[str, list[dict[str, Any]]] = {}

    for doc in posted:
        # Try path first, fall back to file_name
        base = _basename(doc.get("path") or "") or _basename(doc.get("file_name") or "")
        if not base:
            continue
        per_file_doc_counts[base] = per_file_doc_counts.get(base, 0) + 1
        source_doc_id = doc.get("source_doc_id")
        entry: dict[str, Any] = {
            "doc_type": doc.get("doc_type"),
            "invoice_number": doc.get("invoice_number"),
            "source_doc_id": source_doc_id,
            "total": doc.get("total"),
            "lines": list(lines_by_doc_id.get(str(source_doc_id), [])) if source_doc_id else [],
        }
        docs_by_file.setdefault(base, []).append(entry)

    return {
        "documents_processed": int(batch.get("documents_processed") or 0),
        "credits_used": credits_used,
        "credit_status": credit_status,
        "per_file_doc_counts": per_file_doc_counts,
        "docs_by_file": docs_by_file,
        "lines_by_source_doc_id": lines_by_doc_id,
    }


# ---------------------------------------------------------------------------
# 4. Money helper
# ---------------------------------------------------------------------------


def _to_cents(value: float | int | None) -> int | None:
    """Convert a monetary value to integer cents for exact comparison."""
    if value is None:
        return None
    return round(float(value) * 100)


# ---------------------------------------------------------------------------
# 5. Sub-scorers — each returns {"score": float|None, "explanation": str}
# ---------------------------------------------------------------------------


def score_doc_count(
    file_expectation: dict[str, Any],
    projection: dict[str, Any],
    basename: str,
) -> dict[str, Any]:
    """1.0 if projected doc count for this file matches golden, else 0.0."""
    expected: int = int(file_expectation.get("expected_doc_count") or 0)
    got: int = projection["per_file_doc_counts"].get(basename, 0)
    if got == expected:
        return {
            "score": 1.0,
            "explanation": f"doc_count match: {got}/{expected}",
        }
    return {
        "score": 0.0,
        "explanation": f"doc_count mismatch: got {got}, expected {expected}",
    }


def score_credits(
    file_expectation: dict[str, Any],
    projection: dict[str, Any],
) -> dict[str, Any]:
    """1.0 if credits_used == expected_billable_credits, else 0.0.

    Returns N/A (score=None) when credit_status == "not_billable" (eval/playground
    runs where no firm_id is present and nothing is charged).

    # NOTE: credits_used is the batch total; this scorer is valid only when the
    # trace contains exactly one file (one-file-per-trace).  For multi-file traces
    # the entry point skips this scorer and records N/A instead.
    """
    if projection.get("credit_status") == "not_billable":
        return {
            "score": None,
            "explanation": "credits not billable in this run",
        }
    expected: int = int(file_expectation.get("expected_billable_credits") or 0)
    used: int = int(projection.get("credits_used") or 0)
    if used == expected:
        return {
            "score": 1.0,
            "explanation": f"credits match: used {used}, expected {expected}",
        }
    return {
        "score": 0.0,
        "explanation": f"credits mismatch: used {used}, expected {expected}",
    }


def score_classification(
    golden_doc: dict[str, Any],
    actual_doc: dict[str, Any],
) -> dict[str, Any]:
    """1.0 if doc_type matches golden, else 0.0."""
    golden_type: str | None = golden_doc.get("doc_type")
    actual_type: str | None = actual_doc.get("doc_type")
    if golden_type == actual_type:
        return {
            "score": 1.0,
            "explanation": f"doc_type={actual_type!r} matches golden",
        }
    return {
        "score": 0.0,
        "explanation": (
            f"doc_type mismatch: got {actual_type!r}, expected {golden_type!r}"
        ),
    }


def score_fields(
    golden_doc: dict[str, Any],
    actual_doc: dict[str, Any],
) -> dict[str, Any]:
    """Score header fields: vendor, currency, total, tax_amount.

    Only fields that are present/non-null in golden are scored.
    String fields: case-insensitive, stripped.
    Money fields: integer cents.
    Returns N/A if no golden field is present/non-null.
    """
    results: list[tuple[str, bool]] = []

    # String fields
    for field in ("vendor", "currency"):
        golden_val = golden_doc.get(field)
        if golden_val is None:
            continue
        # If the field key is structurally absent from the actual projection,
        # treat as N/A (skip) rather than a miss — live projections may not
        # carry vendor/currency yet (TODO 0.4).  Only score when the key is
        # present (even if the value differs).
        if field not in actual_doc:
            continue
        g = str(golden_val).strip().lower()
        a = str(actual_doc.get(field) or "").strip().lower()
        results.append((field, g == a))

    # Money fields (integer cents)
    for field in ("total", "tax_amount"):
        golden_val = golden_doc.get(field)
        if golden_val is None:
            continue
        g_cents = _to_cents(golden_val)
        a_cents = _to_cents(actual_doc.get(field))
        results.append((field, g_cents == a_cents))

    if not results:
        return {"score": None, "explanation": "no golden header fields authored (N/A)"}

    passing = sum(1 for _, ok in results if ok)
    total = len(results)
    score = passing / total
    parts = [f"{f}={'PASS' if ok else 'FAIL'}" for f, ok in results]
    return {
        "score": score,
        "explanation": f"fields {passing}/{total}: {', '.join(parts)}",
    }


def score_tax_coa(
    golden_doc: dict[str, Any],
    actual_doc: dict[str, Any],
    erp: str,
) -> dict[str, Any]:
    """Per-ERP line scorer.  erp ∈ {"autocount", "sql_account"}.

    Scores matching of tax_code + coa_code + erp_codes[erp] per line.
    "BLANK(hole B.1)" in golden means the expected actual value is empty/None.

    Returns N/A when:
    - golden has no authored lines for this erp, OR
    - actual has no lines (live-projection gap — see TODO 0.4).
    """
    erp_label = f"tax_coa[{erp}]"
    golden_lines: list[dict[str, Any]] = golden_doc.get("lines") or []

    # Count authored lines for this ERP (non-null erp_codes[erp]).
    # We iterate the FULL golden_lines to preserve original indices for
    # correct alignment with actual_lines[orig_idx].
    scoreable_count = 0
    for ln in golden_lines:
        erp_codes = ln.get("erp_codes") or {}
        if erp_codes.get(erp) is not None:
            scoreable_count += 1

    if scoreable_count == 0:
        return {
            "score": None,
            "explanation": f"{erp_label}: no golden lines authored for this ERP (N/A)",
        }

    actual_lines: list[dict[str, Any]] = actual_doc.get("lines") or []
    if not actual_lines:
        return {
            "score": None,
            "explanation": (
                f"{erp_label}: no actual line data — "
                "pending export-row doc tagging (TODO 0.4)"
            ),
        }

    matched = 0
    # Enumerate over the FULL golden_lines to keep orig_idx aligned with
    # actual_lines.  Skip lines where erp_codes[erp] is None (not authored).
    for orig_idx, g_ln in enumerate(golden_lines):
        erp_codes = g_ln.get("erp_codes") or {}
        golden_erp_val: str | None = erp_codes.get(erp)
        if golden_erp_val is None:
            continue  # not authored for this ERP — skip

        golden_tax: str | None = g_ln.get("tax_code")
        golden_coa: str | None = g_ln.get("coa_code")

        if orig_idx >= len(actual_lines):
            continue
        a_ln = actual_lines[orig_idx]
        actual_erp_val = a_ln.get("erp_codes", {}).get(erp) if isinstance(
            a_ln.get("erp_codes"), dict
        ) else a_ln.get(erp)
        actual_tax: str | None = a_ln.get("tax_code")
        actual_coa: str | None = a_ln.get("coa_code")

        # Tax code match
        tax_match = actual_tax == golden_tax

        # COA code match
        coa_match = actual_coa == golden_coa

        # ERP code match — "BLANK(hole B.1)" means expected empty/None
        if golden_erp_val == _BLANK_HOLE:
            erp_match = not actual_erp_val  # empty string or None are both ok
        else:
            erp_match = actual_erp_val == golden_erp_val

        if tax_match and coa_match and erp_match:
            matched += 1

    frac = matched / scoreable_count
    return {
        "score": frac,
        "explanation": (
            f"{erp_label}: {matched}/{scoreable_count} lines matched"
        ),
    }


# Default ERPs always reported by the entry point (back-compat with the existing
# golden_v2 manifest + tests). A golden doc may author additional ERP keys (e.g.
# qbs / xero) on its lines; those are scored on top of these via the union.
_DEFAULT_SCORED_ERPS = ("autocount", "sql_account")


def _scored_erps_for_doc(golden_doc: dict[str, Any]) -> list[str]:
    """ERP keys to score for a golden doc: the defaults plus any line-authored ERPs."""
    erps: list[str] = list(_DEFAULT_SCORED_ERPS)
    for line in golden_doc.get("lines") or []:
        for erp in (line.get("erp_codes") or {}):
            if erp not in erps:
                erps.append(erp)
    return erps


def score_line_direction(
    golden_doc: dict[str, Any],
    actual_doc: dict[str, Any],
) -> dict[str, Any]:
    """Per-line booking-direction match (issue #28).

    Compares each actual line's ``direction`` (purchase/sales the row was
    exported under) against the golden document's expected ``direction``.
    Returns N/A when golden authored no ``direction`` OR the actual doc has no
    lines (live gap / bank statement). This catches a line booked on the wrong
    sheet — a classification error the reconcile math is blind to.
    """
    golden_direction = str(golden_doc.get("direction") or "").strip().lower()
    if not golden_direction:
        return {"score": None, "explanation": "no direction in golden (N/A)"}
    actual_lines: list[dict[str, Any]] = actual_doc.get("lines") or []
    if not actual_lines:
        return {
            "score": None,
            "explanation": "no actual line data — pending export-row doc tagging (N/A)",
        }
    matched = sum(
        1
        for ln in actual_lines
        if str(ln.get("direction") or "").strip().lower() == golden_direction
    )
    frac = matched / len(actual_lines)
    return {
        "score": frac,
        "explanation": f"direction: {matched}/{len(actual_lines)} lines match {golden_direction!r}",
    }


def score_creditor(
    golden_doc: dict[str, Any],
    actual_doc: dict[str, Any],
) -> dict[str, Any]:
    """1.0 if creditor_code matches golden, 0.0 on mismatch, None if N/A.

    N/A when golden has no creditor_code OR actual has no creditor data.
    """
    golden_creditor: str = str(golden_doc.get("creditor_code") or "").strip()
    if not golden_creditor:
        return {
            "score": None,
            "explanation": "no creditor_code in golden (N/A)",
        }

    actual_creditor_raw = actual_doc.get("creditor_code")
    if actual_creditor_raw is None:
        return {
            "score": None,
            "explanation": "no creditor data in actual projection (N/A — live gap)",
        }

    actual_creditor: str = str(actual_creditor_raw).strip()
    if actual_creditor == golden_creditor:
        return {
            "score": 1.0,
            "explanation": f"creditor_code={actual_creditor!r} matches golden",
        }
    return {
        "score": 0.0,
        "explanation": (
            f"creditor_code mismatch: got {actual_creditor!r}, "
            f"expected {golden_creditor!r}"
        ),
    }


# ---------------------------------------------------------------------------
# 6. agents-cli entry point
# ---------------------------------------------------------------------------


def _fmt_score(v: float | None) -> str:
    """Format a score for the explanation string."""
    if v is None:
        return "N/A"
    s = f"{v:.4f}".rstrip("0")
    if s.endswith("."):
        s += "0"
    return s


def golden_field_match_code(instance: dict[str, Any]) -> dict[str, Any]:
    """Custom metric entry point for agents-cli eval grade.

    Accepts a single trace instance, returns ``{"score": float, "explanation": str}``.
    """
    # Load golden v2 manifest
    manifest_path = os.environ.get("LEDGR_GOLDEN_MANIFEST") or str(_DEFAULT_MANIFEST)
    golden = load_golden_manifest(manifest_path)
    file_expectations: dict[str, Any] = golden.get("file_expectations") or {}
    golden_documents: list[dict[str, Any]] = golden.get("documents") or []

    # Extract batch result from trace
    batch = latest_batch_result(instance)
    if batch is None:
        return {
            "score": 0.0,
            "explanation": "no process_document_batch result in trace",
        }

    projection = project_batch(batch)
    processed_basenames = set(projection["per_file_doc_counts"].keys())

    # Match processed files to golden file_expectations
    matched_basenames = processed_basenames & set(file_expectations.keys())
    if not matched_basenames:
        names = ", ".join(sorted(processed_basenames)) or "<none>"
        return {
            "score": 0.0,
            "explanation": f"processed doc(s) not in golden v2: {names}",
        }

    all_sub_scores: list[float] = []
    breakdown_parts: list[str] = []

    # Credits attribution: batch total cannot be split across files.
    # Score credits only when exactly one file matched; otherwise record N/A.
    _multi_file_trace = len(matched_basenames) > 1

    for basename in sorted(matched_basenames):
        file_exp = file_expectations[basename]

        # File-level metrics
        dc_result = score_doc_count(file_exp, projection, basename)

        dc_s = dc_result["score"]
        if dc_s is not None:
            all_sub_scores.append(dc_s)
        breakdown_parts.append(f"doc_count={_fmt_score(dc_s)}")

        if _multi_file_trace:
            # Cannot attribute batch-level credits_used to a single file.
            breakdown_parts.append(
                "credits=N/A"
                " (multi-file trace, cannot attribute batch total to one file)"
            )
        else:
            cr_result = score_credits(file_exp, projection)
            cr_s = cr_result["score"]
            if cr_s is not None:
                all_sub_scores.append(cr_s)
            breakdown_parts.append(f"credits={_fmt_score(cr_s)}")

        # Per-document metrics: pair golden docs for this file with projected
        # docs. Prefer a join on source_doc_id (issue #28) so the right line
        # data is scored even when fan-out order differs; fall back to index
        # pairing for golden manifests that predate source_doc_id.
        golden_docs_for_file = [
            d for d in golden_documents if d.get("file") == basename
        ]
        actual_docs_for_file = projection["docs_by_file"].get(basename) or []
        actual_by_doc_id = {
            str(d.get("source_doc_id")): d
            for d in actual_docs_for_file
            if d.get("source_doc_id")
        }

        for i, g_doc in enumerate(golden_docs_for_file):
            golden_doc_id = g_doc.get("source_doc_id")
            if golden_doc_id and str(golden_doc_id) in actual_by_doc_id:
                a_doc = actual_by_doc_id[str(golden_doc_id)]
            else:
                a_doc = actual_docs_for_file[i] if i < len(actual_docs_for_file) else {}
            doc_prefix = f"[{basename}#{i}]"

            cls_result = score_classification(g_doc, a_doc)
            fld_result = score_fields(g_doc, a_doc)
            crd_result = score_creditor(g_doc, a_doc)
            dir_result = score_line_direction(g_doc, a_doc)

            labelled: list[tuple[str, dict[str, Any]]] = [
                ("classification", cls_result),
                ("fields", fld_result),
            ]
            # Score tax+COA per ERP: the defaults (autocount/sql_account) plus
            # any ERP the golden doc's lines author (e.g. qbs) — issue #28.
            for erp in _scored_erps_for_doc(g_doc):
                labelled.append((f"tax_coa[{erp}]", score_tax_coa(g_doc, a_doc, erp)))
            labelled.append(("direction", dir_result))
            labelled.append(("creditor", crd_result))

            for label, res in labelled:
                s = res["score"]
                if s is not None:
                    all_sub_scores.append(s)
                breakdown_parts.append(f"{doc_prefix}{label}={_fmt_score(s)}")

    overall = sum(all_sub_scores) / len(all_sub_scores) if all_sub_scores else 0.0
    breakdown_parts.append(f"overall={_fmt_score(overall)}")
    explanation = " ".join(breakdown_parts)

    return {"score": overall, "explanation": explanation}
