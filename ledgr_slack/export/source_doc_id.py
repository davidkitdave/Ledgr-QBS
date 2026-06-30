"""Stable per-logical-document id for tagging exported rows (issue #28).

Line-level deterministic eval needs every exported row to carry the identity of
the *logical document* it came from, so a golden scorer can join live
``process_document_batch`` output rows back to an expected manifest and score
per-line tax treatment / COA / direction (ADR-0026 §5).

The id must be:

- **stable per logical document** — survives a multi-document PDF fan-out, so two
  invoices in one PDF get two distinct ids;
- **reproducible run-to-run** — it must NOT embed the Slack ``file_id`` (that
  rotates on every re-upload) or any timestamp.

This mirrors the dedupe identity in
:func:`accounting_agents.ledger_doc_identity.ledger_doc_identity`: source basename
+ reference + page range. ``page_range`` is what disambiguates N documents from
one multi-invoice PDF when references would otherwise collide.
"""

from __future__ import annotations

from typing import Any


def source_doc_id_for_invoice(
    inv: Any,
    *,
    source_basename: str,
    index: int = 0,
) -> str:
    """Return a stable id for the logical document behind a ``NormalizedInvoice``.

    ``{source_basename}:{reference}:{start}-{end}`` when a page range is known,
    else ``{source_basename}:{reference}``. ``reference`` falls back to
    ``i{index}`` when the invoice carries no number (keeps distinct line-less
    docs from one bundle apart). The Slack ``file_id`` is deliberately NOT used
    — it rotates per run and would break run-to-run reproducibility.
    """
    base = (source_basename or "").strip()
    reference = (getattr(inv, "invoice_number", None) or "").strip() or f"i{index}"
    page_range = getattr(inv, "page_range", None)
    if page_range is not None and len(page_range) >= 2:
        start, end = page_range[0], page_range[1]
        return f"{base}:{reference}:{start}-{end}"
    return f"{base}:{reference}"
