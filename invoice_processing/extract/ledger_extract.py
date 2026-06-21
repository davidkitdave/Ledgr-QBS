"""Drive-parity document understanding — one structured Gemini call.

Default invoice extraction path (ADR-0011): a single multimodal ``generate_content``
with the PDF ``Part`` plus a Pydantic ``response_schema``. Matches Google's
recommended pattern for invoice extraction.

ADR-0014's two-call Capture → Book → Verify pipeline is available as opt-in via
``LEDGR_CAPTURE_BOOK=1`` (SOA experiments only); not the default.

Returns a human-readable summary table plus ledger-ready lines in a single
``DocumentLedgerExtract``. Maps to :class:`NormalizedInvoice` via a thin adapter
that reuses ``reconcile`` / ``to_normalized`` from ``invoice_extractor``.

Per ADR-0011 and the Batch Direction plan, this module also owns the
``direction_for_client`` field: one multimodal call now decides the
sales-vs-purchase direction by looking at the document's From/To parties AND
the client identity passed into the prompt — replacing the legacy two-step
``classify_document`` + ``resolve_direction`` plumbing for invoice lane.
"""

from __future__ import annotations

import os
from typing import Literal, Optional

from google.genai import types
from pydantic import BaseModel, Field

from ..export.models import NormalizedInvoice
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
- ledger_lines at the granularity a bookkeeper would post (not every
  itemized row).
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

Granularity:
- Simple invoice / receipt: usually one ledger line with the service total.
- Telco / utility bill: exactly two summary ledger lines — one standard-rated
  GST bucket and one zero-rated bucket from the bill summary. Do NOT emit
  per-phone or per-call detail lines.
- Multi-line trade invoice: one ledger line per visible item row when there
  is no separate summary section.

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


class PartyField(BaseModel):
    """From/To party on a document (Drive-style "Sender" / "Recipient")."""

    name: str = Field(description="Party name as shown on the document")
    uen: Optional[str] = Field(
        default=None,
        description="UEN / tax reg no if visible on the document",
    )
    role: Literal["issuer", "recipient"] = Field(
        description="issuer = the party that sent/issued; recipient = the party billed/addressed"
    )
    country: Optional[str] = Field(
        default=None,
        description=(
            "2-letter country code (SG / MY / US / ...). Infer from any country "
            "indicator on the document. CRITICAL for multi-jurisdiction tax routing."
        ),
    )


class SummaryField(BaseModel):
    category: str = Field(description="Field label e.g. Vendor Name, Invoice Number")
    details: str = Field(description="Extracted value as shown on document")


class LedgerLine(BaseModel):
    description: str
    net_amount: float
    gst_amount: float = Field(default=0, description="GST component on this line if shown")
    tax_hint: Optional[str] = Field(
        default=None,
        description="SR, ZR, ES, OS, or NT — only when tax wording is visible on the document",
    )


#: Direction labels the Understand call can return. ``"unknown"`` triggers
#: a HITL gate — never a fuzzy Python rewrite.
DirectionForClient = Literal["purchase", "sales", "self_referential", "unknown"]


#: Document kinds the Understand call can declare (per ADR-0015 eval gate).
#: Used for eval rubrics and Slack UX surfacing only — Python never switches
#: on this value. ``"expense_claim"`` is the route the previous case study
#: (employee reimbursement) needs the model to surface.
DocKind = Literal["invoice", "receipt", "expense_claim", "credit_note", "other"]

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
        description="Classify each document in-call — invoice, receipt, credit_note, …"
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
            "itemized when lines mirror printed rows; summary when the document "
            "only exposes summary/totals rows"
        ),
    )
    lines: list[ExtractedDocumentLine] = Field(
        default_factory=list,
        description="Every printed charge row — do NOT collapse for bookkeeping",
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
            "Direction in the client's books for this document. "
            "purchase = client pays; sales = client collects."
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
        description="One entry per distinct invoice/receipt in the upload",
    )
    skipped_pages: Optional[list[int]] = Field(
        default=None,
        description="1-based pages deliberately skipped (e.g. SOA cover)",
    )
    notes: Optional[str] = Field(
        default=None,
        description="Optional note about segmentation decisions",
    )


