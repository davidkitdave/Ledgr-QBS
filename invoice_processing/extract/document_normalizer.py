"""Phase 2 — map DocumentRecord capture to NormalizedInvoice(s).

FROZEN for SOA packages only (ADR-0014). Invoice/receipt lane uses
Capture → Book → Verify; do not add new heuristics here.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

from ..export.line_grouping import apply_line_grouping_to_lines
from ..export.models import NormalizedInvoice
from .document_record import DocumentRecord, DocumentRecordBundle, LabeledField
from .record_merge import merge_document_records
from .invoice_extractor import reconcile, to_normalized, _parse_date
from .invoice_extractor import ExtractedInvoice, ExtractedLine

logger = logging.getLogger(__name__)

_INVOICE_NUMBER_LABELS = frozenset({
    "invoice number", "invoice no", "invoice no.", "invoice #", "inv #", "inv no",
    "inv no.", "bill no", "bill number", "tax invoice no", "receipt no",
    "reference", "ref", "doc no", "document number",
    "claim ref", "claim no", "claim number", "task ref", "task no", "task number", "job no",
    "no.", "no",
})
_INVOICE_NUMBER_LABEL_PATTERNS = (
    re.compile(r"^inv(?:oice)?\s*#?\s*no?\.?$", re.I),
    re.compile(r"^invoice\s*#", re.I),
)
_DATE_LABELS = frozenset({
    "invoice date", "date", "document date", "issue date", "bill date",
    "date of bill", "inv. date", "inv date",
})
_DUE_DATE_LABELS = frozenset({"due date", "payment due", "due"})
_CURRENCY_LABELS = frozenset({"currency", "curr"})
_TOTAL_LABELS = frozenset({"total", "grand total", "amount due", "total amount"})
_SUBTOTAL_LABELS = frozenset({"sub total", "subtotal", "net total"})
_GST_LABELS = frozenset({"gst", "tax", "vat", "gst total", "tax total"})
_KNOWN_ISO = frozenset({
    "SGD", "USD", "MYR", "IDR", "AUD", "NZD", "EUR", "GBP", "HKD", "JPY", "CNY", "THB", "PHP", "INR",
})
_CLAIM_REF_PATTERN = re.compile(r"\b([A-Z]{2,5}-\d{2}-\d{2,4})\b", re.I)
_MAX_LINE_SUM_FALLBACK = 25
_GST_BUCKET_RE = re.compile(
    r"GST\s*@\s*(\d+(?:\.\d+)?)\s*%\s*on\s*\$?\s*([\d,]+\.?\d*)",
    re.I,
)
_DEFAULT_TELCO_MARKERS = (
    "telco",
    "mobile pte",
    "telecommunications",
    "broadband",
    "m1 ",
    "simba",
)


def record_matches_telco_markers(
    record: DocumentRecord,
    markers: tuple[str, ...] = _DEFAULT_TELCO_MARKERS,
) -> bool:
    """True when capture text or GST bucket fields look like a telco/utility bill."""
    parts = [record.notes or ""]
    parts.extend(f"{f.label} {f.value}" for f in record.labeled_fields)
    parts.extend((line.description or "") for line in record.line_items[:8])
    blob = " ".join(parts).lower()
    if any(m in blob for m in markers):
        return True
    return any(
        _GST_BUCKET_RE.search(f.label or "") or _GST_BUCKET_RE.search(f.value or "")
        for f in record.labeled_fields
    )


def slim_document_record_for_state(record: DocumentRecord) -> dict:
    """Serialize for ADK session state without per-line capture bloat.

    Telco bills (and other wide captures) keep labeled_fields/totals for Phase 2
    summary mapping; hundreds of line_items are dropped so Firestore session
    events stay under nested-entity limits. Table ``rows`` are flattened because
    Firestore cannot store ``list[list[str]]``.
    """
    d = record.model_dump()
    if record_matches_telco_markers(record) or len(record.line_items) > _MAX_LINE_SUM_FALLBACK:
        d["line_items"] = []
    # Firestore cannot persist list[list]; line_items already hold grid rows.
    d["tables"] = []
    return d


def _is_telco_bill(record: DocumentRecord) -> bool:
    """Backward-compatible alias — telco detection for slim/reimbursement guards."""
    return record_matches_telco_markers(record)


def _norm_label(label: str) -> str:
    return re.sub(r"\s+", " ", (label or "").strip().lower()).rstrip(".")


def _find_field(fields: list[LabeledField], *candidates: str) -> Optional[str]:
    cand = {_norm_label(c) for c in candidates}
    for f in fields:
        if _norm_label(f.label) in cand:
            return f.value.strip() if f.value else None
    for f in fields:
        nl = _norm_label(f.label)
        for c in cand:
            if c in nl or nl in c:
                return f.value.strip() if f.value else None
    return None


def _find_in_sets(fields: list[LabeledField], label_set: frozenset[str]) -> Optional[str]:
    for f in fields:
        if _norm_label(f.label) in label_set:
            return f.value.strip() if f.value else None
    return None


def _parse_amount(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    # Strip currency codes/prefixes but keep digits.
    stripped = re.sub(r"^[A-Z]{3}\s*", "", text.strip(), flags=re.I)
    cleaned = re.sub(r"[^\d.\-]", "", stripped.replace(",", ""))
    try:
        return float(cleaned)
    except ValueError:
        return None


def _find_invoice_number(fields: list[LabeledField], *, enhanced: bool) -> Optional[str]:
    val = _find_in_sets(fields, _INVOICE_NUMBER_LABELS)
    if val:
        return val
    if not enhanced:
        return None
    for f in fields:
        nl = _norm_label(f.label)
        if any(p.match(nl) for p in _INVOICE_NUMBER_LABEL_PATTERNS):
            if f.value.strip():
                return f.value.strip()
        if "invoice" in nl and f.value.strip():
            if "gst" in nl and ("reg" in nl or "registration" in nl):
                continue
            return f.value.strip()
    # Contractor refs like INV-2026-003; claim refs like CL-25-040
    # (WS-6.1 — was `AAI-\d{2}-\d{3}`; the generic pattern captures any
    # "<2-4 LETTERS>-<digits>-<digits>" shape, which covers most client
    # reference formats without baking a specific vendor in.)
    for f in fields:
        val = (f.value or "").strip()
        if re.match(r"^\d{2}-D\d{2}$", val, re.I):
            return val
    # Malaysian / compact labels: e.g. "No. = <2-5 LETTERS>-<digits>"
    # (WS-6.1 — was `(IA|CNA)-\d+`; the generic pattern is the same shape
    # without hardcoding specific vendor ref formats.)
    for f in fields:
        val = (f.value or "").strip()
        if re.match(r"^[A-Za-z]{2,5}-\d+$", val):
            return val.upper()
    return None


def _claimant_name(record: DocumentRecord) -> Optional[str]:
    name = _party_by_hint(record, "employee", "claimant")
    if name:
        return name
    for f in record.labeled_fields:
        nl = _norm_label(f.label)
        if "employee" in nl or nl in ("name", "staff name", "submitted by"):
            if f.value.strip():
                return f.value.strip()
    return None


def _is_reimbursement_claim(record: DocumentRecord) -> bool:
    kind = (record.doc_kind_guess or "").lower()
    if any(k in kind for k in ("expense", "claim", "reimburse")):
        return True
    if _claimant_name(record):
        return True
    claim_labels = {
        "task ref", "task no", "task number", "claim ref", "claim no",
        "claim number", "job no",
    }
    for f in record.labeled_fields:
        combined = f"{f.label} {f.value}".lower()
        if "expense claim" in combined or "reimbursement" in combined:
            return True
        nl = _norm_label(f.label)
        if nl in claim_labels and f.value.strip():
            return True
    return False


def _apply_reimbursement_ledger(record: DocumentRecord, ex: ExtractedInvoice) -> ExtractedInvoice:
    """Pass through verbatim lines; map claimant to issuer on expense claims only."""
    if not _is_reimbursement_claim(record):
        return ex
    claimant = _claimant_name(record)
    if claimant:
        return ex.model_copy(update={"issuer_name": claimant})
    return ex


def _collect_line_currencies(record: DocumentRecord) -> list[str]:
    codes: list[str] = []
    for line in record.line_items:
        if not line.currency:
            continue
        iso = _symbol_to_iso(line.currency)
        codes.append((iso or line.currency).upper())
    return codes


def _currencies_on_record(record: DocumentRecord, doc_currency: Optional[str]) -> set[str]:
    seen: set[str] = set()
    if doc_currency:
        seen.add(doc_currency.upper())
    seen.update(_collect_line_currencies(record))
    for f in record.totals:
        iso = _symbol_to_iso(f.value or "")
        if iso:
            seen.add(iso.upper())
    for f in record.labeled_fields:
        nl = _norm_label(f.label)
        if nl in _CURRENCY_LABELS or "total" in nl or "amount" in nl:
            iso = _symbol_to_iso(f.value or "")
            if iso:
                seen.add(iso.upper())
    return seen


def _payout_from_record(record: DocumentRecord) -> tuple[Optional[str], Optional[float]]:
    """Payout currency/total for reimbursement (e.g. USD 311.79 after IDR receipts)."""
    for f in record.totals + record.labeled_fields:
        nl = _norm_label(f.label)
        if nl == "total" or nl.endswith(" total"):
            iso = _symbol_to_iso(f.value or "")
            amt = _parse_amount(f.value)
            if iso and amt is not None:
                return iso, amt
    if record.notes:
        m = re.search(r"converted to\s+([\d,.]+)\s+USD", record.notes, re.I)
        if m:
            try:
                return "USD", float(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return None, None


def _party_by_hint(record: DocumentRecord, *hints: str) -> Optional[str]:
    hint_set = {h.lower() for h in hints}
    for p in record.parties:
        if p.role_hint.lower() in hint_set and p.name:
            return p.name.strip()
    return None


def _symbol_to_iso(text: str) -> Optional[str]:
    u = text.upper()
    if "NZ$" in u or "NZD" in u:
        return "NZD"
    if "S$" in u or "SGD" in u:
        return "SGD"
    if "USD" in u or u.strip() == "$" or u.startswith("$"):
        return "USD"
    if "AUD" in u or "A$" in u:
        return "AUD"
    if u.strip() == "RM" or re.search(r"\bRM\b", u):
        return "MYR"
    m = re.search(r"\b([A-Z]{3})\b", u)
    if m and m.group(1) in _KNOWN_ISO:
        return m.group(1)
    return None


def _infer_currency(record: DocumentRecord, *, enhanced: bool = False) -> Optional[str]:
    payout_cur, _ = _payout_from_record(record)
    if payout_cur:
        return payout_cur
    direct = _find_in_sets(record.labeled_fields, _CURRENCY_LABELS)
    if direct:
        iso = _symbol_to_iso(direct)
        if iso:
            return iso
    for line in record.line_items:
        if line.currency:
            iso = _symbol_to_iso(line.currency) if enhanced else None
            return iso or line.currency.upper()
    for t in record.totals:
        if _norm_label(t.label) in _CURRENCY_LABELS or enhanced:
            iso = _symbol_to_iso(t.value or "")
            if iso:
                return iso
    if enhanced:
        for f in record.labeled_fields + record.totals:
            iso = _symbol_to_iso(f.value or "")
            if iso:
                return iso
    return None


def _lookup_fx_rate(
    doc_currency: str,
    base_currency: str,
    *,
    doc_rate: Optional[float] = None,
) -> Optional[float]:
    if doc_rate is not None:
        return doc_rate
    if doc_currency.upper() == base_currency.upper():
        return 1.0
    # Optional external lookup (P3): LEDGR_FX_API_URL + dated rate table.
    if os.environ.get("LEDGR_FX_LOOKUP", "").strip().lower() in ("1", "true", "yes"):
        logger.debug(
            "fx_lookup: no rate for %s→%s (external API not wired)",
            doc_currency,
            base_currency,
        )
    return None


def _record_to_extracted(
    record: DocumentRecord,
    *,
    mapper_version: str = "baseline",
    erp_profile: dict[str, Any] | None = None,
) -> ExtractedInvoice:
    """Bridge DocumentRecord → ExtractedInvoice for reconcile/to_normalized reuse."""
    enhanced = mapper_version == "enhanced"
    all_fields = list(record.labeled_fields) + list(record.totals)
    issuer = (
        _party_by_hint(record, "letterhead", "from_block", "sender_block")
        or _find_field(record.labeled_fields, "From", "Sender")
    )
    bill_to = (
        _party_by_hint(record, "to_block", "bill_to", "employee")
        or _find_field(record.labeled_fields, "To", "Bill To", "Bill To:")
    )
    gst_reg = _find_field(record.labeled_fields, "GST Reg", "UEN", "GST No", "Tax ID")

    subtotal = _parse_amount(_find_in_sets(all_fields, _SUBTOTAL_LABELS))
    gst_total = _parse_amount(_find_in_sets(all_fields, _GST_LABELS))
    total = _parse_amount(_find_in_sets(all_fields, _TOTAL_LABELS))
    if total is None:
        total = _parse_amount(_find_field(all_fields, "Total Amount", "Amount"))
    if total is None and enhanced:
        for f in record.totals:
            if "total" in _norm_label(f.label):
                total = _parse_amount(f.value)
                if total is not None:
                    break

    lines: list[ExtractedLine] = []
    for item in record.line_items:
        net = item.net_amount
        if net is None and item.quantity is not None and item.unit_amount is not None:
            net = round(item.quantity * item.unit_amount, 2)
        lines.append(
            ExtractedLine(
                description=item.description,
                quantity=item.quantity,
                unit_amount=item.unit_amount,
                net_amount=net,
                gst_amount=None,
                tax_label=item.tax_label,
            )
        )
    if total is None and enhanced and lines and len(lines) <= _MAX_LINE_SUM_FALLBACK:
        line_sum = sum(ln.net_amount or 0.0 for ln in lines)
        if line_sum > 0:
            total = round(line_sum + (gst_total or 0.0), 2)
    if len(lines) == 1 and gst_total is not None and all(ln.gst_amount is None for ln in lines):
        lines[0].gst_amount = gst_total

    lines = apply_line_grouping_to_lines(record, lines, erp_profile)

    fx_rate = None
    fx_text = _find_field(record.labeled_fields, "Exchange Rate", "FX Rate", "Rate")
    if fx_text:
        fx_rate = _parse_amount(fx_text)

    paid_notes = [
        a.text for a in record.annotations if a.kind == "payment_stamp" or "paid" in a.text.lower()
    ]
    notes = record.notes
    if paid_notes:
        stamp_note = "; ".join(paid_notes)
        notes = f"{notes}; payment_stamp: {stamp_note}" if notes else f"payment_stamp: {stamp_note}"

    doc_type = (record.doc_kind_guess or "invoice").lower()
    if doc_type not in ("invoice", "receipt"):
        doc_type = "invoice"

    inv_num = _find_invoice_number(record.labeled_fields, enhanced=enhanced)
    inv_date_raw = _find_in_sets(record.labeled_fields, _DATE_LABELS)
    if inv_date_raw is None and enhanced:
        inv_date_raw = _find_field(record.labeled_fields, "Date Range", "Period", "Billing Period")
    inv_date = _parse_date(inv_date_raw)
    inv_date_str = inv_date.isoformat() if inv_date else inv_date_raw
    due_raw = _find_in_sets(record.labeled_fields, _DUE_DATE_LABELS)
    due_date = _parse_date(due_raw)
    due_str = due_date.isoformat() if due_date else due_raw

    ex = ExtractedInvoice(
        doc_type=doc_type,
        invoice_number=inv_num,
        invoice_date=inv_date_str,
        due_date=due_str,
        currency=_infer_currency(record, enhanced=enhanced),
        fx_rate=fx_rate,
        issuer_name=issuer,
        issuer_gst_regno=gst_reg,
        bill_to_name=bill_to,
        lines=lines,
        subtotal=subtotal,
        gst_total=gst_total,
        total=total,
    )
    return _apply_reimbursement_ledger(record, ex)


def normalize_document_record(
    record: DocumentRecord,
    *,
    direction: str,
    our_gst_registered: bool = True,
    client_country: str = "SG",
    base_currency: str = "SGD",
    client_name: Optional[str] = None,
    client_uen: Optional[str] = None,
    mapper_version: str = "enhanced",
    erp_profile: dict[str, Any] | None = None,
) -> NormalizedInvoice:
    """Map one DocumentRecord to NormalizedInvoice using client context."""
    ex = _record_to_extracted(record, mapper_version=mapper_version, erp_profile=erp_profile)
    ok_pre, _ = reconcile(ex)
    raw_mixed = len(_currencies_on_record(record, ex.currency)) > 1
    if ok_pre:
        currency_conflict = False
        line_currencies: list[str] = []
    else:
        currency_conflict = raw_mixed
        line_currencies = _collect_line_currencies(record)
    fx_rate = _lookup_fx_rate(
        (ex.currency or base_currency).upper(),
        base_currency.upper(),
        doc_rate=ex.fx_rate,
    )
    effective_direction = direction if direction in ("purchase", "sales") else "purchase"
    inv = to_normalized(
        ex,
        direction=effective_direction,
        our_gst_registered=our_gst_registered,
        client_country=client_country,
        base_currency=base_currency,
        fx_rate=fx_rate,
        currency_conflict=currency_conflict,
        line_currencies=line_currencies,
    )
    ok, detail = reconcile(ex)
    if not inv.needs_fx_review:
        inv.reconciled = ok
    if direction == "self_referential":
        inv.reconciled = False
        note = (
            "needs review: self-referential document — issuer and bill-to "
            "both match client; not booked as a purchase"
        )
        inv.reconcile_note = f"{inv.reconcile_note}; {note}" if inv.reconcile_note else note
    elif direction == "unknown":
        inv.reconciled = False
        note = (
            "needs review: direction unknown — could not determine whether "
            "client is issuer or bill-to; defaulted to purchase for routing"
        )
        inv.reconcile_note = f"{inv.reconcile_note}; {note}" if inv.reconcile_note else note
    elif not inv.invoice_number and not inv.lines:
        inv.reconciled = False
        note = "needs review: insufficient capture — no invoice reference and no line items"
        inv.reconcile_note = f"{inv.reconcile_note}; {note}" if inv.reconcile_note else note
    return inv


def _is_soa_phantom_record(record: DocumentRecord) -> bool:
    """Drop SOA-summary phantom rows (cover table or bare INVOICE sentinel lines)."""
    kind = (record.doc_kind_guess or "").lower()
    if any(k in kind for k in ("statement of account", "soa", "debtor statement")):
        return True
    for f in record.labeled_fields:
        blob = f"{f.label} {f.value}".lower()
        if "debtor statement" in blob or "statement of account" in blob:
            return True

    if not record.line_items:
        return False

    _sentinel = {"", "INVOICE", "INVOICES"}
    if all((line.description or "").strip().upper() in _sentinel for line in record.line_items):
        return True

    # SOA cover table: rows are bare invoice refs (IA-07465) without a doc-level invoice #.
    _ref_only = re.compile(r"^[A-Z]{2,5}-\d{3,6}$")
    if _find_invoice_number(record.labeled_fields, enhanced=True) is None and len(record.line_items) >= 4:
        ref_rows = sum(
            1 for line in record.line_items
            if _ref_only.match((line.description or "").strip().upper())
        )
        if ref_rows >= 4 and ref_rows >= 0.6 * len(record.line_items):
            return True

    return False


def normalize_document_bundle(
    bundle: DocumentRecordBundle,
    *,
    direction: str,
    our_gst_registered: bool = True,
    client_country: str = "SG",
    base_currency: str = "SGD",
    client_name: Optional[str] = None,
    client_uen: Optional[str] = None,
    mapper_version: str = "enhanced",
    erp_profile: dict[str, Any] | None = None,
) -> list[NormalizedInvoice]:
    """Convert a DocumentRecordBundle into NormalizedInvoices."""
    bundle = merge_document_records(bundle)
    results: list[NormalizedInvoice] = []
    for doc in bundle.documents:
        if _is_soa_phantom_record(doc):
            logger.warning(
                "hard-gate: dropping SOA-summary phantom document record",
                extra={"line_count": len(doc.line_items), "reason": "all_lines_summary_shaped"},
            )
            continue
        results.append(
            normalize_document_record(
                doc,
                direction=direction,
                our_gst_registered=our_gst_registered,
                client_country=client_country,
                base_currency=base_currency,
                client_name=client_name,
                client_uen=client_uen,
                mapper_version=mapper_version,
                erp_profile=erp_profile,
            )
        )
    return results
