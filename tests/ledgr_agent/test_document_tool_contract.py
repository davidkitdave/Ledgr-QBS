from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from invoice_processing.classify.document_classifier import ClassificationResult
from invoice_processing.export.categorizer import categorize_invoice
from invoice_processing.export.client_context import ClientContext, CoaAccount, EntityMemoryEntry
from invoice_processing.export.routing import route_document
from invoice_processing.extract.invoice_extractor import ExtractedInvoice, ExtractedLine
from invoice_processing.pipeline import BatchResult as EngineBatchResult
from invoice_processing.pipeline import ProcessedDoc, process_document
from ledgr_agent.client_registry import register_client
from ledgr_agent.tools.batch_mapper import determine_batch_status, map_engine_batch_to_contract
from ledgr_agent.tools.document_tools import (
    configure_document_batch_pipeline,
    process_document_batch,
    reset_document_batch_pipeline,
)


@pytest.fixture
def demo_client() -> ClientContext:
    return ClientContext(
        client_id="client_demo",
        client_name="Demo Client Pte Ltd",
        fye_month=12,
        accounting_software="QBS Ledger",
        base_currency="SGD",
        tax_registered=True,
        coa=[
            CoaAccount(
                code="500",
                description="Office Expenses",
                account_type="Expense",
                keywords="office,supplies",
            )
        ],
        entity_memory=[
            EntityMemoryEntry(
                name="Acme Supplier",
                reg_no="200012345A",
                mapping_code="500",
                tax_code="SR",
            )
        ],
    )


@pytest.fixture(autouse=True)
def _reset_pipeline_inject() -> None:
    reset_document_batch_pipeline()
    yield
    reset_document_batch_pipeline()


def _make_cls(doc_type: str = "invoice") -> ClassificationResult:
    return ClassificationResult(
        doc_type=doc_type,
        issuer_name="Acme Supplier",
        bill_to_name="Demo Client Pte Ltd",
        currency="SGD",
        total_amount=109.0,
        confidence=0.99,
        reason="stub",
    )


def _make_extracted_invoice() -> ExtractedInvoice:
    return ExtractedInvoice(
        doc_type="invoice",
        invoice_number="INV-001",
        invoice_date="2025-01-15",
        currency="SGD",
        issuer_name="Acme Supplier",
        issuer_gst_regno="200012345A",
        bill_to_name="Demo Client Pte Ltd",
        lines=[
            ExtractedLine(
                description="Office supplies",
                net_amount=100.0,
                gst_amount=9.0,
                tax_label="SR",
            )
        ],
        subtotal=100.0,
        gst_total=9.0,
        total=109.0,
    )


def _stub_categorize_no_llm(inv, *, coa, category_mapping, entity_memory, **_kw):
    return categorize_invoice(
        inv,
        coa=coa,
        category_mapping=category_mapping,
        entity_memory=entity_memory,
        use_llm=False,
    )


def _configure_hermetic_pipeline() -> None:
    configure_document_batch_pipeline(
        classify_fn=lambda path, **_kw: _make_cls("invoice"),
        direction_fn=lambda cls, **kw: "purchase",
        extract_fn=lambda path, **_kw: _make_extracted_invoice(),
        bank_fn=lambda path, **_kw: (_make_extracted_invoice(), "stub"),
        categorize_fn=_stub_categorize_no_llm,
    )


def test_process_document_batch_blocked_when_client_missing() -> None:
    result = process_document_batch("missing-client", ["/tmp/does-not-exist.pdf"])

    assert result["status"] == "blocked"
    assert result["validation_summary"]["block_reason"] == "client_not_found"
    assert result["llm_call_count"] == 0


def test_process_document_batch_blocked_when_no_files() -> None:
    register_client(
        ClientContext(client_id="empty-files-client", client_name="Empty Files Client")
    )

    result = process_document_batch("empty-files-client", [])

    assert result["status"] == "blocked"
    assert result["validation_summary"]["block_reason"] == "no_source_files"


def test_process_document_batch_hermetic_success(demo_client: ClientContext, tmp_path: Path) -> None:
    register_client(demo_client)
    _configure_hermetic_pipeline()

    doc_path = tmp_path / "invoice.pdf"
    doc_path.write_bytes(b"%PDF stub")

    result = process_document_batch("client_demo", [str(doc_path)])

    assert result["status"] == "success"
    assert result["client_id"] == "client_demo"
    assert result["documents_processed"] == 1
    assert result["documents_skipped_before_llm"] == 0
    assert result["llm_call_count"] == 0
    assert result["credits"]["credit_status"] == "not_checked"
    assert len(result["erp_exports"]) == 1
    assert result["posted_documents"][0]["invoice_number"] == "INV-001"


def test_process_document_batch_skips_missing_files_before_llm(
    demo_client: ClientContext,
    tmp_path: Path,
) -> None:
    register_client(demo_client)
    _configure_hermetic_pipeline()

    doc_path = tmp_path / "invoice.pdf"
    doc_path.write_bytes(b"%PDF stub")
    missing = str(tmp_path / "missing.pdf")

    result = process_document_batch("client_demo", [str(doc_path), missing])

    assert result["status"] == "partial"
    assert result["documents_processed"] == 1
    assert result["documents_skipped_before_llm"] == 1
    assert missing in result["validation_summary"]["missing_files"]


def test_map_engine_batch_marks_review_when_direction_unknown(
    demo_client: ClientContext,
    tmp_path: Path,
) -> None:
    doc_path = tmp_path / "unknown-direction.pdf"
    doc_path.write_bytes(b"%PDF stub")

    doc = process_document(
        doc_path,
        demo_client,
        classify_fn=lambda path, **_kw: _make_cls("invoice"),
        direction_fn=lambda cls, **kw: "unknown",
        extract_fn=lambda path, **_kw: _make_extracted_invoice(),
        bank_fn=lambda path, **_kw: (_make_extracted_invoice(), "stub"),
        categorize_fn=_stub_categorize_no_llm,
    )
    engine_result = EngineBatchResult(workbooks={}, docs=[doc], errors=[])

    batch = map_engine_batch_to_contract(
        engine_result,
        client=demo_client,
        source_files=[str(doc_path)],
        missing_files=[],
    )

    assert batch.status == "needs_review"
    assert batch.review_requests
    assert batch.review_requests[0].severity == "hard_review"


def test_determine_batch_status_partial_on_mixed_errors() -> None:
    ok_doc = ProcessedDoc(
        path="ok.pdf",
        doc_type="invoice",
        direction="purchase",
        normalized=None,
        bank=None,
        route=route_document(
            doc_type="invoice",
            direction="purchase",
            doc_date=date.today(),
            fye_month=12,
            client_id="client_demo",
            filename="ok.pdf",
        ),
        reconciled=True,
        note="ok",
    )
    bad_doc = ProcessedDoc(
        path="bad.pdf",
        doc_type="unknown",
        direction=None,
        normalized=None,
        bank=None,
        route=ok_doc.route,
        reconciled=False,
        note="ERROR: boom",
    )

    status = determine_batch_status(
        blocked_reason=None,
        processed_docs=[ok_doc, bad_doc],
        missing_files=[],
    )

    assert status == "partial"
