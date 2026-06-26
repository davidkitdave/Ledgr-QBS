"""Faithful multi-document extraction — one structured Gemini call.

Production default (ADR-0011, ADR-0025): a single multimodal ``generate_content``
with the PDF ``Part`` plus a Pydantic ``response_schema`` matching
:class:`ExtractedDocumentBundle` (one entry per logical document in the upload).
Matches Google's recommended pattern for invoice extraction.

Retired from live routing (Phase F): ``LEDGR_CAPTURE_BOOK`` and ``LEDGR_LEGACY_SOA``
env flags are ignored by ``process_invoice_document`` (warn-only if set).

The Understand call owns the ``direction_for_client`` decision per document:
one multimodal call resolves the sales-vs-purchase direction by comparing the
document's From/To parties against the client identity passed into the prompt,
replacing the legacy two-step ``classify_document`` + ``resolve_direction`` plumbing.
"""

from __future__ import annotations

import logging
import os
from typing import Literal, Optional

from google.genai import types
from pydantic import BaseModel, Field

from ..export.models import NormalizedInvoice
from ..shared_libraries.context_cache_config import log_context_cache_usage
from ..shared_libraries.gemini_call_config import default_llm_config
from ..shared_libraries.genai_client import lite_model, make_client
from .invoice_extractor import (
    ExtractedInvoice,
    ExtractedLine,
    _append_hint,
    append_direction_review_note,
    direction_needs_review,
    mime_for,
    reconcile,
    to_normalized,
)

logger = logging.getLogger(__name__)


