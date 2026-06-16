"""Phase 2 — map DocumentRecord capture to NormalizedInvoice(s).

Deterministic rules + client profile; no per-vendor Python branches.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

from ..export.models import InvoiceLine, NormalizedInvoice, PartyInfo
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
_TELCO_MARKERS = ("telco", "mobile pte", "telecommunications", "broadband", "m1 ", "simba")
_GST_BUCKET_RE = re.compile(
    r"GST\s*@\s*(\d+(?:\.\d+)?)\s*%\s*on\s*\$?\s*([\d,]+\.?\d*)",
    re.I,
)
_MAX_LINE_SUM_FALLBACK = 25


def slim_document_record_for_state(record: DocumentRecord) -> dict:
    """Serialize for ADK session state without per-line capture bloat.

    Telco bills (and other wide captures) keep labeled_fields/totals for Phase 2
    summary mapping; hundreds of line_items are dropped so Firestore session
    events stay under nested-entity limits.
    """
    d = record.model_dump()
    if _is_telco_bill(record) or len(record.line_items) > _MAX_LINE_SUM_FALLBACK:
        d["line_items"] = []
        d["tables"] = []
    return d


def _is_telco_bill(record: DocumentRecord) -> bool:
    """Telco/utility bill — ledger uses GST summary buckets, not per-line detail."""
    parts = [record.notes or ""]
    parts.extend(f"{f.label} {f.value}" for f in record.labeled_fields)
    parts.extend((line.description or "") for line in record.line_items[:8])
    blob = " ".join(parts).lower()
    if any(m in blob for m in _TELCO_MARKERS):
        return True
    return any(_GST_BUCKET_RE.search(f.label or "") or _GST_BUCKET_RE.search(f.value or "")
               for f in record.labeled_fields)


def _telco_ledger_lines(record: DocumentRecord) -> Optional[list[ExtractedLine]]:
    """Build SR/ZR summary lines from GST @ rate% on $net fields (Telco Provider A/Telco Provider B pattern)."""
    seen: set[tuple[float, float]] = set()
    buckets: list[tuple[float, float, float]] = []
    for f in record.labeled_fields:
        for text, gst_src in ((f.label, f.value), (f.value, f.label)):
            m = _GST_BUCKET_RE.search(text or "")
            if not m:
                continue
            rate = float(m.group(1))
            net = _parse_amount(m.group(2))
            if net is None:
                continue
            key = (rate, net)
            if key in seen:
                break
            seen.add(key)
            gst = _parse_amount(gst_src) or 0.0
            buckets.append((rate, net, gst))
            break

    if not buckets:
        return None

    lines: list[ExtractedLine] = []
    for rate, net, gst in buckets:
        if net == 0 and gst == 0:
            continue
        if rate == 0:
            lines.append(ExtractedLine(
                description="Telecommunication services - zero rated",
                net_amount=net,
                gst_amount=0.0,
                tax_label="ZR",
            ))
        else:
            lines.append(ExtractedLine(
                description=f"Telecommunication services - standard rated ({rate:g}%)",
                net_amount=net,
                gst_amount=gst,
                tax_label="SR",
            ))
    return lines or None


def _telco_current_charges(record: DocumentRecord) -> Optional[float]:
    for f in list(record.totals) + list(record.labeled_fields):
        nl = _norm_label(f.label)
        if "current charges" in nl or nl == "current charges":
            amt = _parse_amount(f.value)
            if amt is not None:
                return amt
    return None


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
    # Contractor refs like INV-2026-003; claim refs like AAI-25-040
    for f in fields:
        val = (f.value or "").strip()
        if re.match(r"^\d{2}-D\d{2}$", val, re.I):
            return val
    # Malaysian / compact labels: No. = IA-07465, CNA-00176
    for f in fields:
        val = (f.value or "").strip()
        if re.match(r"^(IA|CNA)-\d+$", val, re.I):
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


def _line_currency_iso(line) -> Optional[str]:
    if not line.currency:
        return None
    return (_symbol_to_iso(line.currency) or line.currency).upper()


def _line_to_extracted(item) -> ExtractedLine:
    net = item.net_amount
    if net is None and item.quantity is not None and item.unit_amount is not None:
        net = round(item.quantity * item.unit_amount, 2)
    return ExtractedLine(
        description=item.description,
        quantity=item.quantity,
        unit_amount=item.unit_amount,
        net_amount=net,
        gst_amount=None,
        tax_label=item.tax_label,
    )


def _apply_reimbursement_ledger(record: DocumentRecord, ex: ExtractedInvoice) -> ExtractedInvoice:
    """Book reimbursement claims in payout currency — one payable to the employee."""
    if _is_telco_bill(record):
        return ex

    doc_cur = (ex.currency or "").upper()
    currencies = _currencies_on_record(record, ex.currency)
    updates: dict = {}

    if _is_reimbursement_claim(record):
        claimant = _claimant_name(record)
        if claimant:
            updates["issuer_name"] = claimant

    payout_cur, payout_total = _payout_from_record(record)
    if _is_reimbursement_claim(record) and payout_cur and payout_total is not None:
        desc = _find_field(record.labeled_fields, "Purpose") or "Expense reimbursement"
        lines = [ExtractedLine(description=desc, quantity=1.0, net_amount=payout_total)]
        updates.update({
            "currency": payout_cur,
            "lines": lines,
            "subtotal": payout_total,
            "total": payout_total,
            "gst_total": 0.0,
        })
        return ex.model_copy(update=updates)

    if len(currencies) <= 1:
        return ex.model_copy(update=updates) if updates else ex

    payout_items = [
        item for item in record.line_items
        if doc_cur and _line_currency_iso(item) == doc_cur
    ]
    if payout_items:
        lines = [_line_to_extracted(item) for item in payout_items]
        subtotal = sum(l.net_amount or 0.0 for l in lines)
        total = ex.total if ex.total is not None else subtotal
        updates.update({"lines": lines, "subtotal": subtotal, "total": total})
    elif ex.total is not None:
        desc = "Expense reimbursement"
        for item in record.line_items:
            d = (item.description or "").lower()
            if any(k in d for k in ("total", "reimburse", "claim", "summary")):
                desc = item.description
                break
        lines = [ExtractedLine(description=desc, quantity=1.0, net_amount=ex.total)]
        updates.update({
            "lines": lines,
            "subtotal": ex.total,
            "total": ex.total,
            "gst_total": ex.gst_total or 0.0,
        })

    return ex.model_copy(update=updates) if updates else ex


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


def _record_to_extracted(record: DocumentRecord, *, mapper_version: str = "baseline") -> ExtractedInvoice:
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
    telco_summary = _telco_ledger_lines(record) if enhanced and _is_telco_bill(record) else None
    if telco_summary:
        lines = telco_summary
        subtotal = round(sum(l.net_amount or 0.0 for l in lines), 2)
        gst_total = round(sum(l.gst_amount or 0.0 for l in lines), 2)
        total = _telco_current_charges(record) or round(subtotal + gst_total, 2)
        reconcile(
            ExtractedInvoice(
                doc_type="invoice",
                lines=lines,
                subtotal=subtotal,
                gst_total=gst_total,
                total=total,
            )
        )
        logger.info(
            "telco-summary: collapsed %d capture lines to %d ledger lines (SR/ZR buckets)",
            len(record.line_items),
            len(lines),
        )
    else:
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
            line_sum = sum(l.net_amount or 0.0 for l in lines)
            if line_sum > 0:
                total = round(line_sum + (gst_total or 0.0), 2)
        if len(lines) == 1 and gst_total is not None and all(l.gst_amount is None for l in lines):
            lines[0].gst_amount = gst_total

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
) -> NormalizedInvoice:
    """Map one DocumentRecord to NormalizedInvoice using client context."""
    ex = _record_to_extracted(record, mapper_version=mapper_version)
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
            )
        )
    return results
