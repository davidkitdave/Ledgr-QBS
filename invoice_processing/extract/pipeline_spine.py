"""Two-phase extraction spine for pipeline / eval (production winner: V3)."""

from __future__ import annotations

from pathlib import Path

from ..export.models import NormalizedInvoice
from .invoice_extractor import mime_for
from .process_invoice_document import process_invoice_document


def normalize_path_two_phase(
    path: str | Path,
    *,
    direction: str,
    our_gst_registered: bool = True,
    client_country: str = "SG",
    base_currency: str = "SGD",
    client_name: str | None = None,
    client_uen: str | None = None,
    doc_type: str = "invoice",
) -> NormalizedInvoice:
    """Understand or legacy extract → normalize; returns primary invoice."""
    path = Path(path)
    result = process_invoice_document(
        path.read_bytes(),
        mime_for(path),
        doc_type=doc_type,
        direction=direction if direction in ("purchase", "sales") else "purchase",
        our_gst_registered=our_gst_registered,
        base_currency=base_currency,
        client_name=client_name,
        client_uen=client_uen,
    )
    if not result.normalized:
        raise ValueError(f"normalize produced no invoices for {path.name}")
    return result.normalized[0]