def _build_understand_prompt(client_name: Optional[str], client_uen: Optional[str]) -> str:
    """Return the Understand prompt with client context appended for direction.

    Prompt is principle-based (no Python-style rule branching) per ADR-0015.
    Schema ``description=`` strings carry the per-field rules; the prompt
    body teaches the model how to read the document and decide direction from
    visible signals. All party names in the appended exemplars are
    placeholders (Company-A / Company-B / Person-1) — never real client or
    vendor names. Direction classification is decided in one multimodal call
    by comparing the document's From / To parties against the client; the
    legacy ``resolve_direction()`` fuzzy-match pass is kept for bank / SOA
    only.
    """
    base = """You are an SG bookkeeper reading a single document for a known client.

Decide direction by who pays whom in the client's books, using whichever of
these visible signals is present: From / Bill-To / Sender / Recipient blocks,
header letterhead vs claimant signature, the words "Claim",
"Reimbursement", "Invoice", "Receipt", "Credit Note". The company on a
letterhead is not automatically the issuer; on a reimbursement form the
claimant signing the form is the supplier. If the client identity is not on
the document, return "unknown".

Produce:
- A Drive-style summary_table (Category / Details pairs) for human review.
- ledger_lines transcribed verbatim from visible charge rows — do NOT collapse itemized rows
  into bookkeeper buckets during extraction.
- doc_kind classifying the document shape (invoice / receipt /
  expense_claim / credit_note / other).
- claimant_name when doc_kind == expense_claim (the person signing the form;
  the company letterhead is the approver, not the issuer).
- tax_visible_on_document strictly per the schema description (true only
  when a literal "GST" / "Tax" / "VAT" row, column, or percentage appears).
- from_party.country / to_party.country: 2-letter country code (SG / MY /
  US / ...) inferred from any country indicator on the document (address
  block, country prefix on phone, "Made in <country>", tax-reg-no country
  prefix, explicit country text). CRITICAL for multi-jurisdiction tax
  routing — a past MY receipt was wrongly processed under SG GST because
  this was null. Always populate when ANY country indicator is visible;
  null only when truly absent.
- tax_system_hint: "GST" / "SST" / "VAT" / "NONE" / null inferred from
  explicit tax wording on the document (e.g. "Service Tax 8%" -> "SST",
  "GST 9%" -> "GST", "VAT 20%" -> "VAT"). Informational; the jurisdiction
  router applies the canonical rule based on the client profile + this hint.

Line granularity (faithful transcription — M1):
- Itemized invoice/receipt: one ledger line per visible printed row.
- Telco/utility bill whose ONLY charge breakdown is a summary section: transcribe those
  summary rows as printed (often SR + ZR buckets) — do NOT emit per-phone/per-call detail
  from appendix pages unless those rows are visibly printed.
- Simple single-total receipt: one line when only one charge row exists.

Arithmetic:
- document_reference is the invoice or bill number — NOT a GST registration
  number.
- document_date as ISO yyyy-mm-dd.
- Ensure ledger line nets + GST reconcile to document_total.
- from_party: the entity that ISSUED or SENT the document (the seller /
  vendor / billing party). Capture their UEN / tax reg no if visible.
- to_party: the entity the document is ADDRESSED or BILLED to (the buyer /
  recipient / bill-to party). Capture their UEN / tax reg no if visible.
- tax_visible_on_document: true ONLY when the document shows a Tax / GST
  column or a tax / GST amount. When false, set per-line gst_amount = 0 and
  gst_total = null — the export layer must NEVER invent tax.
- currency: read it from the document, not the client. Primary: the document's
  dedicated Currency column when present (if every row agrees, use that code).
  Secondary: the total footer (e.g. "TOTAL USD $1,195.11" -> USD). Foreign
  codes embedded in Details text (e.g. "BHT 4466.68 x 0.0295" -- a per-line FX
  conversion note) are NOT the document currency when a Currency column says
  otherwise. Never infer from letterhead, the SG address, or base_currency.
  When every line and the footer agree on USD, currency is USD even if the
  client books in SGD.

Examples (synthetic -- placeholders, no real names):

[Doc 1] Header "<Company-A>" letterhead. Body title: "EXPENSE CLAIM".
Claimant field: "<Person-1>". Task reference: "CL-XX-NNN". Item rows
("Transport", "Accommodation", ...) with a "Currency" and "Amount" column.
Footer "Approved by" line for finance; "Total USD <amount>".
- doc_kind: expense_claim
- direction_for_client (when client = Company-A): purchase
- claimant_name: <Person-1>
- tax_visible_on_document: false
- per-line gst_amount: 0
- currency: USD
- direction_reason: claimant signed the form; client is the approver

[Doc 2] Header "<Company-A>" letterhead. Body title: "TAX INVOICE".
"Bill To: <Company-B>". Item rows with a "GST 9%" column and a numeric
"GST" amount.
- doc_kind: invoice
- direction_for_client (when client = Company-A): sales
- direction_for_client (when client = Company-B): purchase
- tax_visible_on_document: true
- per-line gst_amount: net × 9%
- currency: SGD
- direction_reason: client matches the issuer / Bill-To respectively

[Doc 3] Header "<Company-A>" letterhead. Body title: "EXPENSE CLAIM".
Columns: Category | Dates | Task Card # | Details | Currency | Amount.
No "GST", no "Tax", no percentage anywhere. Footer: "Total USD <amount>".
- doc_kind: expense_claim
- direction_for_client (when client = Company-A): purchase
- tax_visible_on_document: false
- per-line gst_amount: 0
- currency: USD
- direction_reason: claimant signed the form; no tax column on document"""

    if client_name or client_uen:
        bits: list[str] = []
        if client_name:
            bits.append(f"name = {client_name}")
        if client_uen:
            bits.append(f"UEN = {client_uen}")
        ctx = " · ".join(bits)
        base += f"""

Client context: {ctx}.
Match the client by name OR UEN against the document's From / To blocks and
the letterhead / claimant. Visual layout (header block, "Bill To", "From",
signature block) takes precedence over party-name string matching. Return
"unknown" rather than guessing when the client does not appear on the
document."""

    return base


# Backward-compatible alias used by tests / older imports that pre-date the
# client-context injection. Prefer ``_build_understand_prompt`` directly.
UNDERSTAND_PROMPT = _build_understand_prompt(None, None)


#: Direction labels the Understand call can return. ``"unknown"`` triggers
#: a HITL gate — never a fuzzy Python rewrite.
DirectionForClient = Literal["purchase", "sales", "self_referential", "unknown"]


# WS-2.1 — faithful multi-document schema (ADR-0025, spec §1).
ExtractedDocType = Literal[
    "invoice", "receipt", "statement", "credit_note", "expense_claim", "other"
]
LinePresentation = Literal["itemized", "summary"]


