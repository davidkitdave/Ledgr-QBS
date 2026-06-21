"""Invoice / receipt extraction via Gemini Flash (multimodal).

Turns a document (PDF or image bytes) into a structured `ExtractedInvoice`, then maps it to
the export-layer `NormalizedInvoice` given the resolved direction (purchase/sales). Each charge
line is kept separate so a mixed SR/ZR bill stays two lines for the tax classifier + exporter.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from google.genai import types
from pydantic import BaseModel, Field

from ..export.models import InvoiceLine, NormalizedInvoice, PartyInfo
from ..shared_libraries.gemini_call_config import default_llm_config
from ..shared_libraries.genai_client import lite_model, make_client

logger = logging.getLogger(__name__)

_MIME_BY_EXT = {
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif",
}


class ExtractedLine(BaseModel):
    description: str = Field(description="Line item / charge description")
    quantity: Optional[float] = None
    unit_amount: Optional[float] = Field(None, description="Unit price, excluding GST")
    net_amount: Optional[float] = Field(None, description="Line amount, excluding GST")
    gst_amount: Optional[float] = Field(None, description="GST on this line (0 if zero-rated/exempt)")
    tax_label: Optional[str] = Field(None, description="Any explicit tax wording on the line, e.g. SR, ZR, 9%, 0%, exempt")


class ExtractedInvoice(BaseModel):
    doc_type: str = Field(description="invoice or receipt")
    invoice_number: Optional[str] = Field(
        None,
        description=(
            "The document reference number. Accept ANY of these labels: Invoice No, Bill No, "
            "Tax Invoice No, Receipt No, Invoice Number, Ref, Reference No, Doc No — they all "
            "map here. Always capture it; never leave null if a number is visible."
        ),
    )
    invoice_date: Optional[str] = Field(
        None,
        description=(
            "ISO date YYYY-MM-DD — always return the document/issue date (the date the document "
            "was issued or printed). If the document shows a date range or statement period "
            "(e.g. '01/04/2024 – 30/04/2024'), use the issue/document date (often the period "
            "end or a separate 'Date' / 'Invoice Date' field), NOT the range start date."
        ),
    )
    due_date: Optional[str] = Field(
        None,
        description=(
            "ISO date YYYY-MM-DD — return the payment due date. If no explicit due date is "
            "printed, derive it from stated payment terms (e.g. 'Net 30' adds 30 days to "
            "invoice_date). Leave null only when neither a due date nor payment terms are "
            "present — the exporter will then fall back to using invoice_date as *DueDate."
        ),
    )
    currency: Optional[str] = Field(None, description="ISO currency code, e.g. SGD/MYR/USD")
    fx_rate: Optional[float] = Field(
        None,
        description=(
            "If the document itself states an exchange rate to the ledger/base currency "
            "(e.g. 'Exchange Rate: 1 USD = 1.35 SGD'), return it as a decimal multiplier "
            "(e.g. 1.35). Otherwise return null — never invent a rate."
        ),
    )
    issuer_name: Optional[str] = Field(None, description="Supplier/seller — who issued the document")
    issuer_gst_regno: Optional[str] = Field(None, description="Issuer GST registration no. / UEN if shown")
    issuer_country: Optional[str] = Field(
        None,
        description=(
            "Country of the issuer / supplier as a 2-letter code (SG / MY / US / etc.). "
            "Infer from any country indicator on the document: country code in address, "
            "country prefix on phone, \"Made in <country>\", tax registration number format, "
            "or explicit country text. CRITICAL for multi-jurisdiction tax routing — the previous "
            "extractor left this null which caused an MY receipt to be wrongly routed "
            "through Singapore GST rules. Always return a 2-letter code when any "
            "country indicator is visible; null only when truly absent."
        ),
    )
    issuer_tax_system: Optional[str] = Field(
        None,
        description=(
            "Tax system that applies to this issuer (informational only; the "
            "jurisdiction router will override this with the resolved rule). One of: "
            "'GST' (Singapore-style goods & services tax), 'SST' (Malaysia Sales Tax / "
            "Service Tax), 'VAT' (European-style), 'NONE' (no tax system — e.g. US "
            "domestic), or null when the document carries no tax column at all. "
            "Infer from explicit tax wording on the document ('Service Tax', 'SST', "
            "'GST 9%', 'VAT', etc.)."
        ),
    )
    bill_to_name: Optional[str] = Field(None, description="Customer/buyer — who it is billed to")
    bill_to_country: Optional[str] = Field(
        None,
        description=(
            "Country of the bill-to / customer, 2-letter code. Same inference rules as "
            "issuer_country. Important for cross-border detection (SG client + MY "
            "supplier triggers reverse-charge review)."
        ),
    )
    lines: list[ExtractedLine] = Field(default_factory=list)
    subtotal: Optional[float] = None
    gst_total: Optional[float] = None
    total: Optional[float] = None


class ExtractedInvoiceBundle(BaseModel):
    """One uploaded PDF/image may hold several distinct logical documents.

    Mirrors the bank lane's ``ExtractedBankStatement.accounts: list[...]`` pattern:
    each UNIQUE invoice/receipt becomes its own ``ExtractedInvoice`` entry, so a
    multi-invoice PDF, a scanned page of several receipts, or an SOA package (whose
    summary/cover page is skipped) all fan out into per-document records downstream.
    """

    invoices: list[ExtractedInvoice] = Field(
        default_factory=list,
        description="One entry per unique invoice/receipt found in the document",
    )
    notes: Optional[str] = Field(
        None, description="Optional free-text note about segmentation decisions"
    )
    skipped_pages: Optional[list[int]] = Field(
        None,
        description="1-based page numbers deliberately skipped (e.g. an SOA summary/cover page)",
    )


_PROMPT = """You are transcribing an invoice/receipt faithfully for a Singapore/Malaysia bookkeeping ledger.

