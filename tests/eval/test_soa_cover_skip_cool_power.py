"""Regression gate: SOA cover-page skip for COOL POWER DEC 2025 package.

Ground truth (11-page PDF):
  - Page 1 = SOA cover ("DEBTOR STATEMENT") listing 19 line items — MUST be skipped.
  - Pages 2–11 = 10 real documents:
      CNA-00176 (credit note, 6 lines)
      IA-07465, IA-07467, IA-07514, IA-07522, IA-07526, IA-07527,
      IA-07573, IA-07588, IA-07590  (9 invoices)
  - Expected: 10 docs, 22 total ledger rows.
  - Phantom numbers hallucinated from the SOA table in the wrong extraction:
    IA-07316, IA-07330, IA-07332, IA-07365, IA-07368, IA-07383, IA-07392, IA-07428

Gated by LEDGR_TEST_DOC_DIR env var — the PDF is NOT committed to the repo.
Set LEDGR_TEST_DOC_DIR to the parent directory of the relative path below and
the test will run live against a real Gemini call (requires GOOGLE_API_KEY or
GOOGLE_CLOUD_PROJECT).

Run deliberately:
    LEDGR_TEST_DOC_DIR=~/Desktop/LocalTest/TestDoc \
    pytest tests/eval/test_soa_cover_skip_cool_power.py -v
"""

from __future__ import annotations

import os
import pathlib
from typing import Optional

import pytest

# ──────────────────────────────────────────────────────────────────────────────
# Path resolution
# ──────────────────────────────────────────────────────────────────────────────

_REL_PATH = pathlib.Path(
    "MYDoc/JBI PLUS AUTO ENTERPRISE/Purchase/COOL POWER - DEC 2025_.pdf"
)

_TEST_DOC_DIR: Optional[str] = os.environ.get("LEDGR_TEST_DOC_DIR")
_PDF_PATH: Optional[pathlib.Path] = (
    pathlib.Path(_TEST_DOC_DIR).expanduser() / _REL_PATH
    if _TEST_DOC_DIR
    else None
)

_SKIP_REASON = (
    "LEDGR_TEST_DOC_DIR not set — set it to the LocalTest/TestDoc directory "
    "and ensure GOOGLE_API_KEY or GOOGLE_CLOUD_PROJECT is exported."
)

_needs_pdf = pytest.mark.skipif(
    _PDF_PATH is None or not (_PDF_PATH and _PDF_PATH.exists()),
    reason=_SKIP_REASON,
)

# ──────────────────────────────────────────────────────────────────────────────
# Module-scoped bundle (call extract_invoice_bundle once per pytest session)
# ──────────────────────────────────────────────────────────────────────────────

_BUNDLE_CACHE: dict = {}

_EXPECTED_INVOICE_NUMBERS = {
    "CNA-00176",
    "IA-07465",
    "IA-07467",
    "IA-07514",
    "IA-07522",
    "IA-07526",
    "IA-07527",
    "IA-07573",
    "IA-07588",
    "IA-07590",
}

_PHANTOM_INVOICE_NUMBERS = {
    "IA-07316",
    "IA-07330",
    "IA-07332",
    "IA-07365",
    "IA-07368",
    "IA-07383",
    "IA-07392",
    "IA-07428",
}


def _get_bundle():
    """Return cached ExtractedInvoiceBundle, calling the extractor once."""
    if "result" not in _BUNDLE_CACHE:
        from invoice_processing.extract.invoice_extractor import extract_file_bundle

        assert _PDF_PATH is not None and _PDF_PATH.exists(), (
            f"PDF not found at {_PDF_PATH}"
        )
        _BUNDLE_CACHE["result"] = extract_file_bundle(_PDF_PATH)
    return _BUNDLE_CACHE["result"]


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _is_summary_shaped_line(line) -> bool:
    """True when a line looks like a phantom SOA-summary row.

    All three conditions must hold:
      1. description normalises to "" or "INVOICE" or "INVOICES"
      2. gst_amount == 0  (or None, treated as 0)
      3. ExtractedLine has no item_code attribute OR it is empty/None
         (ExtractedLine.item_code does not exist on the extraction model —
          real invoices that have no GST are caught only when description is
          also the bare "INVOICE" sentinel, so legit NT-coded SG invoices with
          real descriptions are safe.)
    """
    desc_upper = (line.description or "").strip().upper()
    gst = line.gst_amount or 0.0
    item_code = getattr(line, "item_code", None)
    no_item_code = not item_code
    return desc_upper in {"", "INVOICE", "INVOICES"} and gst == 0.0 and no_item_code