class ExtractedTaxLine(BaseModel):
    """One printed tax grouping row — N lines allowed, not forced to two."""

    label: str = Field(description="Verbatim tax label as printed, e.g. GST 9%, SST 8%")
    rate: Optional[str] = Field(
        default=None,
        description="Verbatim rate text when shown, e.g. 9%, 8%, 0%",
    )
    base: Optional[float] = Field(default=None, description="Taxable base amount when printed")
    amount: Optional[float] = Field(default=None, description="Tax amount when printed")


class ExtractedDocumentLine(BaseModel):
    """One visible line row — transcribed verbatim, not bookkeeper-summarized."""

    description: str
    quantity: Optional[float] = None
    unit_amount: Optional[float] = None
    net_amount: Optional[float] = Field(
        None, description="Line net/ex-tax amount as printed"
    )
    gst_amount: Optional[float] = Field(
        default=0.0,
        description="Tax on this line when a tax column or amount is printed",
    )
    tax_label: Optional[str] = Field(
        default=None,
        description="Verbatim tax wording on the line, e.g. SR, ZR, GST 9%",
    )


class ExtractedDocument(BaseModel):
    """One logical invoice/receipt in an upload — faithful transcription."""

    doc_type: ExtractedDocType = Field(
        description=(
            "Document shape: invoice, receipt, credit_note, expense_claim, statement, other. "
            "Use statement ONLY for an SOA summary/cover page that lists invoice refs and "
            "balances — never bookable. Do NOT emit a statement entry alongside the "
            "itemized invoices it summarizes; put the cover page in skipped_pages instead."
        ),
    )
    page_range: list[int] = Field(
        description="1-based inclusive [start_page, end_page] this document occupies",
        min_length=2,
        max_length=2,
    )
    vendor: str = Field(description="Issuer / seller name as printed")
    buyer: Optional[str] = Field(default=None, description="Bill-to / buyer as printed")
    reference: str = Field(description="Invoice or bill number — verbatim, not a tax reg no")
    date: str = Field(description="Document date ISO yyyy-mm-dd")
    due_date: Optional[str] = None
    currency: str = Field(description="ISO 4217 currency code read from the document")
    presentation: LinePresentation = Field(
        default="itemized",
        description=(
            "itemized when lines[] mirrors every visible printed charge row; summary when "
            "the document exposes only summary/total buckets (e.g. SR + ZR on a telco bill). "
            "Never collapse itemized rows into summary buckets during extraction."
        ),
    )
    lines: list[ExtractedDocumentLine] = Field(
        default_factory=list,
        description=(
            "Every visible charge row transcribed verbatim — one line per printed row on "
            "itemized invoices/receipts. Do NOT merge or collapse rows for bookkeeping. "
            "When presentation=summary, lines[] holds only the summary rows as printed."
        ),
    )
    subtotal: Optional[float] = Field(None, description="Subtotal ex-tax as printed")
    tax_total: Optional[float] = Field(None, description="Total tax as printed")
    grand_total: float = Field(description="Grand total as printed")
    tax_lines: list[ExtractedTaxLine] = Field(
        default_factory=list,
        description="Tax groupings exactly as printed (any count N, not forced to 2)",
    )
    direction_for_client: DirectionForClient = Field(
        default="unknown",
        description=(
            "Direction in the client's books: purchase = client pays vendor; "
            "sales = client is issuer/collects; self_referential when client is both sides. "
            "Return unknown when the client cannot be identified on the document — "
            "never guess from partial name matches."
        ),
    )
    direction_reason: Optional[str] = None
    tax_visible_on_document: bool = Field(
        default=False,
        description="True only when a literal Tax/GST/VAT row or column is visible",
    )
    claimant_name: Optional[str] = Field(
        default=None,
        description="Claimant when doc_type == expense_claim",
    )
    vendor_tax_regno: Optional[str] = Field(
        default=None,
        description="Issuer UEN / tax registration number when visible",
    )
    vendor_country: Optional[str] = Field(
        default=None,
        description="2-letter issuer country when visible",
    )
    buyer_country: Optional[str] = Field(
        default=None,
        description="2-letter buyer country when visible",
    )
    tax_system_hint: Optional[str] = Field(
        default=None,
        description="GST / SST / VAT / NONE inferred from explicit tax wording",
    )


