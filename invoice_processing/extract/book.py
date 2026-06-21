"""BOOK layer — posting proposal from faithful capture (Simple Intelligent Puzzle).

One structured Gemini call: capture JSON + client profile → BookingProposal.
Python does not override direction or invent tax.
"""

from __future__ import annotations

import json
from typing import Callable, Literal, Optional

from google.genai import types
from google.genai import errors as genai_errors
from pydantic import BaseModel, Field

from ..shared_libraries.gemini_call_config import default_llm_config
from ..shared_libraries.genai_client import lite_model, make_client
from ..shared_libraries.model_config import std_model
from .document_record import DocumentRecord
from .invoice_extractor import ExtractedInvoice, ExtractedLine


class BookingLedgerLine(BaseModel):
    description: str
    net_amount: float
    gst_amount: float = Field(
        default=0.0,
        description="GST on this line only when a tax column exists on the capture",
    )
    tax_hint: Optional[str] = Field(
        default=None,
        description="SR/ZR/ES/OS/NT only when tax wording is visible on the document",
    )
    capture_row_ref: Optional[str] = Field(
        default=None,
        description="Reference to capture line_items index or table row",
    )


class BookingProposal(BaseModel):
    doc_kind: str = Field(
        description="invoice | expense_claim | receipt | credit_note | other",
    )
    direction_for_client: Literal["purchase", "sales", "unknown"]
    direction_reason: str = Field(
        description="One sentence citing which party block matched the client",
    )
    ledger_lines: list[BookingLedgerLine]
    invoice_number: Optional[str] = None
    document_date: Optional[str] = Field(
        default=None,
        description="ISO yyyy-mm-dd when visible on capture",
    )
    currency: Optional[str] = None
    document_total: float = Field(
        description="Footer total — must match capture totals when present",
    )
    tax_visible_on_document: bool = Field(
        description="True only when capture shows a Tax/GST column or amount",
    )
    issuer_name: Optional[str] = None
    issuer_gst_regno: Optional[str] = None
    bill_to_name: Optional[str] = None


def _build_book_prompt(
    record: DocumentRecord,
    *,
    client_name: Optional[str] = None,
    client_uen: Optional[str] = None,
) -> str:
    bits: list[str] = []
    if client_name:
        bits.append(f"name = {client_name}")
    if client_uen:
        bits.append(f"UEN = {client_uen}")
    client_ctx = " · ".join(bits) if bits else "not provided"

    return f"""You are a bookkeeper proposing ledger postings from a faithful document capture.

Client context: {client_ctx}.

Capture JSON:
{json.dumps(record.model_dump(), indent=2)}

Rules:
- Set direction_for_client by comparing client name/UEN to parties on the capture:
  purchase when the client is the recipient/bill-to; sales when the client is the issuer.
  Expense claims and employee reimbursements are payables (purchase) to the employee.
- Set direction_reason in one sentence citing the matching party block.
- Set tax_visible_on_document true ONLY when the capture shows a Tax/GST column or amount.
- ledger_lines: bookkeeper posting granularity (may collapse telco to SR+ZR buckets).
  Line amounts must reconcile to document_total / capture footer total.
- per-line gst_amount must be 0 when tax_visible_on_document is false.
- Do not invent tax amounts or standard-rate assumptions.
- invoice_number and document_date from capture labeled_fields when visible.
"""


def book_from_capture(
    record: DocumentRecord,
    *,
    project: Optional[str] = None,
    location: Optional[str] = None,
    model: Optional[str] = None,
    client_name: Optional[str] = None,
    client_uen: Optional[str] = None,
) -> BookingProposal:
    """Structured book call — capture JSON in, posting proposal out."""
    client = make_client(project, location)
    primary = model or lite_model()
    models_to_try = [primary]
    if primary != std_model():
        models_to_try.append(std_model())
    prompt = _build_book_prompt(record, client_name=client_name, client_uen=client_uen)
    last_exc: Exception | None = None
    for attempt_model in models_to_try:
        try:
            resp = client.models.generate_content(
                model=attempt_model,
                contents=[prompt],
                config=default_llm_config(
                    temperature=0,
                    response_mime_type="application/json",
                    response_schema=BookingProposal,
                ),
            )
            return BookingProposal.model_validate_json(resp.text)
        except genai_errors.ServerError as exc:
            last_exc = exc
            if getattr(exc, "status_code", None) == 503 and attempt_model != std_model():
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("book_from_capture: no model attempted")


BOOK_FROM_CAPTURE_FN: Callable[..., BookingProposal] = book_from_capture


def slim_booking_proposal_for_state(proposal: BookingProposal) -> dict:
    """Minimal booking summary for session state (critic + HITL); no nested bloat."""
    return {
        "doc_kind": proposal.doc_kind,
        "direction_for_client": proposal.direction_for_client,
        "direction_reason": proposal.direction_reason,
        "invoice_number": proposal.invoice_number,
        "document_total": proposal.document_total,
        "tax_visible_on_document": proposal.tax_visible_on_document,
        "line_count": len(proposal.ledger_lines),
    }


def booking_to_extracted_invoice(
    proposal: BookingProposal,
    record: DocumentRecord,
) -> ExtractedInvoice:
    """Map BookingProposal → ExtractedInvoice for verify + to_normalized."""
    lines = [
        ExtractedLine(
            description=line.description,
            net_amount=line.net_amount,
            gst_amount=line.gst_amount if proposal.tax_visible_on_document else 0.0,
            tax_label=line.tax_hint,
        )
        for line in proposal.ledger_lines
    ]
    gst_total = sum(ln.gst_amount or 0.0 for ln in lines) if proposal.tax_visible_on_document else 0.0
    net_sum = sum(ln.net_amount or 0.0 for ln in lines)
    return ExtractedInvoice(
        doc_type="invoice",
        invoice_number=proposal.invoice_number,
        invoice_date=proposal.document_date,
        currency=proposal.currency,
        issuer_name=proposal.issuer_name,
        issuer_gst_regno=proposal.issuer_gst_regno,
        bill_to_name=proposal.bill_to_name,
        lines=lines,
        subtotal=net_sum if proposal.tax_visible_on_document else None,
        gst_total=gst_total if proposal.tax_visible_on_document else None,
        total=proposal.document_total,
    )
