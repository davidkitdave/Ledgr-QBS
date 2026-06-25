"""Batch contract tests for large multi-receipt PDF failures (issue #16)."""

from __future__ import annotations

from pathlib import Path

import pytest

from invoice_processing.classify.document_classifier import ClassificationResult
from invoice_processing.extract.process_invoice_document import InvoiceProcessResult
from ledgr_agent.tools import document_tools
from ledgr_agent.tools.document_tools import process_document_batch

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _seed_playground_credits():
    from app.credit_service import CreditService, InMemoryCreditStore

    saved_factory = document_tools._credit_service_factory
    saved_singleton = document_tools._credit_service_singleton
    svc = CreditService(InMemoryCreditStore())
    svc.ensure_firm("T_PLAYGROUND")
    svc.grant("T_PLAYGROUND", 100)
    document_tools._credit_service_factory = lambda: svc
    try:
        yield
    finally:
        document_tools._credit_service_factory = saved_factory
        document_tools._credit_service_singleton = saved_singleton


def _classify_receipt(path, **_kw):
    return ClassificationResult(
        doc_type="receipt",
        confidence=0.99,
        issuer_name="Various",
        bill_to_name="Client",
        reason="test",
    )


def test_empty_extraction_surfaces_actionable_error_not_silent_zero(tmp_path) -> None:
    pdf = tmp_path / "multi.pdf"
    pdf.write_bytes(b"%PDF stub")

    def invoice_process_fn(*_args, **_kwargs) -> InvoiceProcessResult:
        return InvoiceProcessResult(
            normalized=[],
            extraction_path="understand",
            input_page_count=35,
            partial_failure_warnings=[
                "partial extraction: no documents extracted from 35 page(s) — entire file needs review"
            ],
        )

    result = process_document_batch(
        None,
        paths=[str(pdf)],
        classify_fn=_classify_receipt,
        invoice_process_fn=invoice_process_fn,
    )

    assert result["status"] == "error"
    assert result["documents_processed"] == 0
    assert result["credits"]["credits_used"] == 0
    assert result["skipped_documents"]
    note = str(result["skipped_documents"][0].get("note", ""))
    assert "35 page" in note or "no documents extracted" in note.lower()
    assert result.get("fallback_reason")


def test_extraction_exception_surfaces_in_skipped_documents(tmp_path) -> None:
    pdf = tmp_path / "multi.pdf"
    pdf.write_bytes(b"%PDF stub")

    def invoice_process_fn(*_args, **_kwargs):
        raise ValueError("Gemini response truncated (invalid JSON)")

    result = process_document_batch(
        None,
        paths=[str(pdf)],
        classify_fn=_classify_receipt,
        invoice_process_fn=invoice_process_fn,
    )

    assert result["status"] == "error"
    assert result["documents_processed"] == 0
    assert result["credits"]["credits_used"] == 0
    assert result["skipped_documents"]
    assert "ERROR" in str(result["skipped_documents"][0].get("note", ""))
    assert result.get("fallback_reason")


@pytest.mark.integration
def test_large_multi_receipt_pdf_captures_documents() -> None:
    """Live probe against scratch/qa_docs/multi_receipt.pdf (35pp, ~20MB)."""
    pdf = Path("scratch/qa_docs/multi_receipt.pdf")
    if not pdf.is_file():
        pytest.skip("scratch/qa_docs/multi_receipt.pdf not present")

    import os

    if not os.environ.get("GOOGLE_API_KEY"):
        pytest.skip("GOOGLE_API_KEY required for live extraction")

    result = process_document_batch(None, paths=[str(pdf)])

    assert result["credits"]["credits_used"] == 0 or result["documents_processed"] > 0
    assert result["status"] != "error" or result["skipped_documents"], (
        "failed capture must include an actionable skipped_documents entry"
    )
    if result["status"] in {"success", "partial", "needs_review"}:
        assert result["documents_processed"] > 0
