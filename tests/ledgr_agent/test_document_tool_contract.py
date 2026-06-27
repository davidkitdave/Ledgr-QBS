
import pytest

from ledgr_agent.tools import process_document_batch
from ledgr_agent.tools import document_tools


@pytest.fixture(autouse=True)
def _seed_playground_credits():
    """Grant T_PLAYGROUND enough credits for each test and restore factory after."""
    from app.credit_service import CreditService, InMemoryCreditStore

    saved_factory = document_tools._credit_service_factory
    saved_singleton = document_tools._credit_service_singleton
    svc = CreditService(InMemoryCreditStore())
    svc.ensure_firm("T_PLAYGROUND")
    svc.grant("T_PLAYGROUND", 50)
    document_tools._credit_service_factory = lambda: svc
    try:
        yield
    finally:
        document_tools._credit_service_factory = saved_factory
        document_tools._credit_service_singleton = saved_singleton


def test_process_document_batch_light_path(tmp_path) -> None:
    invoice_p = tmp_path / "invoice_test.pdf"
    invoice_p.write_bytes(b"%PDF stub")

    def _bundle_stub(path, **_kw):
        return {
            "documents": [
                {
                    "doc_type": "purchase",
                    "document_kind": "invoice",
                    "vendor_name": "Supplier Inc",
                    "invoice_number": "INV-1234",
                    "invoice_date": "2026-06-24",
                    "currency": "SGD",
                    "grand_total": 109.0,
                    "lines": [
                        {
                            "description": "Office supplies",
                            "net_amount": 100.0,
                            "tax_amount": 9.0,
                            "total_amount": 109.0,
                        }
                    ],
                }
            ],
            "document_count": 1,
            "extraction_meta": {
                "gemini_call_count": 1,
                "model": "gemini-2.5-flash-lite",
            },
        }

    res = process_document_batch(
        None,
        paths=[str(invoice_p)],
        read_bundle_fn=_bundle_stub,
    )

    assert res["status"] == "success"
    assert len(res["client_id"]) > 0
    assert res["documents_requested"] == 1
    assert res["documents_processed"] == 1
    assert len(res["posted_documents"]) == 1
    assert res["posted_documents"][0]["invoice_number"] == "INV-1234"
    assert res["posted_documents"][0]["doc_type"] == "invoice"
    assert res["posted_documents"][0]["path"] == str(invoice_p)
    assert res["llm_call_count"] == 1
    assert res["export_rows"]
