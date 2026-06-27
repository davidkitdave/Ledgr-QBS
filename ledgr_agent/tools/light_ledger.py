"""Light-path document read + optional rule ladder for the factory A/B spike.

Round 1 — one generic Gemini call fills :class:`LedgerRowBundle` (no rules).
Rounds 2–4 add real rulebook steps only when the spike script proves they are
needed (tax classify, COA categorize, route + QBS export).
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from google.genai import types
from pydantic import BaseModel, Field

from invoice_processing.export.categorizer import categorize_invoice
from invoice_processing.export.client_context import CoaAccount
from invoice_processing.export.exporters import QbsLedgerExporter
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice, PartyInfo
from invoice_processing.export.routing import route_document
from invoice_processing.export.tax_classifier import classify_invoice
from invoice_processing.extract.ledger_extract import ExtractedDocType
from invoice_processing.pipeline import tagged_export_rows
from invoice_processing.shared_libraries.gemini_call_config import default_llm_config
from invoice_processing.shared_libraries.genai_client import lite_model, make_client
from invoice_processing.shared_libraries.playground_context import playground_default_context

_log = logging.getLogger(__name__)

LightRound = Literal[1, 2, 3, 4]

GENERIC_PROMPT = (
    "Read this document and fill the JSON schema from what you see. "
    "Set doc_type to the kind of document. "
    "Fill only the fields that apply; leave the rest null. "
    "Set vendor_tax_id to the supplier GST/UEN/tax registration when printed. "
    "Capture every printed tax grouping in tax_lines. "
    "Fill rows with one ledger row per summary charge you would post; "
    "prefer the printed summary breakdown and skip appendix or call-detail "
    "sub-rows when a summary breakdown exists. "
    "Set tax_treatment from the document tax wording (SR, ZR, ES, OS, IM, NT). "
    "Leave account_code null — account assignment is a separate policy step. "
    "Reconcile line nets and tax to grand_total."
)


class LedgerRow(BaseModel):
    description: str | None = None
    net_amount: float | None = None
    gst_amount: float | None = None
    total_amount: float | None = None
    tax_treatment: str | None = Field(
        default=None,
        description="SR/ZR/ES/OS/IM/NT from the document tax wording",
    )
    account_code: str | None = Field(
        default=None,
        description="Best-guess ledger account code from the line nature",
    )
    tax_label: str | None = Field(
        default=None,
        description="Verbatim printed tax label, e.g. GST 9%, 0%",
    )


class LedgerTaxLine(BaseModel):
    label: str
    rate: str | None = None
    base: float | None = None
    amount: float | None = None


class LedgerDoc(BaseModel):
    doc_type: ExtractedDocType | str | None = None
    vendor: str | None = None
    vendor_tax_id: str | None = Field(
        default=None,
        description="Supplier GST/UEN/tax registration when printed on the document",
    )
    reference: str | None = None
    date: str | None = None
    currency: str | None = None
    subtotal: float | None = None
    tax_total: float | None = None
    grand_total: float | None = None
    tax_lines: list[LedgerTaxLine] = Field(default_factory=list)
    rows: list[LedgerRow] = Field(default_factory=list)


class LedgerRowBundle(BaseModel):
    documents: list[LedgerDoc] = Field(default_factory=list)
    notes: str | None = None


def _parse_date(raw: str | None) -> date | None:
    if not raw or not str(raw).strip():
        return None
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    return None


_ZR_DESC_HINTS = ("switch", "international", "roaming", "idd", "incoming trunk")


def _normalize_currency(raw: str | None) -> str:
    text = (raw or "SGD").strip()
    if text in ("$", "S$", "SG$"):
        return "SGD"
    return text or "SGD"


def _playground_coa() -> list[CoaAccount]:
    from app.coa_ingest import standard_coa_rows

    ctx = playground_default_context()
    if ctx.coa:
        return list(ctx.coa)
    return [
        CoaAccount(
            code=str(row["code"]),
            description=str(row["description"]),
            account_type=row.get("account_type"),
            financial_statement=row.get("financial_statement"),
            nature=row.get("nature"),
            keywords=row.get("keywords"),
        )
        for row in standard_coa_rows()
    ]


def _coa_code_set(coa: list[CoaAccount]) -> set[str]:
    return {str(acc.code) for acc in coa if acc.code}


def _line_looks_zr(description: str) -> bool:
    lowered = (description or "").lower()
    return any(hint in lowered for hint in _ZR_DESC_HINTS)


def _zero_rated_base(doc: LedgerDoc) -> float:
    total = 0.0
    for tax_line in doc.tax_lines:
        rate_text = f"{tax_line.rate or ''} {tax_line.label or ''}".lower()
        if "0%" in rate_text or "zero" in rate_text:
            total += float(tax_line.base or 0.0)
    return total


def _standard_gst_rate(doc: LedgerDoc) -> float | None:
    best_rate: float | None = None
    best_tax = -1.0
    for tax_line in doc.tax_lines:
        base = float(tax_line.base or 0.0)
        amount = float(tax_line.amount or 0.0)
        if base <= 0 or amount <= 0:
            continue
        rate = amount / base
        if amount > best_tax:
            best_tax = amount
            best_rate = rate
    return best_rate


def _enrich_lines_for_tax(doc: LedgerDoc, inv: NormalizedInvoice) -> None:
    """Spread doc-level tax_lines onto lines so classify_invoice can apply SR/ZR rules."""
    if doc.vendor_tax_id and str(doc.vendor_tax_id).strip():
        inv.supplier.gst_regno = str(doc.vendor_tax_id).strip()

    zr_budget = _zero_rated_base(doc)
    sr_rate = _standard_gst_rate(doc)
    tax_total = float(doc.tax_total or inv.doc_gst_total or 0.0)

    positive_lines = [ln for ln in inv.lines if (ln.net_amount or 0) > 0]
    zr_line_ids: set[int] = set()
    zr_used = 0.0

    for line in positive_lines:
        if _line_looks_zr(line.description or ""):
            zr_line_ids.add(id(line))
            zr_used += float(line.net_amount or 0.0)

    if zr_budget > zr_used + 0.02:
        candidates = sorted(
            [ln for ln in positive_lines if id(ln) not in zr_line_ids],
            key=lambda ln: float(ln.net_amount or 0.0),
        )
        for line in candidates:
            net = float(line.net_amount or 0.0)
            if zr_used + net <= zr_budget + 0.02:
                zr_line_ids.add(id(line))
                zr_used += net
            if zr_used >= zr_budget - 0.02:
                break

    sr_lines: list[InvoiceLine] = []
    for line in inv.lines:
        if id(line) in zr_line_ids:
            line.gst_amount = 0.0
            line.tax_keyword = "GST @ 0%"
            continue
        if (line.net_amount or 0) > 0:
            sr_lines.append(line)

    if not sr_lines or sr_rate is None:
        return

    gst_running = 0.0
    for idx, line in enumerate(sr_lines):
        net = float(line.net_amount or 0.0)
        if idx == len(sr_lines) - 1 and tax_total > 0:
            line.gst_amount = round(tax_total - gst_running, 2)
        else:
            gst = round(net * sr_rate, 2)
            line.gst_amount = gst
            gst_running += gst


def _strip_non_coa_account_codes(inv: NormalizedInvoice, coa_codes: set[str]) -> None:
    for line in inv.lines:
        code = (line.account_code or "").strip()
        if code and code not in coa_codes:
            line.account_code = None


def light_read(pdf_path: Path, *, model: str | None = None) -> tuple[LedgerRowBundle, dict[str, Any]]:
    """Round 1 core — one direct Gemini call, generic prompt, no rules code."""
    data = pdf_path.read_bytes()
    client = make_client()
    chosen_model = model or lite_model()
    part = types.Part.from_bytes(data=data, mime_type="application/pdf")
    t0 = time.perf_counter()
    resp = client.models.generate_content(
        model=chosen_model,
        contents=[part, GENERIC_PROMPT],
        config=default_llm_config(
            temperature=0,
            response_mime_type="application/json",
            response_schema=LedgerRowBundle,
        ),
    )
    elapsed = time.perf_counter() - t0
    bundle = LedgerRowBundle.model_validate_json(resp.text or "{}")
    meta = {
        "elapsed_seconds": round(elapsed, 2),
        "model": chosen_model,
        "bytes_sent": len(data),
        "gemini_call_count": 1,
        "usage": _usage(resp),
    }
    return bundle, meta


def _usage(resp: object) -> dict[str, Any]:
    meta = getattr(resp, "usage_metadata", None) or getattr(resp, "usage", None)
    if meta is None:
        return {}
    out: dict[str, Any] = {}
    for attr in (
        "prompt_token_count",
        "candidates_token_count",
        "thoughts_token_count",
        "total_token_count",
        "cached_content_token_count",
    ):
        val = getattr(meta, attr, None)
        if val is not None:
            out[attr] = val
    return out


def light_to_normalized(
    doc: LedgerDoc,
    *,
    our_gst_registered: bool = True,
    source_filename: str = "",
) -> NormalizedInvoice:
    """Map one light doc into the shared NormalizedInvoice contract."""
    doc_kind = (str(doc.doc_type or "invoice")).strip().lower()
    direction = "purchase"
    if doc_kind == "credit_note" and doc.vendor:
        direction = "purchase"

    lines = [
        InvoiceLine(
            description=row.description or "",
            net_amount=row.net_amount,
            gst_amount=row.gst_amount,
            tax_treatment=row.tax_treatment,
            tax_keyword=row.tax_label,
            account_code=row.account_code,
        )
        for row in doc.rows
    ]
    inv = NormalizedInvoice(
        doc_type=direction,
        invoice_number=doc.reference,
        invoice_date=_parse_date(doc.date),
        currency=_normalize_currency(doc.currency),
        supplier=PartyInfo(name=doc.vendor),
        lines=lines,
        doc_subtotal=doc.subtotal,
        doc_gst_total=doc.tax_total,
        doc_total=doc.grand_total,
        our_gst_registered=our_gst_registered,
        document_kind=doc_kind,
        tax_breakdown=[tl.model_dump(exclude_none=True) for tl in doc.tax_lines],
        tax_visible_on_document=bool(doc.tax_lines),
    )
    _enrich_lines_for_tax(doc, inv)
    return inv


def _rows_from_bundle(bundle: LedgerRowBundle) -> list[dict[str, Any]]:
    """Comparable export-style rows straight from the LLM (round 1)."""
    out: list[dict[str, Any]] = []
    for doc in bundle.documents:
        for row in doc.rows:
            out.append(
                {
                    "description": row.description,
                    "net_amount": row.net_amount,
                    "gst_amount": row.gst_amount,
                    "tax_treatment": row.tax_treatment,
                    "account_code": row.account_code,
                    "tax_label": row.tax_label,
                    "vendor": doc.vendor,
                    "reference": doc.reference,
                    "doc_type": doc.doc_type,
                    "grand_total": doc.grand_total,
                }
            )
    return out


def _apply_round_rules(
    inv: NormalizedInvoice,
    *,
    round_num: LightRound,
    pdf_path: Path,
    llm_calls: list[int],
) -> tuple[NormalizedInvoice, list[dict[str, Any]], dict[str, Any] | None]:
    """Apply ladder steps 2–4 to one normalized invoice."""
    route_info: dict[str, Any] | None = None
    export_rows: list[dict[str, Any]] = []

    if round_num >= 2:
        classify_invoice(inv)

    if round_num >= 3:
        ctx = playground_default_context()
        coa = _playground_coa()
        _strip_non_coa_account_codes(inv, _coa_code_set(coa))
        categorize_invoice(
            inv,
            coa=coa,
            category_mapping=dict(ctx.category_mapping or {}),
            entity_memory=list(ctx.entity_memory or []),
            tax_registered=bool(ctx.tax_registered),
            client_region=ctx.region or "SG",
            client_currency=inv.currency or ctx.base_currency or "SGD",
        )

    if round_num >= 4:
        doc_date = inv.invoice_date or date.today()
        route = route_document(
            doc_type=inv.document_kind or "invoice",
            direction=inv.doc_type,
            doc_date=doc_date,
            fye_month=12,
            client_id="playground",
            filename=pdf_path.name,
        )
        route_info = {
            "fy": route.fy,
            "bucket": route.bucket,
            "workbook": route.workbook,
            "sheet": route.sheet,
        }
        exporter = QbsLedgerExporter()
        purchases = [inv] if inv.doc_type == "purchase" else []
        sales = [inv] if inv.doc_type == "sales" else []
        export_rows = tagged_export_rows(exporter, route.workbook, purchases, sales)
    elif round_num >= 2:
        export_rows = [
            {
                "description": line.description,
                "net_amount": line.net_amount,
                "gst_amount": line.gst_amount,
                "tax_treatment": line.tax_treatment,
                "account_code": line.account_code,
                "vendor": inv.supplier.name,
                "reference": inv.invoice_number,
                "doc_type": inv.document_kind,
                "grand_total": inv.doc_total,
            }
            for line in inv.lines
        ]
    return inv, export_rows, route_info


def light_process(
    pdf_path: Path,
    *,
    round_num: LightRound = 1,
    model: str | None = None,
) -> dict[str, Any]:
    """Run the light path for the given ladder round."""
    llm_calls = [0]
    bundle, read_meta = light_read(pdf_path, model=model)
    llm_calls[0] = int(read_meta.get("gemini_call_count") or 1)

    if round_num == 1:
        export_rows = _rows_from_bundle(bundle)
        return {
            "round": round_num,
            "status": "ok",
            "path": str(pdf_path),
            "bundle": bundle.model_dump(),
            "export_rows": export_rows,
            "documents": [d.model_dump() for d in bundle.documents],
            "doc_count": len(bundle.documents),
            "row_count": len(export_rows),
            "tax_line_count": sum(len(d.tax_lines) for d in bundle.documents),
            "gemini_call_count": llm_calls[0],
            "elapsed_seconds": read_meta["elapsed_seconds"],
            "model": read_meta["model"],
            "usage": read_meta.get("usage") or {},
        }

    all_export_rows: list[dict[str, Any]] = []
    routes: list[dict[str, Any]] = []
    normalized_docs: list[dict[str, Any]] = []

    for doc in bundle.documents:
        inv = light_to_normalized(doc, source_filename=pdf_path.name)
        inv, export_rows, route_info = _apply_round_rules(
            inv,
            round_num=round_num,
            pdf_path=pdf_path,
            llm_calls=llm_calls,
        )
        all_export_rows.extend(export_rows)
        if route_info:
            routes.append(route_info)
        normalized_docs.append(
            {
                "document_kind": inv.document_kind,
                "direction": inv.doc_type,
                "invoice_number": inv.invoice_number,
                "line_count": len(inv.lines),
                "tax_treatments": [ln.tax_treatment for ln in inv.lines],
                "account_codes": [ln.account_code for ln in inv.lines],
            }
        )

    return {
        "round": round_num,
        "status": "ok",
        "path": str(pdf_path),
        "bundle": bundle.model_dump(),
        "export_rows": all_export_rows,
        "normalized_docs": normalized_docs,
        "routes": routes,
        "doc_count": len(bundle.documents),
        "row_count": len(all_export_rows),
        "tax_line_count": sum(len(d.tax_lines) for d in bundle.documents),
        "gemini_call_count": llm_calls[0],
        "elapsed_seconds": read_meta["elapsed_seconds"],
        "model": read_meta["model"],
        "usage": read_meta.get("usage") or {},
    }