def _all_lines_summary_shaped(invoice) -> bool:
    """True when ALL lines on an invoice look like SOA-summary phantom rows."""
    if not invoice.lines:
        return False
    return all(_is_summary_shaped_line(line) for line in invoice.lines)


# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────


@_needs_pdf
def test_soa_cover_page_in_skipped_pages():
    """(a) Page 1 (the SOA cover) must appear in bundle.skipped_pages."""
    bundle = _get_bundle()
    skipped = bundle.skipped_pages or []
    assert 1 in skipped, (
        f"Expected page 1 in skipped_pages, got: {skipped}"
    )


@_needs_pdf
def test_ten_documents_extracted():
    """(b) Exactly 10 documents (1 credit note + 9 invoices) extracted."""
    bundle = _get_bundle()
    assert len(bundle.invoices) == 10, (
        f"Expected 10 invoices, got {len(bundle.invoices)}: "
        f"{[inv.invoice_number for inv in bundle.invoices]}"
    )


@_needs_pdf
def test_total_ledger_rows():
    """(c) 22 total ledger rows across all extracted documents."""
    bundle = _get_bundle()
    total_lines = sum(len(inv.lines) for inv in bundle.invoices)
    assert total_lines == 22, (
        f"Expected 22 ledger rows, got {total_lines}. "
        f"Per-doc counts: {[(inv.invoice_number, len(inv.lines)) for inv in bundle.invoices]}"
    )


@_needs_pdf
def test_no_invoice_has_skipped_page_number():
    """(d) Hard-gate invariant: no extracted invoice originates from a skipped page.

    ExtractedInvoice has no page_number field on the extraction model, so this
    test asserts the PROXY invariant: after the hard-gate is applied, no invoice
    whose lines are ALL summary-shaped (SOA-proxy) survives in bundle.invoices.
    This is the deterministic filter that commit 2 introduces in to_normalized_bundle.
    """
    bundle = _get_bundle()
    survivors = [
        inv.invoice_number
        for inv in bundle.invoices
        if _all_lines_summary_shaped(inv)
    ]
    assert survivors == [], (
        f"Invoices with all-summary-shaped lines (phantom SOA rows) survived: {survivors}"
    )


@_needs_pdf
def test_no_phantom_summary_rows():
    """(e) No invoice has description=='INVOICE', gst==0, and no item_code (phantom pattern)."""
    bundle = _get_bundle()
    phantoms = []
    for inv in bundle.invoices:
        for line in inv.lines:
            if _is_summary_shaped_line(line):
                phantoms.append((inv.invoice_number, line.description, line.gst_amount))
    assert phantoms == [], (
        f"Phantom summary-shaped rows found: {phantoms}"
    )


@_needs_pdf
def test_invoice_numbers_correct_no_phantoms():
    """(f) Invoice numbers match ground truth; no phantom SOA numbers appear."""
    bundle = _get_bundle()
    extracted_numbers = {inv.invoice_number for inv in bundle.invoices if inv.invoice_number}

    # No phantom numbers from the SOA table
    phantom_overlap = extracted_numbers & _PHANTOM_INVOICE_NUMBERS
    assert phantom_overlap == set(), (
        f"Phantom invoice numbers from SOA table found: {phantom_overlap}"
    )

    # All real numbers present (allow extra if model splits differently)
    missing = _EXPECTED_INVOICE_NUMBERS - extracted_numbers
    assert missing == set(), (
        f"Expected invoice numbers missing: {missing}. Got: {extracted_numbers}"
    )
