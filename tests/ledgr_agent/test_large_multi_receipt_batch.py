"""Batch contract tests for large multi-receipt PDF failures (issue #16)."""

from __future__ import annotations

import pytest

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


def test_empty_extraction_surfaces_actionable_error_not_silent_zero(tmp_path) -> None:
    pdf = tmp_path / "multi.pdf"
    pdf.write_bytes(b"%PDF stub")

    def read_bundle_fn(_path, **_kw):
        return {
            "documents": [],
            "document_count": 0,
            "extraction_meta": {"gemini_call_count": 1, "model": "gemini-2.5-flash-lite"},
        }

    result = process_document_batch(
        None,
        paths=[str(pdf)],
        read_bundle_fn=read_bundle_fn,
    )

    assert result["status"] == "error"
    assert result["documents_processed"] == 0
    assert result["credits"]["credits_used"] == 0
    assert result["skipped_documents"]
    note = str(result["skipped_documents"][0].get("note", ""))
    assert "no documents extracted" in note.lower()
    assert result.get("fallback_reason")


def test_extraction_exception_surfaces_in_skipped_documents(tmp_path) -> None:
    pdf = tmp_path / "multi.pdf"
    pdf.write_bytes(b"%PDF stub")

    def read_bundle_fn(_path, **_kw):
        return {"status": "error", "message": "Gemini response truncated (invalid JSON)"}

    result = process_document_batch(
        None,
        paths=[str(pdf)],
        read_bundle_fn=read_bundle_fn,
    )

    assert result["status"] == "error"
    assert result["documents_processed"] == 0
    assert result["credits"]["credits_used"] == 0
    assert result["skipped_documents"]
    assert "ERROR" in str(result["skipped_documents"][0].get("note", ""))
    assert result.get("fallback_reason")
