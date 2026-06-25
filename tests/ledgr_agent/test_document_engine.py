from datetime import date

from invoice_processing.classify.document_classifier import ClassificationResult
from invoice_processing.export.client_context import ClientContext, CoaAccount
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice, PartyInfo
from invoice_processing.extract.process_invoice_document import InvoiceProcessResult
from ledgr_agent.tools.batch_mapper import map_engine_batch_to_contract
from ledgr_agent.tools.document_engine import process_batch_with_document_spine


def _client() -> ClientContext:
    return ClientContext(
        client_id="jbi-plus-auto",
        client_name="JBI PLUS AUTO SDN BHD",
        region="MALAYSIA",
        accounting_software="qbs",
        base_currency="MYR",
        tax_registered=True,
        fye_month=12,
        coa=[
            CoaAccount(
                code="500-010",
                description="Cost of Sales - Vehicle Parts & Accessories",
            )
        ],
        category_mapping={"vehicle_parts": "500-010"},
    )


def _invoice(number: str, inv_date: date, total: float) -> NormalizedInvoice:
    invoice = NormalizedInvoice(
        doc_type="purchase",
        invoice_number=number,
        invoice_date=inv_date,
        currency="MYR",
        supplier=PartyInfo(name="MYCARKIT ASIA SUPPLY (M) SDN BHD"),
        doc_subtotal=total,
        doc_gst_total=0.0,
        doc_total=total,
        reconciled=True,
        reconcile_note="ok",
        document_kind="invoice",
    )
    invoice.lines.append(
        InvoiceLine(
            description=f"Part for {number}",
            net_amount=total,
            gst_amount=0.0,
            account_code="500-010",
        )
    )
    return invoice


def test_document_spine_fans_out_soa_embedded_invoices(tmp_path) -> None:
    pdf = tmp_path / "mycarkit-soa.pdf"
    pdf.write_bytes(b"%PDF fake")

    def classify_fn(path: str) -> ClassificationResult:
        return ClassificationResult(
            doc_type="statement_of_account",
            confidence=0.99,
            issuer_name="MYCARKIT ASIA SUPPLY (M) SDN BHD",
            bill_to_name="JBI PLUS AUTO SDN BHD",
            reason="test",
        )

    def direction_fn(cls, **kwargs) -> str:
        return "purchase"

    def invoice_process_fn(*args, **kwargs) -> InvoiceProcessResult:
        return InvoiceProcessResult(
            normalized=[
                _invoice("IV-10181", date(2025, 12, 1), 700.0),
                _invoice("IV-10276", date(2025, 12, 8), 510.0),
                _invoice("IV-10390", date(2025, 12, 18), 185.0),
                _invoice("IV-10400", date(2025, 12, 19), 230.0),
            ],
            extraction_path="understand",
            skipped_pages=[1],
            input_page_count=5,
        )

    engine = process_batch_with_document_spine(
        [pdf],
        _client(),
        classify_fn=classify_fn,
        direction_fn=direction_fn,
        invoice_process_fn=invoice_process_fn,
        categorize_fn=lambda *_args, **_kwargs: None,
    )
    batch = map_engine_batch_to_contract(
        engine,
        client=_client(),
        source_files=[str(pdf)],
        missing_files=[],
    )

    assert batch.documents_processed == 4
    assert [doc.normalized.invoice_number for doc in engine.docs] == [
        "IV-10181",
        "IV-10276",
        "IV-10390",
        "IV-10400",
    ]
    assert {row["Invoice Number"] for row in batch.export_rows} == {
        "IV-10181",
        "IV-10276",
        "IV-10390",
        "IV-10400",
    }
