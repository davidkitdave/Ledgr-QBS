"""Verify layer — arithmetic and grounding checks only (Simple Intelligent Puzzle).

Python counts; it does not reinterpret document content.
"""

from __future__ import annotations

import re

from .document_record import DocumentRecord
from .invoice_extractor import ExtractedInvoice, reconcile


_TAX_LABEL_HINTS = frozenset(
    {
        "gst",
        "tax",
        "vat",
        "tax total",
        "gst total",
    }
)


def tax_visible_on_capture(record: DocumentRecord) -> bool:
    """True when capture shows an explicit tax/GST column or amounts."""
    for field in record.totals:
        label = (field.label or "").strip().lower()
        if any(h in label for h in _TAX_LABEL_HINTS):
            return True
    for item in record.line_items:
        if item.tax_label and str(item.tax_label).strip():
            return True
    for table in record.tables:
        headers = [h.strip().lower() for h in table.headers]
        if any(h in _TAX_LABEL_HINTS or "tax type" in h for h in headers):
            return True
    return False


def subtotal_present_on_capture(record: DocumentRecord) -> bool:
    for field in record.totals:
        label = (field.label or "").strip().lower()
        if "sub" in label and "total" in label:
            return True
    return False


def footer_total_from_capture(record: DocumentRecord) -> float | None:
    for field in record.totals:
        label = (field.label or "").strip().lower()
        if label in ("total", "grand total", "amount due", "total amount", "total usd"):
            parsed = _parse_amount(field.value)
            if parsed is not None:
                return parsed
        if label.endswith(" total") and "sub" not in label:
            parsed = _parse_amount(field.value)
            if parsed is not None:
                return parsed
    return None


def _parse_amount(text: str | None) -> float | None:
    if not text:
        return None
    cleaned = re.sub(r"[^\d.\-]", "", text.replace(",", ""))
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def verify_extracted_invoice(
    ex: ExtractedInvoice,
    record: DocumentRecord,
) -> tuple[bool, str]:
    """Reconcile lines against capture visibility and footer total."""
    tax_visible = tax_visible_on_capture(record)
    subtotal_present = subtotal_present_on_capture(record)
    footer = footer_total_from_capture(record)
    if footer is not None and ex.total is None:
        ex = ex.model_copy(update={"total": footer})
    elif footer is not None and ex.total is not None:
        ex = ex.model_copy(update={"total": footer})
    return reconcile(
        ex,
        tax_visible_on_document=tax_visible,
        subtotal_in_capture=subtotal_present,
    )
