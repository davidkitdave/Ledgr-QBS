from ledgr_agent.tools.document_truth import document_truth_report, expected_invoices_from_text


MYCARKIT_TEXT = """
Statement of Account
-- 1 of 5 --
IV-10181 INVOICE
Date
01/12/2025
No Description Qty Item Code Discount Price/Unit Amount
210.00 B F30 MP FRONT LIP WITH PAINT 2.00 UNIT 420.00 1 BFLF30MP-WP
140.00 B E39 M5 SPOILER -AGB 2.00 UNIT 280.00 2 BSPE39-M5
Total (RM) 700.00
-- 2 of 5 --
IV-10276 INVOICE
Date
08/12/2025
Total (RM) 510.00
-- 3 of 5 --
IV-10390 INVOICE
Date
18/12/2025
Total (RM) 185.00
-- 4 of 5 --
IV-10400 INVOICE
Date
19/12/2025
Total (RM) 230.00
"""


def test_expected_invoices_from_text_finds_all_embedded_invoices() -> None:
    invoices = expected_invoices_from_text(MYCARKIT_TEXT)

    assert [item["invoice_number"] for item in invoices] == [
        "IV-10181",
        "IV-10276",
        "IV-10390",
        "IV-10400",
    ]
    assert [item["total"] for item in invoices] == [700.0, 510.0, 185.0, 230.0]


def test_document_truth_report_fails_when_export_rows_cover_only_one_invoice(
    tmp_path,
    monkeypatch,
) -> None:
    pdf = tmp_path / "mycarkit.pdf"
    pdf.write_bytes(b"%PDF fake")
    monkeypatch.setattr(
        "ledgr_agent.tools.document_truth.pdf_text",
        lambda _path: MYCARKIT_TEXT,
    )

    report = document_truth_report(
        [pdf],
        [
            {
                "Invoice Number": "IV-10181",
                "Source Amount": 420.0,
            },
            {
                "Invoice Number": "IV-10181",
                "Source Amount": 280.0,
            },
        ],
    )

    assert report["status"] == "fail"
    assert report["expected_invoice_count"] == 4
    assert report["exported_invoice_count"] == 1
    assert report["missing_invoice_numbers"] == ["IV-10276", "IV-10390", "IV-10400"]
    assert report["expected_total_amount"] == 1625.0
    assert report["exported_total_amount"] == 700.0
    assert report["amount_coverage"] == 0.4308