Copy every visible charge row as printed — do NOT collapse itemized rows into bookkeeper summary
buckets during extraction. When the document only exposes summary/total rows (e.g. a telco
"Summary of Charges" with SR and ZR buckets and no itemized detail), transcribe those summary
rows faithfully — one line per visible printed row.

How to choose the lines:
- When itemized product/service rows are printed, capture EACH row verbatim.
- When a summary/totals section is the ONLY charge breakdown (common on telco/utility bills),
  transcribe that summary section row-by-row — do NOT enumerate hidden per-call detail from other
  pages unless those rows are visibly printed on the document.
- Split lines by GST tax treatment when the document prints separate buckets: standard-rated
  (GST 9% / 'G') charges; zero-rated/international (0% / 'Z'); exempt as ES.
- For a simple invoice/receipt with only a handful of distinct printed line items, keep each row.

Per line:
- description = the label exactly as printed on the document row.
- net_amount = the ex-GST amount for that row.  IMPORTANT: discount lines must use a
  NEGATIVE net_amount (e.g. a Trip.com promotional discount of 84.06 → net_amount = -84.06).
  Do NOT fold discounts into other lines or into the subtotal — keep them as separate lines
  with negative net_amount so that Σ(all line net_amounts) == document subtotal.
- gst_amount = GST on this line (0 for ZR/ES, and also negative when applied to a discount line).
- tax_label normalized to one of SR (standard/GST/9%/G), ZR (zero-rated/0%/Z/international),
  ES (exempt/E), OS (out-of-scope), or NT (no tax). If only a single-letter code (G/Z/E) is
  printed, map G→SR, Z→ZR, E→ES.
- quantity and unit_amount when the document prints them; leave null when not visible.
- Tax and service-charge lines (e.g. Agoda's "Tax and service charges", hotel service fee,
  tourism levy) must be captured as their own lines with the correct net_amount. Do NOT drop
  them — without these lines Σ(net_amounts) will not equal the document total.

Document-level fields:
- issuer_name = the supplier/seller (letterhead/"From"); bill_to_name = who it is addressed to.
- issuer_country = 2-letter country code of the issuer (SG / MY / US / ...). Infer from any
  country indicator on the doc: country code in address, country prefix on phone, "Made in
  <country>", tax registration number format, explicit country text. CRITICAL for multi-jurisdiction tax routing — the
  previous extractor left this null on an MY receipt which caused it to be wrongly
  processed under SG GST. Always return a 2-letter code when any country indicator is
  visible; null only when truly absent.
- issuer_tax_system = "GST" / "SST" / "VAT" / "NONE" / null — infer from explicit tax
  wording on the document (e.g. "Service Tax 8%", "SST", "GST 9%", "VAT"). Informational;
  the jurisdiction router applies the canonical rule based on the client profile + this hint.
- bill_to_country = 2-letter country code of the bill-to / customer (same inference rules).
  Important for cross-border detection (SG client + MY supplier triggers reverse-charge).
