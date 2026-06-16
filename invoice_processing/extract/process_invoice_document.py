"""Shared invoice extraction orchestrator — graph nodes and eval harness.

Single entry for the invoice/receipt lane so ``nodes.py`` and eval do not
duplicate routing between understand-extract and legacy two-phase capture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..export.models import NormalizedInvoice
from .document_extractor import extract_document_bundle
from .document_normalizer import normalize_document_bundle
from .document_record import DocumentRecordBundle
from .ledger_extract import (
    DocumentLedgerExtract,
    extract_document_ledger,
    ledger_extract_to_normalized,
    should_use_legacy_extract,
    validate_ledger_extract,
)

EXTRACT_LEDGER_FN: Callable[..., DocumentLedgerExtract] = extract_document_ledger
EXTRACT_DOCUMENT_FN = extract_document_bundle
NORMALIZE_DOCUMENT_FN = normalize_document_bundle


@dataclass
class InvoiceProcessResult:
    """Outcome of ``process_invoice_document``."""

    normalized: list[NormalizedInvoice]
    extraction_path: str  # "understand" | "legacy"
    summary_table: list[dict[str, str]] = field(default_factory=list)
    ledger_extract: Optional[dict[str, Any]] = None
    document_records: Optional[list[dict]] = None
    skipped_pages: Optional[list[int]] = None
    document_read_notes: Optional[str] = None


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
    """Classify-routed extraction: understand path or legacy two-phase."""
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