class ExtractedDocumentBundle(BaseModel):
    """One multimodal call per file — zero or more logical documents."""

    documents: list[ExtractedDocument] = Field(
        default_factory=list,
        description=(
            "One entry per distinct bookable invoice, receipt, credit note, or expense claim. "
            "Never include SOA summary covers — record those pages in skipped_pages only."
        ),
    )
    skipped_pages: Optional[list[int]] = Field(
        default=None,
        description=(
            "1-based page numbers for SOA summary/cover pages ONLY — never use for "
            "bookable invoices or receipts. Record the cover here and omit it from "
            "documents[]; embedded invoices on following pages become separate entries."
        ),
    )
    notes: Optional[str] = Field(
        default=None,
        description=(
            "Optional free-text note about segmentation or extraction uncertainty "
            "(e.g. ambiguous page splits, unreadable stamps) — not for bookkeeping summaries."
        ),
    )


def use_understand_extract() -> bool:
    """Drive-parity single-call Understand path (production default).

    Invoice/receipt/SOA packages always use the understand path. Routing ignores
    retired ``LEDGR_CAPTURE_BOOK`` and ``LEDGR_LEGACY_SOA``. ``LEDGR_UNDERSTAND_EXTRACT=0``
    is retained for diagnostics only.
    """
    raw = os.environ.get("LEDGR_UNDERSTAND_EXTRACT", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


FAITHFUL_EXTRACT_STATIC_INSTRUCTION = """You are transcribing financial documents faithfully into structured JSON.

Your job is to READ and COPY what is printed — not to summarize for bookkeeping.

Segmentation (one call, zero or more bookable documents):
- Return one ``documents`` entry per DISTINCT logical invoice, receipt, credit note,
  or expense claim.
- If the file is a single document, return a one-element ``documents`` list.
- For multiple invoices in one PDF: one entry each, with its own ``page_range``,
  ``reference``, and ``grand_total``.
- For multiple receipts on one scanned page: one entry per receipt.

SOA packages (Statement of Account):
- Page 1 is often a summary/cover listing invoice refs and a rolled-up total.
- Put cover page number(s) in ``skipped_pages`` ONLY — do NOT add a ``documents[]``
  entry for the cover. Extract only the embedded invoices on following pages.
- A summary/cover total re-sums the invoices that follow; it is NOT a bookable
  document even when it prints a grand total.
- ``doc_type`` = statement describes an SOA summary cover if you must classify it;
  never book it alongside the itemized invoices it summarizes.

Line granularity (faithful transcription):
- Itemized invoice/receipt: one ``lines[]`` row per visible printed charge row.
- Telco/utility bill whose ONLY charge breakdown is a summary section: transcribe
  those summary rows as printed (often SR + ZR buckets) with ``presentation`` =
  "summary" — do NOT emit per-phone/per-call detail from appendix pages unless
  those rows are visibly printed on the bill face.
- Simple single-total receipt: one line when only one charge row exists.
- Do NOT collapse itemized rows into bookkeeper buckets during extraction.

Per document, transcribe VERBATIM:
- ``doc_type``: invoice | receipt | credit_note | expense_claim | other (see SOA rule
  above for statement).
- ``page_range``: [start_page, end_page] 1-based inclusive pages this doc uses.
- ``vendor`` / ``buyer`` / ``reference`` / ``date`` / ``currency`` as printed.
- ``lines[]``: every visible charge row — do NOT collapse rows into ledger buckets.
- ``presentation``: "itemized" when lines mirror printed rows; "summary" when only
  summary/total rows exist on the document.
- ``subtotal`` / ``tax_total`` / ``grand_total`` exactly as printed on that document.
- ``tax_lines[]``: every printed tax grouping (any count N — do NOT force two lines).
- ``tax_visible_on_document``: true ONLY when Tax/GST/VAT wording and amounts appear.
- When ``tax_visible_on_document`` is false, line ``gst_amount`` must be 0.

Direction (when client context is supplied in the user turn):
- ``direction_for_client``: purchase | sales | self_referential | unknown.
- Decide from From/Bill-To/claimant/letterhead blocks vs the client identity.
- Abstain: return ``unknown`` when the client does not appear on the document —
  never guess from partial name matches or ambiguous layout.

Expense claims:
- ``doc_type`` = expense_claim; ``claimant_name`` = person signing the form.
- The letterhead company is the approver, not the supplier.

Uncertainty:
- Use ``notes`` for segmentation or extraction uncertainty (ambiguous splits,
  unreadable stamps) — not for bookkeeping summaries.

Arithmetic:
- Line nets + tax must reconcile to ``grand_total`` for each document.
- Discount lines use negative ``net_amount`` when printed as discounts.

Examples (synthetic — placeholders only, no real names):

[SOA package] Page 1: "<Vendor-A> Statement of Account" listing INV-001..003 and
total MYR 1,500. Pages 2-4: three separate tax invoices with own refs and totals
summing to 1,500.
- skipped_pages: [1]
- documents: three invoice entries (pages 2, 3, 4) — NO cover entry
- notes: null unless page boundaries are ambiguous

[Itemized invoice] "<Vendor-B> Tax Invoice" with 5 printed line rows and a GST column.
- presentation: itemized
- lines[]: 5 rows matching the printout — do NOT merge into one "Services" line

[Unknown direction] Receipt with no client name or UEN visible anywhere.
- direction_for_client: unknown"""


def _build_faithful_extract_static_instruction() -> str:
    """Invariant faithful-extraction rules (WS-6.2 cacheable prefix)."""
    return FAITHFUL_EXTRACT_STATIC_INSTRUCTION


def _build_faithful_extract_dynamic_prompt(
    client_name: Optional[str], client_uen: Optional[str]
) -> str:
    """Per-call client context appended to the PDF user turn."""
    if not client_name and not client_uen:
        return "Transcribe the uploaded document(s) into the JSON schema."
    bits: list[str] = []
    if client_name:
        bits.append(f"name = {client_name}")
    if client_uen:
        bits.append(f"UEN = {client_uen}")
    ctx = " · ".join(bits)
    return f"""Client context: {ctx}.
Match the client by name OR UEN against vendor/buyer/letterhead/claimant blocks.
Abstain with direction_for_client=unknown when the client does not appear on the document — never guess."""


def _build_faithful_extract_prompt(
    client_name: Optional[str], client_uen: Optional[str]
) -> str:
    """Full faithful prompt (static + dynamic) — backward-compatible helper."""
    static = _build_faithful_extract_static_instruction()
    dynamic = _build_faithful_extract_dynamic_prompt(client_name, client_uen)
    return f"{static}\n\n{dynamic}"


def _drop_soa_cover_documents(bundle: ExtractedDocumentBundle) -> ExtractedDocumentBundle:
    """Deterministically drop a Statement-of-Account / summary cover document.

    The model is told to skip SOA cover pages (FAITHFUL_EXTRACT_STATIC_INSTRUCTION)
    but sometimes returns the cover as a full document alongside the real invoices,
    double-counting the totals. Two vendor-agnostic signals identify a redundant
    cover in a multi-document bundle:

    1. PRIMARY: a document whose ``doc_type`` is ``statement`` — a summary cover
       sitting among the real invoices/receipts it itemizes.
    2. FALLBACK (only when NO ``statement`` doc is present — handles the model
       mislabeling the cover as ``invoice``): a SINGLE document whose
       ``grand_total`` equals the sum of all the other documents' grand_totals
       within one cent. If more than one document matches, the signal is
       ambiguous and nothing is dropped.

    Guard: never empty the bundle. A cover is only dropped when at least one
    bookable document survives; if every document is a statement the whole
    bundle is left intact for human review.
    """
    if len(bundle.documents) < 2:
        return bundle

    docs = bundle.documents
    totals = [float(doc.grand_total or 0.0) for doc in docs]

    statement_idxs = [
        i for i, doc in enumerate(docs)
        if (doc.doc_type or "").strip().lower() == "statement"
    ]

    if statement_idxs:
        drop_idxs = statement_idxs
        signal = "doc_type=statement"
    else:
        # Arithmetic fallback: one doc whose total == sum of the others (±1c).
        fallback_idxs = [
            i
            for i in range(len(docs))
            if len(docs) - 1 >= 2
            and abs(totals[i] - (sum(totals) - totals[i])) < 0.01
        ]
        if len(fallback_idxs) == 1:
            drop_idxs = fallback_idxs
            signal = "grand_total==sum-of-others"
        else:
            drop_idxs = []
            signal = ""

    # Guard: never drop so many docs that zero bookable documents remain.
    if not drop_idxs or len(drop_idxs) >= len(docs):
        return bundle

    drop_set = set(drop_idxs)
    skipped: set[int] = set(bundle.skipped_pages or [])
    for i in drop_idxs:
        doc = docs[i]
        pages = [int(p) for p in (doc.page_range or [])]
        skipped.update(pages)
        logger.warning(
            "hard-gate: dropping SOA cover document (signal=%s, reference=%s, page_range=%s)",
            signal,
            doc.reference,
            pages or None,
        )

    bundle.documents = [doc for i, doc in enumerate(docs) if i not in drop_set]
    bundle.skipped_pages = sorted(skipped) if skipped else None
    return bundle


def _extract_document_ledger_once(
    data: bytes,
    mime_type: str,
    *,
    project: Optional[str] = None,
    location: Optional[str] = None,
    model: Optional[str] = None,
    hint: Optional[str] = None,
    client_name: Optional[str] = None,
    client_uen: Optional[str] = None,
) -> ExtractedDocumentBundle:
    """One multimodal call — faithful ``documents[]`` array (WS-2.1)."""
    client = make_client(project, location)
    model = model or lite_model()
    part = types.Part.from_bytes(data=data, mime_type=mime_type)
    static_instruction = _build_faithful_extract_static_instruction()
    dynamic_prompt = _append_hint(
        _build_faithful_extract_dynamic_prompt(client_name, client_uen),
        hint,
    )
    resp = client.models.generate_content(
        model=model,
        contents=[part, dynamic_prompt],
        config=default_llm_config(
            system_instruction=static_instruction,
            temperature=0,
            response_mime_type="application/json",
            response_schema=ExtractedDocumentBundle,
        ),
    )
    log_context_cache_usage(resp, lane="extract")
    bundle = ExtractedDocumentBundle.model_validate_json(resp.text)
    return _drop_soa_cover_documents(bundle)


def extract_document_ledger(
    data: bytes,
    mime_type: str,
    *,
    project: Optional[str] = None,
    location: Optional[str] = None,
    model: Optional[str] = None,
    hint: Optional[str] = None,
    client_name: Optional[str] = None,
    client_uen: Optional[str] = None,
) -> ExtractedDocumentBundle:
    """Extract bookable documents from one upload.

    Large multi-receipt PDFs are split into page windows so Gemini JSON does not
    truncate mid-response (issue #16). Smaller files use a single call, with a
    chunked retry when the model returns invalid JSON.
    """
    from pydantic import ValidationError

    from .pdf_chunks import (
        extract_document_ledger_chunked,
        should_chunk_pdf,
    )
    from .segmentation_gates import count_input_pages

    kwargs = {
        "project": project,
        "location": location,
        "model": model,
        "hint": hint,
        "client_name": client_name,
        "client_uen": client_uen,
    }
    page_count = count_input_pages(data, mime_type) if mime_type == "application/pdf" else 1
    if should_chunk_pdf(data, mime_type, page_count=page_count):
        bundle = extract_document_ledger_chunked(
            data,
            mime_type,
            extract_fn=_extract_document_ledger_once,
            **kwargs,
        )
        # Per-chunk SOA drop only sees one window at a time; a cover alone in
        # chunk 1 survives until we merge the full faithful array.
        return _drop_soa_cover_documents(bundle)

    try:
        return _extract_document_ledger_once(data, mime_type, **kwargs)
    except ValidationError:
        if mime_type != "application/pdf":
            raise
        bundle = extract_document_ledger_chunked(
            data,
            mime_type,
            extract_fn=_extract_document_ledger_once,
            **kwargs,
        )
        return _drop_soa_cover_documents(bundle)


def extract_ledger_file(path: str, **kwargs) -> ExtractedDocumentBundle:
    from pathlib import Path

    p = Path(path)
    return extract_document_ledger(p.read_bytes(), mime_for(p), **kwargs)


def extracted_document_to_extracted_invoice(doc: ExtractedDocument) -> ExtractedInvoice:
    """Adapt one faithful document to the existing ExtractedInvoice reconcile path."""
    lines = [
        ExtractedLine(
            description=line.description,
            quantity=line.quantity,
            unit_amount=line.unit_amount,
            net_amount=line.net_amount,
            gst_amount=line.gst_amount,
            tax_label=line.tax_label,
        )
        for line in doc.lines
    ]
    issuer_name = doc.vendor
    if doc.doc_type == "expense_claim" and doc.claimant_name:
        issuer_name = doc.claimant_name
    mapped_doc_type = "receipt" if doc.doc_type == "receipt" else "invoice"
    return ExtractedInvoice(
        doc_type=mapped_doc_type,
        invoice_number=doc.reference,
        invoice_date=doc.date,
        due_date=doc.due_date,
        currency=doc.currency,
        issuer_name=issuer_name,
        issuer_gst_regno=doc.vendor_tax_regno,
        issuer_country=doc.vendor_country,
        issuer_tax_system=doc.tax_system_hint or "NONE",
        bill_to_name=doc.buyer,
        bill_to_country=doc.buyer_country,
        lines=lines,
        subtotal=doc.subtotal if doc.subtotal is not None else 0.0,
        gst_total=doc.tax_total if doc.tax_total is not None else 0.0,
        total=doc.grand_total,
    )


def _effective_direction(doc: ExtractedDocument, direction: str) -> str:
    effective_direction = direction
    if direction == "auto" and doc.direction_for_client != "unknown":
        effective_direction = doc.direction_for_client
    elif doc.direction_for_client not in ("unknown", "") and not direction:
        effective_direction = doc.direction_for_client
    return effective_direction


def extracted_document_to_normalized(
    doc: ExtractedDocument,
    *,
    direction: str,
    our_gst_registered: bool = True,
    client_country: str = "SG",
    base_currency: str = "SGD",
) -> NormalizedInvoice:
    """Map one ExtractedDocument → NormalizedInvoice."""
    effective_direction = _effective_direction(doc, direction)
    structural_direction = (
        effective_direction
        if effective_direction in ("purchase", "sales")
        else "purchase"
    )
    inv = to_normalized(
        extracted_document_to_extracted_invoice(doc),
        direction=structural_direction,
        our_gst_registered=our_gst_registered,
        client_country=client_country,
        base_currency=base_currency,
    )
    if direction_needs_review(effective_direction):
        append_direction_review_note(inv, effective_direction)
    inv.tax_visible_on_document = doc.tax_visible_on_document
    inv.direction_reason = doc.direction_reason
    if len(doc.page_range) >= 2:
        inv.page_range = (int(doc.page_range[0]), int(doc.page_range[1]))
    doc_kind = (doc.doc_type or "").strip().lower()
    if doc_kind:
        inv.document_kind = doc_kind
    return inv


def validate_extracted_document(doc: ExtractedDocument) -> tuple[bool, str]:
    """Math gate for one faithful document (G1 + G4 per-currency tolerance)."""
    if not doc.lines:
        return False, "no lines extracted"
    ex = extracted_document_to_extracted_invoice(doc)
    return reconcile(
        ex,
        tax_visible_on_document=doc.tax_visible_on_document,
        currency=doc.currency,
    )


def bundle_page_ranges(bundle: ExtractedDocumentBundle) -> list[tuple[int, int]]:
    """Project each document's page_range to (start, end) tuples."""
    ranges: list[tuple[int, int]] = []
    for doc in bundle.documents:
        if len(doc.page_range) >= 2:
            ranges.append((doc.page_range[0], doc.page_range[1]))
    return ranges
