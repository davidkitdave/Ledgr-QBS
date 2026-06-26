"""Shared invoice extraction orchestrator — graph nodes and eval harness.

Single entry for the invoice/receipt lane so ``nodes.py`` and eval do not
duplicate routing. Production always uses the understand/faithful-array path
(``extract_document_ledger``).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..export.client_context import EntityMemoryEntry
from ..export.models import NormalizedInvoice
from .ledger_extract import (
    ExtractedDocumentBundle,
    _drop_soa_cover_documents,
    extract_document_ledger,
    extracted_document_to_normalized,
    validate_extracted_document,
)
from .partial_failure import build_partial_failure_warnings
from .segmentation_gates import (
    apply_segmentation_uncertain_flag,
    count_input_pages,
    validate_bundle_page_coverage,
)

logger = logging.getLogger(__name__)

EXTRACT_LEDGER_FN: Callable[..., ExtractedDocumentBundle] = extract_document_ledger

SEGMENTATION_RETRY_HINT_MARKER = "Re-segment:"
SEGMENTATION_RETRY_HINT = (
    "Re-segment: every non-skipped page must appear in exactly one document "
    "page_range; SOA cover pages belong in skipped_pages only, not documents[]"
)

_RETIRED_LEGACY_ENV = "LEDGR_LEGACY_SOA"
_RETIRED_CAPTURE_BOOK_ENV = "LEDGR_CAPTURE_BOOK"


@dataclass
class InvoiceProcessResult:
    """Outcome of ``process_invoice_document``."""

    normalized: list[NormalizedInvoice]
    extraction_path: str  # "understand"
    summary_table: list[dict[str, str]] = field(default_factory=list)
    ledger_extract: Optional[dict[str, Any]] = None
    skipped_pages: Optional[list[int]] = None
    document_read_notes: Optional[str] = None
    booking_proposals: Optional[list[dict]] = None
    input_page_count: Optional[int] = None
    partial_failure_warnings: list[str] = field(default_factory=list)


def _is_segmentation_retry_hint(hint: Optional[str]) -> bool:
    return bool(hint and SEGMENTATION_RETRY_HINT_MARKER in hint)


def _build_segmentation_retry_hint(base_hint: Optional[str]) -> str:
    if base_hint:
        return f"{base_hint.strip()}\n\n{SEGMENTATION_RETRY_HINT}"
    return SEGMENTATION_RETRY_HINT


def _warn_if_retired_legacy_flags_set() -> None:
    """Log when deprecated env flags are set; routing ignores them."""
    legacy_raw = os.environ.get(_RETIRED_LEGACY_ENV, "0").strip().lower()
    if legacy_raw in ("1", "true", "yes", "on"):
        logger.warning(
            "%s is set but legacy SOA extraction is retired; "
            "using understand path instead.",
            _RETIRED_LEGACY_ENV,
        )
    capture_raw = os.environ.get(_RETIRED_CAPTURE_BOOK_ENV, "0").strip().lower()
    if capture_raw in ("1", "true", "yes", "on"):
        logger.warning(
            "%s is set but Capture→Book→Verify is retired; "
            "using understand path instead.",
            _RETIRED_CAPTURE_BOOK_ENV,
        )


def _normalize_ledger_bundle(
    bundle: ExtractedDocumentBundle,
    *,
    direction: str,
    our_gst_registered: bool,
    base_currency: str,
    entity_memory: list[EntityMemoryEntry] | None = None,
) -> list[NormalizedInvoice]:
    normalized: list[NormalizedInvoice] = []
    for doc in bundle.documents:
        inv = extracted_document_to_normalized(
            doc,
            direction=direction,
            our_gst_registered=our_gst_registered,
            base_currency=base_currency,
            entity_memory=entity_memory,
        )
        ok, note = validate_extracted_document(doc)
        if not ok:
            inv.reconciled = False
            inv.reconcile_note = note
        elif inv.reconciled:
            inv.reconcile_note = note
        normalized.append(inv)
    return normalized


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
    entity_memory: list[EntityMemoryEntry] | None = None,
    hint: Optional[str] = None,
    model: Optional[str] = None,
) -> InvoiceProcessResult:
    """Single orchestrated extraction via the understand/faithful-array path."""
    _warn_if_retired_legacy_flags_set()
    input_page_count = count_input_pages(data, mime_type)

    extract_kwargs = {
        "model": model,
        "client_name": client_name,
        "client_uen": client_uen,
    }
    bundle = EXTRACT_LEDGER_FN(data, mime_type, hint=hint, **extract_kwargs)
    normalized = _normalize_ledger_bundle(
        bundle,
        direction=direction,
        our_gst_registered=our_gst_registered,
        base_currency=base_currency,
        entity_memory=entity_memory,
    )

    total_pages = input_page_count
    page_ok, page_detail = validate_bundle_page_coverage(bundle, total_pages=total_pages)

    if not page_ok and not _is_segmentation_retry_hint(hint):
        retry_hint = _build_segmentation_retry_hint(hint)
        try:
            retry_bundle = EXTRACT_LEDGER_FN(
                data,
                mime_type,
                hint=retry_hint,
                **extract_kwargs,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("segmentation retry failed: %s", exc)
            retry_bundle = None
        if retry_bundle is not None:
            retry_bundle = _drop_soa_cover_documents(retry_bundle)
            retry_page_ok, retry_page_detail = validate_bundle_page_coverage(
                retry_bundle,
                total_pages=total_pages,
            )
            if retry_page_ok:
                bundle = retry_bundle
                normalized = _normalize_ledger_bundle(
                    bundle,
                    direction=direction,
                    our_gst_registered=our_gst_registered,
                    base_currency=base_currency,
                    entity_memory=entity_memory,
                )
                page_ok = retry_page_ok
                page_detail = retry_page_detail

    if not page_ok:
        apply_segmentation_uncertain_flag(normalized, page_detail)

    partial_warnings = build_partial_failure_warnings(
        normalized,
        page_coverage_ok=page_ok,
        page_coverage_detail=page_detail,
        input_page_count=input_page_count,
    )

    return InvoiceProcessResult(
        normalized=normalized,
        extraction_path="understand",
        skipped_pages=bundle.skipped_pages,
        document_read_notes=bundle.notes,
        ledger_extract=bundle.model_dump(),
        input_page_count=input_page_count,
        partial_failure_warnings=partial_warnings,
    )
