"""Drive-parity document understanding — one structured Gemini call.

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
from ..shared_libraries.genai_client import default_model, make_client
from .invoice_extractor import (
    ExtractedInvoice,
    ExtractedLine,
    _append_hint,
    mime_for,
    reconcile,
    to_normalized,
)


def _build_understand_prompt(client_name: Optional[str], client_uen: Optional[str]) -> str:
    """Return the Understand prompt with client context appended for direction.

    When client identity is provided, the prompt explicitly asks Gemini to
    determine ``direction_for_client`` by comparing the document's
    From/To parties against the client. This replaces the legacy
    ``resolve_direction()`` fuzzy-match pass for the invoice lane (kept for
    bank/SOA only).
    """
    base = """Extract billing details from this document for accounting review.

Produce:
- A Drive-style summary_table (Category / Details pairs) for human review.
- ledger_lines at the granularity a bookkeeper would post (not every itemized row).

Rules:
- Simple invoice/receipt: usually one ledger line with the service total.
- Telco/utility bill (Telco Provider A, Telco Provider B, M1): exactly two summary ledger lines —
  one standard-rated GST bucket and one zero-rated bucket from the bill summary.
  Do NOT emit per-phone or per-call detail lines.
- Multi-line trade invoice: one ledger line per visible item row when there is no
  separate summary section.
- document_reference is the invoice or bill number — NOT a GST registration number.
- document_date as ISO yyyy-mm-dd.
- Ensure ledger line nets + GST reconcile to document_total.
- from_party: the entity that ISSUED or SENT the document (the seller / vendor /
  billing party). Capture their UEN / tax reg no if visible.
- to_party: the entity the document is ADDRESSED or BILLED to (the buyer /
  recipient / bill-to party). Capture their UEN / tax reg no if visible."""

    if client_name or client_uen:
        bits: list[str] = []
        if client_name:
            bits.append(f"name = {client_name}")
        if client_uen:
            bits.append(f"UEN = {client_uen}")
        ctx = " · ".join(bits)
        base += f"""

Client context: {ctx}.
Set direction_for_client as follows:
- "purchase" if the client is the RECIPIENT (to_party matches the client)
- "sales" if the client is the ISSUER (from_party matches the client)
- "self_referential" if from_party and to_party are the same entity
- "unknown" if the document's From/To is ambiguous or the client is not visible

Match the client by name OR UEN. Visual layout (header block, "Bill To", "From",
signature block) takes precedence over party-name string matching."""

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


class SummaryField(BaseModel):
    category: str = Field(description="Field label e.g. Vendor Name, Invoice Number")
    details: str = Field(description="Extracted value as shown on document")


class LedgerLine(BaseModel):
    description: str
    net_amount: float
    gst_amount: float = Field(default=0, description="GST component on this line if shown")
    tax_hint: str = Field(
        default="SR",
        description="SR, ZR, ES, OS, or NT — document-level hint; client GST rules apply later",
    )


#: Direction labels the Understand call can return. ``"unknown"`` triggers
#: a HITL gate — never a fuzzy Python rewrite.
DirectionForClient = Literal["purchase", "sales", "self_referential", "unknown"]


class DocumentLedgerExtract(BaseModel):
    vendor_name: str
    customer_name: Optional[str] = None
    document_reference: str = Field(description="Invoice or bill number — not GST reg no")
    document_date: str = Field(description="ISO yyyy-mm-dd")
    due_date: Optional[str] = None
    currency: str = Field(default="SGD")
    document_total: float
    subtotal: Optional[float] = Field(None, description="Ex-GST total if shown separately")
    gst_total: Optional[float] = Field(None, description="GST grand total if shown")
    issuer_gst_regno: Optional[str] = None
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
            "Resolved against the client identity passed into the prompt: "
            "'purchase' if the client is the recipient, 'sales' if the client is "
            "the issuer, 'self_referential' if from==to, 'unknown' otherwise."
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
    """Feature flag: Drive-parity understand path (default on)."""
    raw = os.environ.get("LEDGR_UNDERSTAND_EXTRACT", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def should_use_legacy_extract(doc_type: str) -> bool:
    """SOA packages and complex multi-doc splits stay on DocumentRecordBundle."""
    if not use_understand_extract():
        return True
    return doc_type.strip().lower() in ("statement_of_account",)


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
) -> DocumentLedgerExtract:
    """Single multimodal call — summary table + ledger lines + direction.

    When ``client_name`` and/or ``client_uen`` are supplied, the prompt is
    augmented with a "Client context" block that asks the model to set
    ``direction_for_client`` based on the document's From/To parties. This is
    the Drive-shaped pattern: the client is known to the call (like a file
    owner in Drive) and the model is responsible for resolving sales/purchase
    in one shot, not via a downstream fuzzy-match pass.
    """
    client = make_client(project, location)
    model = model or default_model()
    part = types.Part.from_bytes(data=data, mime_type=mime_type)
    prompt = _build_understand_prompt(client_name, client_uen)
    resp = client.models.generate_content(
        model=model,
        contents=[part, _append_hint(prompt, hint)],
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=DocumentLedgerExtract,
        ),
    )
    return DocumentLedgerExtract.model_validate_json(resp.text)


def extract_ledger_file(path: str, **kwargs) -> DocumentLedgerExtract:
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
    net_sum = sum(l.net_amount or 0.0 for l in lines)
    gst_sum = sum(l.gst_amount or 0.0 for l in lines)
    # Prefer structured parties when present; they win over the legacy strings.
    issuer_name = extract.vendor_name
    issuer_gst_regno = extract.issuer_gst_regno
    bill_to_name = extract.customer_name
    if extract.from_party and extract.from_party.name:
        issuer_name = extract.from_party.name
        if extract.from_party.uen:
            issuer_gst_regno = extract.from_party.uen
    if extract.to_party and extract.to_party.name:
        bill_to_name = extract.to_party.name
    return ExtractedInvoice(
        doc_type="invoice",
        invoice_number=extract.document_reference,
        invoice_date=extract.document_date,
        due_date=extract.due_date,
        currency=extract.currency,
        issuer_name=issuer_name,
        issuer_gst_regno=issuer_gst_regno,
        bill_to_name=bill_to_name,
        lines=lines,
        subtotal=extract.subtotal if extract.subtotal is not None else net_sum,
        gst_total=extract.gst_total if extract.gst_total is not None else gst_sum,
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
    """
    effective_direction = direction
    if direction == "auto" and extract.direction_for_client != "unknown":
        effective_direction = extract.direction_for_client
    elif extract.direction_for_client not in ("unknown", "") and not direction:
        # No direction supplied at all (Understand call owns it now).
        effective_direction = extract.direction_for_client
    ex = ledger_extract_to_extracted_invoice(extract)
    return to_normalized(
        ex,
        direction=effective_direction,
        our_gst_registered=our_gst_registered,
        client_country=client_country,
        base_currency=base_currency,
    )


def validate_ledger_extract(extract: DocumentLedgerExtract) -> tuple[bool, str]:
    """CEL-style math gate after model extraction."""
    if not extract.ledger_lines:
        return False, "no ledger lines extracted"
    ex = ledger_extract_to_extracted_invoice(extract)
    return reconcile(ex)