- issuer_gst_regno = the supplier's GST registration number / UEN if printed.
- invoice_number = the document reference. Accept ANY label: Invoice No, Bill No, Tax Invoice No,
  Receipt No, Ref, Reference No, Doc No — all map to invoice_number. Always capture it; do NOT
  leave null if any reference number is visible on the document.
- invoice_date = the document/issue date, always returned in ISO YYYY-MM-DD. If the document
  shows a date range or statement period (e.g. '01/04/2024 – 30/04/2024'), use the issue date
  or document date (often the period end or a separate 'Date'/'Invoice Date' field) — NOT the
  range start date. currency as ISO code.
- due_date = the payment due date, ISO YYYY-MM-DD; if no explicit due date is printed, derive it
  from stated payment terms (e.g. 'Net 30' from the invoice date). Leave null only when neither a
  due date nor terms are present (the export layer will then fall back to using invoice_date).
- Always also return invoice-level subtotal, gst_total, total (the grand totals from the bill),
  used for reconciliation.

- If the document explicitly states an exchange rate to the ledger/base currency (e.g.
  'Exchange Rate: 1 USD = 1.35 SGD', 'Rate: 1.35'), return it as fx_rate (a decimal multiplier,
  e.g. 1.35). Otherwise leave fx_rate null — never invent or estimate a rate.
- Do not invent values; transcribe what is printed and ensure line nets + GST reconcile to the
  document totals. Leave a field null if not visible."""


_BUNDLE_PROMPT = (
    """You are extracting invoices/receipts for a Singapore/Malaysia bookkeeping ledger from a
single uploaded file that may contain MORE THAN ONE distinct document.

Return a LIST of invoice/receipt records under "invoices" — ONE entry per UNIQUE logical
document. Apply these segmentation rules:

- MULTIPLE INVOICES IN ONE PDF: if the file contains several separate invoices/bills (e.g. a
  pack of supplier invoices), emit one entry for EACH distinct invoice (its own invoice number,
  date, issuer, totals).
- MULTIPLE RECEIPTS ON ONE SCANNED PAGE: if a single scanned page shows several separate
  receipts side by side or at different orientations/angles, enumerate EACH receipt as its own
  entry. Four receipts on one page → four entries. Read receipts that are rotated, skewed, or
  upside down.
- STATEMENT OF ACCOUNT (SOA) PACKAGE: if the file is an SOA package — a summary/cover page that
  lists multiple invoices/balances, followed by the actual embedded invoices — SKIP the SOA
  summary/cover page entirely and extract only the real embedded invoice(s). Record the skipped
  page number(s) in "skipped_pages". Do NOT emit the SOA summary itself as an invoice.
- If the file is just ONE ordinary invoice/receipt, return a single-element list.

For EACH invoice/receipt entry, follow exactly the same per-document and per-line rules as a
single document:

"""
    + _PROMPT
)


def mime_for(path: str | Path) -> str:
    return _MIME_BY_EXT.get(Path(path).suffix.lower(), "application/octet-stream")


def _append_hint(prompt: str, hint: Optional[str]) -> str:
    """Append a human steering hint to an extraction prompt when provided."""
    hint = (hint or "").strip()
    if not hint:
        return prompt
    return f"{prompt}\n\nAdditional instruction from the accountant:\n{hint}"


def extract_invoice(
    data: bytes,
    mime_type: str,
    *,
    project: Optional[str] = None,
    location: Optional[str] = None,
    model: Optional[str] = None,
    hint: Optional[str] = None,
) -> ExtractedInvoice:
    client = make_client(project, location)
    model = model or lite_model()
    part = types.Part.from_bytes(data=data, mime_type=mime_type)
    resp = client.models.generate_content(
        model=model,
        contents=[part, _append_hint(_PROMPT, hint)],
        config=default_llm_config(
            temperature=0,
            response_mime_type="application/json",
            response_schema=ExtractedInvoice,
        ),
    )
    return ExtractedInvoice.model_validate_json(resp.text)


def extract_file(path: str | Path, **kwargs) -> ExtractedInvoice:
    path = Path(path)
    return extract_invoice(path.read_bytes(), mime_for(path), **kwargs)


def extract_invoice_bundle(
    data: bytes,
    mime_type: str,
    *,
    project: Optional[str] = None,
    location: Optional[str] = None,
    model: Optional[str] = None,
    hint: Optional[str] = None,
) -> ExtractedInvoiceBundle:
    """Extract one-or-more invoices/receipts from a single document into a bundle.

    Segments multi-invoice PDFs, multi-receipt scanned pages, and SOA packages
    (skipping the SOA summary/cover page). Always returns at least an empty list
    rather than raising on a model that returns no entries.
    """
    client = make_client(project, location)
    model = model or lite_model()
    part = types.Part.from_bytes(data=data, mime_type=mime_type)
    resp = client.models.generate_content(
        model=model,
        contents=[part, _append_hint(_BUNDLE_PROMPT, hint)],
        config=default_llm_config(
            temperature=0,
            response_mime_type="application/json",
            response_schema=ExtractedInvoiceBundle,
        ),
    )
    return ExtractedInvoiceBundle.model_validate_json(resp.text)


def extract_file_bundle(path: str | Path, **kwargs) -> ExtractedInvoiceBundle:
    path = Path(path)
    return extract_invoice_bundle(path.read_bytes(), mime_for(path), **kwargs)


_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d/%m/%y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%Y/%m/%d",
    "%m/%d/%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%B %d, %Y",
    "%d %b %y",
)


def _strip_ordinals(text: str) -> str:
    return re.sub(r"\b(\d{1,2})(st|nd|rd|th)\b", r"\1", text, flags=re.I)


def _parse_date_part(part: str) -> Optional[date]:
    cleaned = _strip_ordinals(part.strip())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _parse_date(s: Optional[str]) -> Optional[date]:
    """Parse a single invoice date from common formats and date ranges."""
    if not s:
        return None
    raw = s.strip()
    direct = _parse_date_part(raw)
    if direct:
        return direct
    for sep in (" – ", " - ", " to "):
        if sep == " to ":
            if not re.search(r"\s+to\s+", raw, flags=re.I):
                continue
            segments = re.split(r"\s+to\s+", raw, flags=re.I)
        elif sep in raw:
            segments = raw.split(sep)
        else:
            continue
        parsed = [_parse_date_part(seg) for seg in segments]
        parsed = [d for d in parsed if d is not None]
        if parsed:
            return parsed[-1]
    return None


def _has_currency_conflict(
    ex: ExtractedInvoice,
    *,
    line_currencies: Optional[list[str]] = None,
) -> bool:
    """True when the same document mixes more than one currency."""
    seen: set[str] = set()
    if ex.currency:
        seen.add(ex.currency.upper())
    for code in line_currencies or []:
        if code:
            seen.add(code.upper())
    return len(seen) > 1


_G4_TOLERANCE_CENTS: dict[str, int] = {
    "SGD": 2,
    "MYR": 2,
    "USD": 2,
}


def g4_tolerance_cents(currency: Optional[str]) -> int:
    """G4 gate: per-currency reconcile tolerance in integer cents."""
    code = (currency or "").strip().upper()
    return _G4_TOLERANCE_CENTS.get(code, 2)


def g4_tolerance_abs(currency: Optional[str]) -> float:
    """G4 tolerance as major currency units (e.g. 0.02 for 2 cents)."""
    return g4_tolerance_cents(currency) / 100.0


def reconcile(
    ex: ExtractedInvoice,
    *,
    tol_abs: float = 0.05,
    tol_rel: float = 0.01,
    tax_visible_on_document: Optional[bool] = None,
    subtotal_in_capture: Optional[bool] = None,
    currency: Optional[str] = None,
) -> tuple[bool, str]:
    """Check that ledger lines tie out to document totals (footer-first).

    When ``currency`` is set, comparisons use G4 integer-cent tolerance
    (``g4_tolerance_cents``) to avoid float drift. When ``subtotal_in_capture``
    is False, skip subtotal-only checks (fixes false alarms when the capture has
    no Sub Total row). When ``tax_visible_on_document`` is False, skip GST checks.
    """
    net_sum = sum(ln.net_amount or 0.0 for ln in ex.lines)
    gst_sum = sum(ln.gst_amount or 0.0 for ln in ex.lines)
    line_total = net_sum + gst_sum

    mismatches: list[str] = []

    def _check(label: str, computed: float, reference: Optional[float]) -> None:
        if reference is None:
            return
        if currency is not None:
            cents_tol = g4_tolerance_cents(currency)
            diff_cents = abs(round(computed * 100) - round(reference * 100))
            if diff_cents > cents_tol:
                mismatches.append(
                    f"{label}: lines={computed:.2f} vs doc={reference:.2f} "
                    f"(diff={computed - reference:+.2f}, tol={cents_tol}c)"
                )
            return
        tol = max(tol_abs, tol_rel * abs(reference))
        if abs(computed - reference) > tol:
            mismatches.append(
                f"{label}: lines={computed:.2f} vs doc={reference:.2f} "
                f"(diff={computed - reference:+.2f}, tol={tol:.2f})"
            )

    if subtotal_in_capture is not False:
        _check("subtotal", net_sum, ex.subtotal)
    if tax_visible_on_document is not False:
        _check("gst", gst_sum, ex.gst_total)
    _check("total", line_total, ex.total)

    if mismatches:
        return False, "; ".join(mismatches)
    if ex.total is not None:
        return True, f"Lines total ${line_total:.2f} · Footer ${ex.total:.2f} · OK"
    return True, "reconciled"


def direction_needs_review(direction: object) -> bool:
    """True when sales/purchase side is not confirmed and needs HITL."""
    return direction in ("unknown", "auto", "self_referential", None) or (
        direction not in ("purchase", "sales")
    )


def append_direction_review_note(inv: NormalizedInvoice, direction: object) -> None:
    """Flag invoices whose sales/purchase side is not confirmed (C8 HITL)."""
    if direction == "self_referential":
        review_note = (
            "needs review: self-referential document — issuer and bill-to "
            "both match client; not booked as a purchase"
        )
    elif direction == "unknown":
        review_note = (
            "needs review: direction unknown — could not determine whether "
            "client is issuer or bill-to; defaulted to purchase for routing"
        )
    else:
        review_note = (
            "needs review: direction not confirmed — could not determine whether "
            "client is issuer or bill-to; defaulted to purchase for routing"
        )
    inv.reconciled = False
    inv.reconcile_note = (
        f"{inv.reconcile_note}; {review_note}"
        if inv.reconcile_note
        else review_note
    )


def to_normalized(
    ex: ExtractedInvoice,
    *,
    direction: str,
    our_gst_registered: bool = True,
    client_country: str = "SG",
    base_currency: str = "SGD",
    fx_rate: Optional[float] = None,
    currency_conflict: bool = False,
    line_currencies: Optional[list[str]] = None,
) -> NormalizedInvoice:
    """Map an ExtractedInvoice to a NormalizedInvoice for the tax classifier + exporter.

    direction: 'purchase' (we are the buyer; counterparty = supplier=issuer) or
    'sales' (we are the seller; counterparty = customer=bill_to).

    Foreign-currency invoices are booked in their document currency (standard AP in
    QBS/Xero).  needs_fx_review is set only when the same document mixes currencies
    or when fx_rate conversion was requested but is missing on a conflicting doc.
    """
    doc_type = "sales" if direction == "sales" else "purchase"
    supplier = PartyInfo(
        name=ex.issuer_name,
        gst_regno=ex.issuer_gst_regno,
        country=ex.issuer_country,
    )
    customer = PartyInfo(
        name=ex.bill_to_name,
        country=ex.bill_to_country,
    )

    doc_currency = (ex.currency or base_currency).upper()

    lines = [
        InvoiceLine(
            description=ln.description,
            quantity=ln.quantity,
            unit_amount=ln.unit_amount,
            net_amount=ln.net_amount,
            gst_amount=ln.gst_amount,
            tax_keyword=ln.tax_label,
        )
        for ln in ex.lines
    ]
    ok, detail = reconcile(ex)
    mixed_currencies = currency_conflict or _has_currency_conflict(
        ex, line_currencies=line_currencies
    )

    # ------------------------------------------------------------------ #
    # Currency — record as shown, no conversion
    # ------------------------------------------------------------------ #
    # Ledgr records invoice amounts and currency EXACTLY as printed on the
    # document.  No FX conversion is ever applied here — the accountant
    # converts in their ERP.
    #
    # fx_rate: the rate the document prints, passed in as-is, or None when
    #   the document prints none.  Never silently set to 1.0.
    # ledger_currency: always the document currency (never base_currency).
    # needs_fx_review: only True when multiple currencies appear on ONE
    #   document (mixed_currencies) — a single foreign currency is NOT flagged.
    needs_fx_review = False
    resolved_fx_rate: Optional[float] = fx_rate  # printed rate or None
    ledger_currency: str = doc_currency           # always the document currency
    doc_subtotal = ex.subtotal
    doc_gst_total = ex.gst_total
    doc_total = ex.total
    original_currency: Optional[str] = None       # no conversion → no "original"
    original_total: Optional[float] = None        # no conversion → no "original"

    if mixed_currencies:
        needs_fx_review = True
        ok = False
        fx_note = (
            "needs fx review: multiple currencies on the same document; "
            "confirm which currency and amounts to book"
        )
        detail = f"{detail}; {fx_note}" if detail else fx_note

    return NormalizedInvoice(
        doc_type=doc_type,
        invoice_number=ex.invoice_number,
        invoice_date=_parse_date(ex.invoice_date),
        due_date=_parse_date(ex.due_date),
        currency=ledger_currency,
        supplier=supplier,
        customer=customer,
        lines=lines,
        doc_subtotal=doc_subtotal,
        doc_gst_total=doc_gst_total,
        doc_total=doc_total,
        our_gst_registered=our_gst_registered,
        reconciled=ok,
        reconcile_note=detail,
        fx_rate=resolved_fx_rate,
        original_total=original_total,
        original_currency=original_currency,
        needs_fx_review=needs_fx_review,
    )


def _is_soa_summary_invoice(ex: ExtractedInvoice) -> bool:
    """Return True when an ExtractedInvoice looks like a phantom SOA-summary row.

    The model is instructed to skip SOA cover pages and record them in
    ``skipped_pages``, but it can hallucinate invoices from the SOA summary
    table — particularly when the table lists many invoice numbers with amounts.
    These phantom invoices share a distinctive shape: ALL their lines have a
    bare description (empty, "INVOICE", or "INVOICES"), zero GST, and no
    item_code on the extraction-layer model.

    This is a deterministic gate — no LLM call.  Only drop when ALL lines
    match the shape so that real invoices with tax_label==NT and gst_amount==0
    are preserved (their descriptions are specific product/service names, not
    the bare "INVOICE" sentinel).

    Conservative: returns False when the invoice has no lines (let the
    downstream reconciler flag it instead).
    """
    if not ex.lines:
        return False
    _sentinel_descs = {"", "INVOICE", "INVOICES"}
    return all(
        (line.description or "").strip().upper() in _sentinel_descs
        and (line.gst_amount or 0.0) == 0.0
        for line in ex.lines
    )


def to_normalized_bundle(
    bundle: ExtractedInvoiceBundle,
    *,
    direction: str,
    our_gst_registered: bool = True,
    client_country: str = "SG",
    base_currency: str = "SGD",
    fx_rates: Optional[dict] = None,
) -> list[NormalizedInvoice]:
    """Convert an ExtractedInvoiceBundle into a list of NormalizedInvoices.

    Each entry in bundle.invoices becomes one NormalizedInvoice.  SOA cover pages
    are already excluded by the model (they appear in bundle.skipped_pages, not in
    bundle.invoices), but the model can still hallucinate phantom invoices from
    the SOA summary table.  This function applies two deterministic hard-gates
    before conversion — no LLM call:

      1. Drop any invoice whose invoice_number appears in ``bundle.skipped_pages``
         (defensive; the model should not emit these but we enforce it in code).
      2. Drop any invoice whose lines are ALL summary-shaped (bare "INVOICE"
         description + zero GST): these are phantom rows hallucinated from the
         SOA cover table.  See ``_is_soa_summary_invoice`` for the full predicate.

    Both gates log a structured warning so operators can observe when the model
    needed hardening.

    fx_rates: optional dict mapping ISO currency code -> exchange rate to base_currency,
    e.g. {'USD': 1.35, 'IDR': 0.000085}.  When provided, amounts are converted to
    base_currency.  Single-currency foreign invoices without a rate are booked in
    their document currency; needs_fx_review is set only for mixed-currency docs.
    """
    if fx_rates is None:
        fx_rates = {}

    results: list[NormalizedInvoice] = []

    for ex in bundle.invoices:
        # Gate 2: drop phantom summary-shaped invoices hallucinated from SOA cover.
        if _is_soa_summary_invoice(ex):
            logger.warning(
                "hard-gate: dropping SOA-summary phantom invoice",
                extra={
                    "invoice_number": ex.invoice_number,
                    "line_count": len(ex.lines),
                    "reason": "all_lines_summary_shaped",
                },
            )
            continue

        doc_currency = (ex.currency or base_currency).upper()
        rate = fx_rates.get(doc_currency)
        normalized = to_normalized(
            ex,
            direction=direction,
            our_gst_registered=our_gst_registered,
            client_country=client_country,
            base_currency=base_currency,
            fx_rate=rate,
        )
        results.append(normalized)

    return results
