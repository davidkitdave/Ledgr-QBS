"""Shared invoice extraction orchestrator — graph nodes and eval harness.

Single entry for the invoice/receipt lane so ``nodes.py`` and eval do not
duplicate routing between capture→book→verify, understand-extract, and legacy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Optional

from ..export.models import NormalizedInvoice
from .book import BOOK_FROM_CAPTURE_FN, BookingProposal, booking_to_extracted_invoice, slim_booking_proposal_for_state
from .document_extractor import extract_document_bundle
from .document_normalizer import normalize_document_bundle
from .document_record import DocumentRecordBundle
from .invoice_extractor import to_normalized
from .ledger_extract import (
    DocumentLedgerExtract,
    extract_document_ledger,
    ledger_extract_to_normalized,
    should_use_legacy_extract,
    use_capture_book_pipeline,
    validate_ledger_extract,
)
from .verify import verify_extracted_invoice

EXTRACT_LEDGER_FN: Callable[..., DocumentLedgerExtract] = extract_document_ledger
EXTRACT_DOCUMENT_FN = extract_document_bundle
NORMALIZE_DOCUMENT_FN = normalize_document_bundle


@dataclass
class InvoiceProcessResult:
    """Outcome of ``process_invoice_document``."""

    normalized: list[NormalizedInvoice]
    extraction_path: str  # "capture_book" | "understand" | "legacy"
    summary_table: list[dict[str, str]] = field(default_factory=list)
    ledger_extract: Optional[dict[str, Any]] = None
    document_records: Optional[list[dict]] = None
    skipped_pages: Optional[list[int]] = None
    document_read_notes: Optional[str] = None
    booking_proposals: Optional[list[dict]] = None


def _parse_iso_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _process_capture_book(
    bundle: DocumentRecordBundle,
    *,
    direction: str,
    our_gst_registered: bool,
    base_currency: str,
    client_name: Optional[str],
    client_uen: Optional[str],
    model: Optional[str],
) -> tuple[list[NormalizedInvoice], list[dict]]:
    normalized: list[NormalizedInvoice] = []
    proposals_dump: list[dict] = []
    for doc in bundle.documents:
        proposal: BookingProposal = BOOK_FROM_CAPTURE_FN(
            doc,
            model=model,
            client_name=client_name,
            client_uen=client_uen,
        )
        proposals_dump.append(slim_booking_proposal_for_state(proposal))
        ex = booking_to_extracted_invoice(proposal, doc)
        ok, note = verify_extracted_invoice(ex, doc)
        effective_direction = direction
        if direction in ("auto", "") and proposal.direction_for_client != "unknown":
            effective_direction = proposal.direction_for_client
        elif not direction and proposal.direction_for_client != "unknown":
            effective_direction = proposal.direction_for_client
        inv = to_normalized(
            ex,
            direction=effective_direction or "purchase",
            our_gst_registered=our_gst_registered,
            base_currency=base_currency,
        )
        inv.invoice_date = inv.invoice_date or _parse_iso_date(proposal.document_date)
        inv.tax_visible_on_document = proposal.tax_visible_on_document
        inv.direction_reason = proposal.direction_reason
        inv.doc_total = proposal.document_total
        inv.reconciled = ok
        inv.reconcile_note = note
        normalized.append(inv)
    return normalized, proposals_dump


def process_invoice_document(
    data: bytes,
    mime_type: str,
    *,
    doc_type: str,
    direction: str,
    our_gst_registered: bool = True,
    base_currency: str = "SGD",
    client_name: Optional[str] = None,
    client_uen: Optional[str] = None,
    hint: Optional[str] = None,
    model: Optional[str] = None,
) -> InvoiceProcessResult:
    """Classify-routed extraction: capture→book→verify, understand, or legacy."""
    if should_use_legacy_extract(doc_type):
        bundle: DocumentRecordBundle = EXTRACT_DOCUMENT_FN(
            data, mime_type, model=model, hint=hint
        )
        normalized = NORMALIZE_DOCUMENT_FN(
            bundle,
            direction=direction,
            our_gst_registered=our_gst_registered,
            base_currency=base_currency,
            client_name=client_name,
            client_uen=client_uen,
        )
        from .document_normalizer import slim_document_record_for_state

        return InvoiceProcessResult(
            normalized=normalized,
            extraction_path="legacy",
            document_records=[
                slim_document_record_for_state(doc) for doc in bundle.documents
            ],
            skipped_pages=bundle.skipped_pages,
            document_read_notes=bundle.notes,
        )

    if use_capture_book_pipeline():
        bundle: DocumentRecordBundle = EXTRACT_DOCUMENT_FN(
            data, mime_type, model=model, hint=hint
        )
        normalized, proposals = _process_capture_book(
            bundle,
            direction=direction,
            our_gst_registered=our_gst_registered,
            base_currency=base_currency,
            client_name=client_name,
            client_uen=client_uen,
            model=model,
        )
        from .document_normalizer import slim_document_record_for_state

        return InvoiceProcessResult(
            normalized=normalized,
            extraction_path="capture_book",
            document_records=[
                slim_document_record_for_state(doc) for doc in bundle.documents
            ],
            skipped_pages=bundle.skipped_pages,
            document_read_notes=bundle.notes,
            booking_proposals=proposals,
        )

    extract = EXTRACT_LEDGER_FN(
        data,
        mime_type,
        model=model,
        hint=hint,
        client_name=client_name,
        client_uen=client_uen,
    )
    normalized = [
        ledger_extract_to_normalized(
            extract,
            direction=direction,
            our_gst_registered=our_gst_registered,
            base_currency=base_currency,
        )
    ]
    ok, note = validate_ledger_extract(extract)
    if not ok and normalized:
        inv = normalized[0]
        inv.reconciled = False
        inv.reconcile_note = note

    summary_table = [
        {"category": row.category, "details": row.details}
        for row in extract.summary_table
    ]
    return InvoiceProcessResult(
        normalized=normalized,
        extraction_path="understand",
        summary_table=summary_table,
        ledger_extract=extract.model_dump(),
    )