class DocumentLedgerExtract(BaseModel):
    vendor_name: str
    customer_name: Optional[str] = None
    document_reference: str = Field(description="Invoice or bill number — not GST reg no")
    document_date: str = Field(description="ISO yyyy-mm-dd")
    due_date: Optional[str] = None
    currency: str = Field(
        default="SGD",
        description=(
            "ISO 4217 currency code of the document — read it from the document, "
            "not from the client. Primary source: the document's dedicated "
            "Currency column when present (if all rows agree, use that code). "
            "Secondary source: the total footer (e.g. 'TOTAL USD $1,195.11' "
            "→ USD; 'Sub Total SGD 1,000' → SGD). Foreign codes embedded in "
            "the Details / Description text (e.g. 'BHT 4466.68 x 0.0295' for "
            "a per-line FX conversion note) are NOT the document currency when "
            "a Currency column says otherwise. Never infer from the client "
            "letterhead, the SG address, or the client's base_currency. When "
            "every line and the footer agree on USD, currency must be USD even "
            "if the client books in SGD."
        ),
    )
    document_total: float
    subtotal: Optional[float] = Field(None, description="Ex-GST total if shown separately")
    gst_total: Optional[float] = Field(None, description="GST grand total if shown")
    issuer_gst_regno: Optional[str] = None
    tax_system_hint: Optional[str] = Field(
        default=None,
        description=(
            "Informational hint of which tax system applies (GST / SST / VAT / NONE). "
            "The jurisdiction router in accounting_agents.jurisdiction applies the "
            "canonical rule based on the client profile + this hint."
        ),
    )
    doc_kind: DocKind = Field(
        default="invoice",
        description=(
            "What kind of document this is. expense_claim = an employee or "
            "contractor submitting receipts for reimbursement (look for "
            '"Expense Claim", "Claim", "Reimbursement", a Claimant / Signature '
            "block, or a task / job reference plus per-row receipts). "
            "receipt = a supplier-issued sale receipt (no claimant). "
            "invoice = standard trade invoice. "
            "credit_note = refund / credit memo. other = none of the above."
        ),
    )
    claimant_name: Optional[str] = Field(
        default=None,
        description=(
            "The person submitting the claim when doc_kind == expense_claim. "
            "The claimant is the supplier of the reimbursement (the company "
            "letterhead is the approver, not the issuer). Leave null for "
            "non-expense_claim documents."
        ),
    )
    tax_visible_on_document: bool = Field(
        default=False,
        description=(
            "True ONLY when the document explicitly shows a Tax / GST / VAT "
            "row, column, or amount with a numeric value. A 'Total', "
            "'Amount', 'Subtotal', 'Currency' or 'Grand Total' column is "
            "NOT a tax column. If you cannot point to the literal word "
            "'GST', 'Tax', 'VAT', or a percentage like '9%' / '0%' on the "
            "document, this field is False. When false, ledger_lines must "
            "use gst_amount=0 — the export layer will never invent tax."
        ),
    )
    # Drive-parity parties: one structured From/To pair replaces the legacy
    # ``issuer_name`` / ``bill_to_name`` split. ``from_party`` and ``to_party``
    # are the single source of truth for parties in the Understand path;
    # ``vendor_name`` / ``customer_name`` are kept for backward compatibility
    # with the ``ExtractedInvoice`` reconcile adapter.
    from_party: Optional[PartyField] = Field(
        default=None,
        description="Who issued/sent the document — visible From block on the document",
    )
    to_party: Optional[PartyField] = Field(
        default=None,
        description="Who it is addressed/billed to — visible Bill-To / Recipient block",
    )
    direction_for_client: DirectionForClient = Field(
        default="unknown",
        description=(
            "Direction in the client's books. Decide by money flow, not "
            "letterhead. 'purchase' when the client is the party that PAYS "
            "(recipient of goods, services, or a reimbursement claim). "
            "'sales' when the client is the party that COLLECTS (issuer of "
            "the bill to a separate counterparty). 'self_referential' when "
            "the same legal entity is both issuer and recipient. 'unknown' "
            "when the client identity does not appear on the document or the "
            "parties are ambiguous — never guess."
        ),
    )
    direction_reason: Optional[str] = Field(
        default=None,
        description=(
            "Short grounded reason for direction_for_client — name the visible "
            "signal (letterhead vs claimant, Bill-To block, From block, etc.) "
            "that decided it. Example: 'claimant signed the form; client is "
            "the approver'. Used by eval rubrics for debugging; never Python-"
            "switched on."
        ),
    )
    summary_table: list[SummaryField] = Field(
        default_factory=list,
        description="Drive-style key facts for human review; 8–15 rows",
    )
    ledger_lines: list[LedgerLine] = Field(
        default_factory=list,
        description=(
            "Accounting lines to import. Simple invoice: usually 1 line. "
            "Telco bill: exactly 2 summary lines (SR + ZR buckets)."
        ),
    )


