
from invoice_processing.classify.document_classifier import ClassificationResult
from invoice_processing.extract.invoice_extractor import ExtractedInvoice
from ledgr_agent.tools import process_document_batch


def _make_cls(doc_type: str) -> ClassificationResult:
    return ClassificationResult(
        doc_type=doc_type,
        confidence=0.99,
        issuer_name="Supplier Inc",
        bill_to_name="Playground Client",
        reason="test",
    )


def test_process_document_batch_converts_engine_output(tmp_path) -> None:
    invoice_p = tmp_path / "invoice_test.pdf"
    invoice_p.write_bytes(b"%PDF stub")

    def _classify(path, **_kw):
        return _make_cls("invoice")

    def _direction(cls, **_kw):
        return "purchase"

    def _extract_stub(path, **_kw):
        from invoice_processing.extract.invoice_extractor import ExtractedLine
        return ExtractedInvoice(
            doc_type="invoice",
            invoice_number="INV-1234",
            invoice_date="2026-06-24",
            currency="SGD",
            issuer_name="Supplier Inc",
            issuer_gst_regno="200012345A",
            bill_to_name="Playground Client",
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
            issuer_tax_system="NONE",
        )

    def stub_cat(inv, **kw):
        if inv.lines:
            inv.lines[0].account_code = "6100"

    # Run the wrapper tool
    res = process_document_batch(
        None,  # tool_context: triggers fallback to playground default
        paths=[str(invoice_p)],
        classify_fn=_classify,
        direction_fn=_direction,
        extract_fn=_extract_stub,
        categorize_fn=stub_cat,
    )

    # Assert Pydantic BatchResult properties in return payload
    assert res["status"] == "success"
    assert len(res["client_id"]) > 0
    assert res["documents_requested"] == 1
    assert res["documents_processed"] == 1
    assert len(res["posted_documents"]) == 1
    assert res["posted_documents"][0]["invoice_number"] == "INV-1234"
    assert res["posted_documents"][0]["doc_type"] == "invoice"
    assert res["posted_documents"][0]["path"] == str(invoice_p)
    assert res["llm_call_count"] == 3
