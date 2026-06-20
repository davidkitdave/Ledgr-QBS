"""Hermetic tests for eval.ledger_eval.

No Gemini / network calls are made. All pipeline interaction is replaced with
deterministic stubs returning real model objects.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from invoice_processing.export.client_context import ClientContext, CoaAccount
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice
from invoice_processing.pipeline import ProcessedDoc
from invoice_processing.export.routing import DocRoute

from eval.ledger_eval import (
    EvalReport,
    default_client,
    discover_samples,
    run_eval,
)


# ---------------------------------------------------------------------------
# Helpers to build stub ProcessedDocs
# ---------------------------------------------------------------------------

def _stub_route() -> DocRoute:
    return DocRoute(
        fy=2025,
        bucket="purchase",
        archive_path="eval-default/FY2025/purchase/stub.pdf",
        workbook="Ledger_FY2025.xlsx",
        sheet="Purchase",
    )


def _make_normalized(
    lines: list[InvoiceLine],
    reconciled: bool = True,
    reconcile_note: str = "ok",
) -> NormalizedInvoice:
    inv = NormalizedInvoice(doc_type="purchase")
    inv.lines = lines
    inv.reconciled = reconciled
    inv.reconcile_note = reconcile_note
    return inv


def _make_invoice_doc(
    path: str,
    lines: list[InvoiceLine],
    reconciled: bool = True,
    doc_type: str = "invoice",
    direction: str = "purchase",
    note: str = "ok",
) -> ProcessedDoc:
    normalized = _make_normalized(lines, reconciled=reconciled)
    return ProcessedDoc(
        path=path,
        doc_type=doc_type,
        direction=direction,
        normalized=normalized,
        bank=None,
        route=_stub_route(),
        reconciled=reconciled,
        note=note,
    )


def _make_error_doc(path: str) -> ProcessedDoc:
    """A doc whose note starts with ERROR (pipeline's internal error sentinel)."""
    return ProcessedDoc(
        path=path,
        doc_type="unknown",
        direction=None,
        normalized=None,
        bank=None,
        route=_stub_route(),
        reconciled=False,
        note="ERROR: extraction failed",
    )


# ---------------------------------------------------------------------------
# Canned docs used in all aggregate tests
#
# Doc A: invoice, 3 lines, 2 have account_code, tax SR+ZR, reconciled=True
# Doc B: receipt, 2 lines, 0 have account_code, tax SR, reconciled=False
# Doc C: error doc (note starts with ERROR)
# Doc D: raises an exception from process_fn itself
# ---------------------------------------------------------------------------

DOC_A_LINES = [
    InvoiceLine(description="Office Supplies", account_code="500", tax_treatment="SR"),
    InvoiceLine(description="Printing",        account_code="501", tax_treatment="ZR"),
    InvoiceLine(description="Misc",            account_code=None,  tax_treatment="SR"),
]

DOC_B_LINES = [
    InvoiceLine(description="Rent",            account_code=None,  tax_treatment="SR"),
    InvoiceLine(description="Utilities",       account_code=None,  tax_treatment=None),
]

PATHS = ["a.pdf", "b.pdf", "c.pdf", "d.pdf"]


def _make_stub_process_fn():
    """Return a process_fn stub that yields canned docs A/B/C and raises on D."""

    def process_fn(path: str, client):
        if path == "a.pdf":
            return _make_invoice_doc("a.pdf", DOC_A_LINES, reconciled=True, doc_type="invoice")
        if path == "b.pdf":
            return _make_invoice_doc("b.pdf", DOC_B_LINES, reconciled=False, doc_type="receipt")
        if path == "c.pdf":
            return _make_error_doc("c.pdf")
        # d.pdf raises
        raise RuntimeError("simulated extraction failure")

    return process_fn


# ---------------------------------------------------------------------------
# Tests: EvalReport aggregates
# ---------------------------------------------------------------------------

@pytest.fixture
def report() -> EvalReport:
    client = ClientContext(client_id="test", fye_month=3, accounting_software="QBS Ledger")
    return run_eval(PATHS, client, process_fn=_make_stub_process_fn())


def test_n_docs(report):
    assert report.n_docs == 4


def test_classify_ok(report):
    # Doc A (invoice) + Doc B (receipt) = 2 classified; C=unknown error, D=unknown raise
    assert report.classify_ok == 2


def test_errors(report):
    # Doc C has ERROR note + Doc D raises = 2 errors
    assert report.errors == 2


def test_recon_pass(report):
    # Doc A reconciled=True, Doc B reconciled=False
    assert report.recon_pass == 1


def test_recon_rate(report):
    # 1 out of 2 eligible (invoice + receipt)
    assert abs(report.recon_rate - 0.5) < 1e-9


def test_total_lines(report):
    # Doc A: 3 lines, Doc B: 2 lines; C and D contribute 0
    assert report.total_lines == 5


def test_categorized_lines(report):
    # Doc A: 2 lines with account_code, Doc B: 0
    assert report.categorized_lines == 2


def test_categorization_fill_rate(report):
    # 2 / 5 = 0.40
    assert abs(report.categorization_fill_rate - 0.4) < 1e-9


# ---------------------------------------------------------------------------
# Tests: per-doc detail
# ---------------------------------------------------------------------------

def test_doc_a_detail(report):
    doc = next(d for d in report.docs if d.path == "a.pdf")
    assert doc.doc_type == "invoice"
    assert doc.direction == "purchase"
    assert doc.reconciled is True
    assert doc.n_lines == 3
    assert doc.n_lines_with_account == 2
    assert set(doc.tax_treatments) == {"SR", "ZR"}
    assert doc.error is None


def test_doc_b_detail(report):
    doc = next(d for d in report.docs if d.path == "b.pdf")
    assert doc.doc_type == "receipt"
    assert doc.reconciled is False
    assert doc.n_lines == 2
    assert doc.n_lines_with_account == 0
    assert doc.error is None


def test_doc_c_is_error(report):
    doc = next(d for d in report.docs if d.path == "c.pdf")
    assert doc.error is not None
    assert "ERROR" in doc.error


def test_doc_d_is_error(report):
    doc = next(d for d in report.docs if d.path == "d.pdf")
    assert doc.error is not None
    assert "simulated extraction failure" in doc.error


# ---------------------------------------------------------------------------
# Tests: edge cases
# ---------------------------------------------------------------------------

def test_empty_paths():
    """run_eval on zero paths returns a zeroed-out report."""
    client = ClientContext(client_id="test", fye_month=3)
    r = run_eval([], client, process_fn=lambda p, c: None)
    assert r.n_docs == 0
    assert r.errors == 0
    assert r.recon_rate == 0.0
    assert r.categorization_fill_rate == 0.0


def test_no_lines_no_division_error():
    """Docs with no lines must not cause ZeroDivisionError."""
    def stub(path, client):
        return _make_invoice_doc(path, [], reconciled=True, doc_type="invoice")

    client = ClientContext(client_id="test", fye_month=3)
    r = run_eval(["x.pdf"], client, process_fn=stub)
    assert r.total_lines == 0
    assert r.categorization_fill_rate == 0.0


def test_bank_statement_excluded_from_recon():
    """Bank-statement docs are classified OK but not counted for recon."""
    from invoice_processing.extract.bank_statement_extractor import ExtractedBankStatement

    def stub(path, client):
        return ProcessedDoc(
            path=path,
            doc_type="bank_statement",
            direction=None,
            normalized=None,
            bank=ExtractedBankStatement(accounts=[]),
            route=_stub_route(),
            reconciled=True,
            note="ok",
        )

    client = ClientContext(client_id="test", fye_month=3)
    r = run_eval(["bank.pdf"], client, process_fn=stub)
    assert r.classify_ok == 1
    assert r.recon_pass == 0
    assert r.recon_rate == 0.0   # no eligible docs → rate=0


# ---------------------------------------------------------------------------
# Tests: default_client
# ---------------------------------------------------------------------------

def test_default_client_coa_non_empty():
    """default_client() must seed a non-empty COA from standard_coa_rows."""
    client = default_client()
    assert isinstance(client.coa, list)
    assert len(client.coa) > 0, "expected at least one CoaAccount from standard COA"


def test_default_client_attributes():
    client = default_client()
    assert client.fye_month == 3
    assert client.accounting_software == "QBS Ledger"
    assert client.tax_registered is True


def test_default_client_coa_items_are_coa_accounts():
    client = default_client()
    for acc in client.coa:
        assert isinstance(acc, CoaAccount)


# ---------------------------------------------------------------------------
# Tests: discover_samples
# ---------------------------------------------------------------------------

def test_discover_samples_respects_limit(tmp_path):
    """discover_samples returns at most `limit` paths."""
    # Create 10 dummy PDF files
    for i in range(10):
        (tmp_path / f"invoice_{i:02d}.pdf").write_bytes(b"%PDF-1.4")

    result = discover_samples(root=tmp_path, limit=4)
    assert len(result) <= 4


def test_discover_samples_returns_strings(tmp_path):
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4")
    result = discover_samples(root=tmp_path, limit=6)
    assert all(isinstance(p, str) for p in result)


def test_discover_samples_skips_bank_statements(tmp_path):
    """Files with 'bank' or 'statement' in the name should be skipped."""
    (tmp_path / "invoice_001.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "bank_statement_jan.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "BankStatement_FY2025.pdf").write_bytes(b"%PDF-1.4")

    result = discover_samples(root=tmp_path, limit=6)
    names = [Path(p).name for p in result]
    assert "invoice_001.pdf" in names
    assert "bank_statement_jan.pdf" not in names
    assert "BankStatement_FY2025.pdf" not in names


def test_discover_samples_empty_dir(tmp_path):
    """No PDFs → empty list, no error."""
    result = discover_samples(root=tmp_path, limit=6)
    assert result == []


def test_discover_samples_nonexistent_root():
    """Non-existent root → empty list, no error."""
    result = discover_samples(root="/nonexistent/path/xyz", limit=6)
    assert result == []