def use_understand_extract() -> bool:
    """Drive-parity single-call Understand path (default). Set LEDGR_UNDERSTAND_EXTRACT=0 to disable."""
    raw = os.environ.get("LEDGR_UNDERSTAND_EXTRACT", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def use_capture_book_pipeline() -> bool:
    """Opt-in Capture → Book → Verify path (ADR-0014). Off by default."""
    raw = os.environ.get("LEDGR_CAPTURE_BOOK", "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


def should_use_legacy_extract(doc_type: str) -> bool:
    """SOA packages stay on DocumentRecordBundle + normalizer."""
    if not use_understand_extract() and not use_capture_book_pipeline():
        return True
    return doc_type.strip().lower() in ("statement_of_account",)


def _build_faithful_extract_prompt(
    client_name: Optional[str], client_uen: Optional[str]
) -> str:
    """Faithful multi-document extraction prompt (WS-2.1, spec §1).

    One call per file returns ``documents[]`` — each element is transcribed
    verbatim (lines + printed totals + tax groupings). No bookkeeper
    summarization; segmentation and doc_type are decided in-call.
    """
    base = """You are transcribing financial documents faithfully into structured JSON.

Your job is to READ and COPY what is printed — not to summarize for bookkeeping.

For the uploaded file:
- Return one ``documents`` entry per DISTINCT logical invoice or receipt.
- If the file is a single document, return a one-element ``documents`` list.
- For SOA packages: skip the summary/cover page (record its page in
  ``skipped_pages``) and extract only embedded invoices.
- For multiple invoices in one PDF: one entry each, with its own ``page_range``,
  ``reference``, and ``grand_total``.
- For multiple receipts on one scanned page: one entry per receipt.

Per document, transcribe VERBATIM:
- ``doc_type``: classify in-call (invoice | receipt | credit_note | expense_claim |
  statement | other).
- ``page_range``: [start_page, end_page] 1-based inclusive pages this doc uses.
- ``vendor`` / ``buyer`` / ``reference`` / ``date`` / ``currency`` as printed.
- ``lines[]``: every visible charge row — do NOT collapse rows into ledger buckets.
- ``presentation``: "itemized" when lines mirror printed rows; "summary" when only
  summary/total rows exist on the document.
- ``subtotal`` / ``tax_total`` / ``grand_total`` exactly as printed on that document.
- ``tax_lines[]``: every printed tax grouping (any count N — do NOT force two lines).
- ``tax_visible_on_document``: true ONLY when Tax/GST/VAT wording and amounts appear.
- When ``tax_visible_on_document`` is false, line ``gst_amount`` must be 0.

Direction (when client context is supplied):
- ``direction_for_client``: purchase | sales | self_referential | unknown.
- Decide from From/Bill-To/claimant blocks vs the client — never guess.

Expense claims:
- ``doc_type`` = expense_claim; ``claimant_name`` = person signing the form.
- The letterhead company is the approver, not the supplier.

Arithmetic:
- Line nets + tax must reconcile to ``grand_total`` for each document.
- Discount lines use negative ``net_amount`` when printed as discounts.

Examples use placeholders only (Vendor-A, Buyer-B, Person-1) — no real names."""

    if client_name or client_uen:
        bits: list[str] = []
        if client_name:
            bits.append(f"name = {client_name}")
        if client_uen:
            bits.append(f"UEN = {client_uen}")
        ctx = " · ".join(bits)
        base += f"""

Client context: {ctx}.
Match the client by name OR UEN against vendor/buyer/letterhead/claimant blocks.
Return "unknown" when the client does not appear on the document."""

    return base


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
    """Single multimodal call — faithful ``documents[]`` array (WS-2.1).

    When ``client_name`` and/or ``client_uen`` are supplied, the prompt asks
    the model to set ``direction_for_client`` per document from visible
    From/To/claimant blocks.
    """
    client = make_client(project, location)
    model = model or lite_model()
    part = types.Part.from_bytes(data=data, mime_type=mime_type)
    prompt = _build_faithful_extract_prompt(client_name, client_uen)
    resp = client.models.generate_content(
        model=model,
        contents=[part, _append_hint(prompt, hint)],
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=ExtractedDocumentBundle,
        ),
    )
    return ExtractedDocumentBundle.model_validate_json(resp.text)


def extract_ledger_file(path: str, **kwargs) -> ExtractedDocumentBundle:
    from pathlib import Path

    p = Path(path)
    return extract_document_ledger(p.read_bytes(), mime_for(p), **kwargs)


def ledger_extract_to_extracted_invoice(extract: DocumentLedgerExtract) -> ExtractedInvoice:
    """Adapt understand output to the existing ExtractedInvoice reconcile path.

    Prefers the structured ``from_party`` / ``to_party`` blocks when present —
    the Drive-parity parties are richer than the legacy ``vendor_name`` /
    ``customer_name`` strings (they carry the role + UEN). Falls back to the
    legacy fields for backward compatibility with older extracts that don't
    populate the new schema fields.

    For ``expense_claim`` documents, the claimant (not the letterhead) is
    the supplier / issuer of the claim; the mapper prefers ``claimant_name``
    for the contact / issuer slot so the export layer's "Pay to" field lands
    on the right person. This is data-shape plumbing, not a domain rule
    (ADR-0015).
    """
    lines = [
        ExtractedLine(
            description=line.description,
            net_amount=line.net_amount,
            gst_amount=line.gst_amount,
            tax_label=line.tax_hint,
        )
        for line in extract.ledger_lines
    ]
    # Prefer structured parties when present; they win over the legacy strings.
    issuer_name = extract.vendor_name
    issuer_gst_regno = extract.issuer_gst_regno
    issuer_country: Optional[str] = None
    bill_to_name = extract.customer_name
    bill_to_country: Optional[str] = None
    if extract.from_party and extract.from_party.name:
        issuer_name = extract.from_party.name
        if extract.from_party.uen:
            issuer_gst_regno = extract.from_party.uen
        issuer_country = extract.from_party.country
    if extract.to_party and extract.to_party.name:
        bill_to_name = extract.to_party.name
        bill_to_country = extract.to_party.country
    # Expense-claim routing: the claimant (employee / contractor) is the
    # supplier; the company letterhead is the approver.
    if extract.doc_kind == "expense_claim" and extract.claimant_name:
        issuer_name = extract.claimant_name
    return ExtractedInvoice(
        doc_type="invoice",
        invoice_number=extract.document_reference,
        invoice_date=extract.document_date,
        due_date=extract.due_date,
        currency=extract.currency,
        issuer_name=issuer_name,
        issuer_gst_regno=issuer_gst_regno,
        issuer_country=issuer_country,
        issuer_tax_system=extract.tax_system_hint,
        bill_to_name=bill_to_name,
        bill_to_country=bill_to_country,
        lines=lines,
        subtotal=extract.subtotal,
        gst_total=extract.gst_total,
        total=extract.document_total,
    )


def ledger_extract_to_normalized(
    extract: DocumentLedgerExtract,
    *,
    direction: str,
    our_gst_registered: bool = True,
    client_country: str = "SG",
    base_currency: str = "SGD",
) -> NormalizedInvoice:
    """Map DocumentLedgerExtract → NormalizedInvoice (thin wrapper).

    Honors ``extract.direction_for_client`` when the caller passes a sentinel
    like ``"auto"`` — the Understand call now owns the direction decision, so
    passing ``"auto"`` is the standard way for the invoice lane to use the
    call's verdict (the legacy ``direction`` kwarg is kept for backward
    compatibility with the legacy two-phase path and tests).

    Propagates ``extract.tax_visible_on_document`` so the export layer honors
    ADR-0014 (never invent tax when the document is silent).
    """
    effective_direction = direction
    if direction == "auto" and extract.direction_for_client != "unknown":
        effective_direction = extract.direction_for_client
    elif extract.direction_for_client not in ("unknown", "") and not direction:
        # No direction supplied at all (Understand call owns it now).
        effective_direction = extract.direction_for_client
    structural_direction = (
        effective_direction
        if effective_direction in ("purchase", "sales")
        else "purchase"
    )
    inv = to_normalized(
        ledger_extract_to_extracted_invoice(extract),
        direction=structural_direction,
        our_gst_registered=our_gst_registered,
        client_country=client_country,
        base_currency=base_currency,
    )
    if direction_needs_review(effective_direction):
        append_direction_review_note(inv, effective_direction)
    inv.tax_visible_on_document = extract.tax_visible_on_document
    return inv


def validate_ledger_extract(extract: DocumentLedgerExtract) -> tuple[bool, str]:
    """CEL-style math gate after model extraction."""
    if not extract.ledger_lines:
        return False, "no ledger lines extracted"
    ex = ledger_extract_to_extracted_invoice(extract)
    return reconcile(ex, tax_visible_on_document=extract.tax_visible_on_document)


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
        issuer_tax_system=doc.tax_system_hint,
        bill_to_name=doc.buyer,
        bill_to_country=doc.buyer_country,
        lines=lines,
        subtotal=doc.subtotal,
        gst_total=doc.tax_total,
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
    return inv


def validate_extracted_document(doc: ExtractedDocument) -> tuple[bool, str]:
    """Math gate for one faithful document."""
    if not doc.lines:
        return False, "no lines extracted"
    ex = extracted_document_to_extracted_invoice(doc)
    return reconcile(ex, tax_visible_on_document=doc.tax_visible_on_document)


def bundle_page_ranges(bundle: ExtractedDocumentBundle) -> list[tuple[int, int]]:
    """Project each document's page_range to (start, end) tuples."""
    ranges: list[tuple[int, int]] = []
    for doc in bundle.documents:
        if len(doc.page_range) >= 2:
            ranges.append((doc.page_range[0], doc.page_range[1]))
    return ranges
