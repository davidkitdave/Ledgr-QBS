#!/usr/bin/env python3
"""Generate synthetic eval PDFs and ``ledgr_light_cases.json`` (no golden values)."""

from __future__ import annotations

import base64
import json
import os
import pathlib

from ledgr_agent.eval.minimal_pdf import make_multipage_pdf, make_pdf

_REPO = pathlib.Path(__file__).resolve().parents[2]
_PDF_DIR = pathlib.Path(__file__).resolve().parent / "fixtures" / "pdfs"
_DATASET = pathlib.Path(__file__).resolve().parent / "datasets" / "ledgr_light_cases.json"
_EVALSET = pathlib.Path(__file__).resolve().parent / "datasets" / "ledgr_light.evalset.json"

_PROMPT = "Process this document and build the workbook."


def _save_pdf(
    name: str,
    lines: list[tuple[float, float, str]] | None = None,
    *,
    pages: list[list[tuple[float, float, str]]] | None = None,
) -> pathlib.Path:
    _PDF_DIR.mkdir(parents=True, exist_ok=True)
    path = _PDF_DIR / name
    if pages is not None:
        path.write_bytes(make_multipage_pdf(pages, title=name))
    else:
        path.write_bytes(make_pdf(lines or [], title=name))
    return path


def _b64_pdf(path: pathlib.Path) -> str:
    return base64.standard_b64encode(path.read_bytes()).decode("ascii")


def _case(case_id: str, pdf_path: pathlib.Path) -> dict:
    return {
        "eval_case_id": case_id,
        "prompt": {
            "role": "user",
            "parts": [
                {"text": _PROMPT},
                {
                    "inline_data": {
                        "mime_type": "application/pdf",
                        "data": _b64_pdf(pdf_path),
                    }
                },
            ],
        },
    }


_EVAL_SESSION_STATE = {
    "fye_month": 12,
    "software": "QBS Ledger",
    "firm_id": "T_EVAL",
    "client_id": "eval_client",
}


def _evalset_case(
    case_id: str,
    pdf_path: pathlib.Path,
    *,
    expected_file_kind: str,
    expected_document_kind: str | None = None,
    expected_document_kinds: list[str] | None = None,
    expected_document_count: int | None = None,
    expect_hierarchy_scope: bool = False,
    max_bookable_lines: int | None = None,
    expect_itemized_lines: bool = False,
    min_bookable_lines: int | None = None,
    forbid_document_kinds: list[str] | None = None,
    expect_tax_buckets: bool = False,
) -> dict:
    case: dict = {
        "eval_id": case_id,
        "expected_file_kind": expected_file_kind,
        "session_input": {
            "app_name": "ledgr_agent",
            "user_id": "eval_user",
            "state": dict(_EVAL_SESSION_STATE),
        },
        "conversation": [
            {
                "invocation_id": f"{case_id}-inv-01",
                "user_content": {
                    "role": "user",
                    "parts": [
                        {"text": _PROMPT},
                        {
                            "inline_data": {
                                "mime_type": "application/pdf",
                                "data": _b64_pdf(pdf_path),
                            }
                        },
                    ],
                },
            }
        ],
    }
    if expected_document_kind is not None:
        case["expected_document_kind"] = expected_document_kind
    if expected_document_kinds is not None:
        case["expected_document_kinds"] = list(expected_document_kinds)
    if expected_document_count is not None:
        case["expected_document_count"] = expected_document_count
    if expect_hierarchy_scope:
        case["expect_hierarchy_scope"] = True
    if max_bookable_lines is not None:
        case["max_bookable_lines"] = max_bookable_lines
    if expect_itemized_lines:
        case["expect_itemized_lines"] = True
    if min_bookable_lines is not None:
        case["min_bookable_lines"] = min_bookable_lines
    if forbid_document_kinds:
        case["forbid_document_kinds"] = list(forbid_document_kinds)
    if expect_tax_buckets:
        case["expect_tax_buckets"] = True
    return case


def main() -> None:
    specs: list[tuple[str, str, list[tuple[float, float, str]], str, str | None]] = [
        (
            "sg_gst_invoice_single",
            "sg_gst_invoice_single.pdf",
            [
                (50, 740, "TAX INVOICE"),
                (50, 710, "From: Fictional Supplies Pte Ltd"),
                (50, 690, "GST Reg No: 202000001A"),
                (50, 660, "Bill To: Playground Client"),
                (50, 640, "Invoice No: INV-SG-001"),
                (50, 620, "Date: 2026-03-15"),
                (50, 600, "Currency: SGD"),
                (50, 560, "Description          Net      GST     Total"),
                (50, 540, "Office stationery    100.00   9.00    109.00"),
                (50, 500, "Subtotal: 100.00"),
                (50, 480, "GST 9%: 9.00"),
                (50, 460, "Grand Total: 109.00"),
            ],
            "commercial_documents",
            "invoice",
        ),
        (
            "sg_gst_invoice_multiline",
            "sg_gst_invoice_multiline.pdf",
            [
                (50, 740, "TAX INVOICE"),
                (50, 710, "From: Fictional Supplies Pte Ltd"),
                (50, 690, "GST Reg No: 202000001A"),
                (50, 670, "Bill To: Playground Client"),
                (50, 650, "Invoice No: INV-SG-ML-001"),
                (50, 630, "Date: 2026-03-20"),
                (50, 610, "Currency: SGD"),
                (50, 570, "Item    Qty  Unit    Net"),
                (50, 550, "Pen A   2    10.00   20.00"),
                (50, 530, "Pen B   3    15.00   45.00"),
                (50, 510, "Pad C   1    25.00   25.00"),
                (50, 490, "Clip D  4     5.00   20.00"),
                (50, 470, "Subtotal: 110.00"),
                (50, 450, "GST 9%: 9.90"),
                (50, 430, "Grand Total: 119.90"),
            ],
            "commercial_documents",
            "invoice",
        ),
        (
            "my_sst_invoice_single",
            "my_sst_invoice_single.pdf",
            [
                (50, 740, "INVOICE"),
                (50, 710, "From: Fictional Trading Sdn Bhd"),
                (50, 690, "Bill To: Playground Client"),
                (50, 670, "Invoice No: INV-MY-001"),
                (50, 650, "Date: 2026-04-01"),
                (50, 630, "Currency: MYR"),
                (50, 590, "Consulting services    200.00"),
                (50, 570, "SST 6%: 12.00"),
                (50, 550, "Grand Total: 212.00"),
            ],
            "commercial_documents",
            "invoice",
        ),
        (
            "multi_doc_two_invoices",
            "multi_doc_two_invoices.pdf",
            [
                (50, 740, "TAX INVOICE"),
                (50, 710, "From: Fictional Alpha Pte Ltd"),
                (50, 690, "Invoice No: INV-A-001"),
                (50, 670, "Date: 2026-06-01"),
                (50, 650, "Currency: SGD"),
                (50, 610, "Widget A             80.00"),
                (50, 590, "Grand Total: 80.00"),
                (50, 520, "TAX INVOICE"),
                (50, 490, "From: Fictional Beta Pte Ltd"),
                (50, 470, "Invoice No: INV-B-002"),
                (50, 450, "Date: 2026-06-02"),
                (50, 430, "Currency: SGD"),
                (50, 390, "Widget B             120.00"),
                (50, 370, "Grand Total: 120.00"),
            ],
            "commercial_documents",
            "invoice",
        ),
        (
            "telco_sr_zr_summary",
            "telco_sr_zr_summary.pdf",
            [
                (50, 740, "TAX INVOICE"),
                (50, 710, "From: Fictional Telco Pte Ltd"),
                (50, 690, "GST Reg No: 202000099Z"),
                (50, 670, "Bill To: Playground Client"),
                (50, 650, "Bill No: TEL-SRZR-2026-001"),
                (50, 630, "Date: 2026-06-20"),
                (50, 610, "Currency: SGD"),
                (50, 580, "Charge Summary (reference only)"),
                (50, 560, "Internet Services    800.00"),
                (50, 540, "Mobile Services      400.00"),
                (50, 520, "Subtotal: 1200.00"),
                (50, 490, "Tax Summary:"),
                (50, 470, "  Standard-Rated 9%: taxable 1100.00 tax 99.00"),
                (50, 450, "  Zero-Rated 0%: taxable 100.00 tax 0.00"),
                (50, 430, "GST Total: 99.00"),
                (50, 410, "Grand Total: 1299.00"),
            ],
            "commercial_documents",
            "invoice",
        ),
        (
            "telco_summary_bill",
            "telco_summary_bill.pdf",
            [
                (50, 740, "TAX INVOICE"),
                (50, 710, "From: Fictional Telco Pte Ltd"),
                (50, 690, "GST Reg No: 202000099Z"),
                (50, 670, "Bill To: Playground Client"),
                (50, 650, "Bill No: TEL-2026-001"),
                (50, 630, "Date: 2026-06-15"),
                (50, 610, "Currency: SGD"),
                (50, 570, "Internet Services    169.42"),
                (50, 550, "Mobile Services      1041.93"),
                (50, 530, "Switch Services      12.00"),
                (50, 500, "Subtotal: 1223.35"),
                (50, 480, "GST 9%: 110.10"),
                (50, 460, "Grand Total: 1333.45"),
            ],
            "commercial_documents",
            "invoice",
        ),
        (
            "receipt_single",
            "receipt_single.pdf",
            [
                (50, 740, "RECEIPT"),
                (50, 710, "From: Fictional Cafe Pte Ltd"),
                (50, 690, "Receipt No: RCP-001"),
                (50, 670, "Date: 2026-06-20"),
                (50, 650, "Currency: SGD"),
                (50, 610, "Lunch set meal       18.50"),
                (50, 590, "Total: 18.50"),
            ],
            "commercial_documents",
            "receipt",
        ),
        (
            "bank_statement_single",
            "bank_statement_single.pdf",
            [
                (50, 740, "Bank Statement"),
                (50, 710, "OCBC Bank"),
                (50, 690, "Account No: 072-955554-5"),
                (50, 670, "Currency: SGD"),
                (50, 650, "Period: 01 DEC 2025 - 31 DEC 2025"),
                (50, 620, "Opening Balance: 1000.00"),
                (50, 590, "Date        Description              Withdrawal  Deposit   Balance"),
                (50, 570, "01 Dec 2025 Transfer In                          500.00    1500.00"),
                (50, 550, "02 Dec 2025 GIRO Payment              200.00              1300.00"),
                (50, 530, "03 Dec 2025 ATM Withdrawal            100.00              1200.00"),
                (50, 510, "04 Dec 2025 Interest Credit                      12.50     1212.50"),
                (50, 480, "Closing Balance: 1212.50"),
            ],
            "bank_statement",
            None,
        ),
        (
            "sg_invoice_sr_zr_split",
            "sg_invoice_sr_zr_split.pdf",
            [
                (50, 740, "TAX INVOICE"),
                (50, 710, "From: Fictional Mixed Pte Ltd"),
                (50, 690, "GST Reg No: 202000002B"),
                (50, 670, "Bill To: Playground Client"),
                (50, 650, "Invoice No: INV-SG-SRZR-001"),
                (50, 630, "Date: 2026-06-25"),
                (50, 610, "Currency: SGD"),
                (50, 570, "Description          Net      GST     Treatment"),
                (50, 550, "Consulting SR        100.00   9.00    Standard-Rated 9%"),
                (50, 530, "Export ZR            50.00    0.00    Zero-Rated 0%"),
                (50, 500, "Subtotal: 150.00"),
                (50, 480, "Tax Summary:"),
                (50, 460, "  Standard-Rated 9%: taxable 100.00 tax 9.00"),
                (50, 440, "  Zero-Rated 0%: taxable 50.00 tax 0.00"),
                (50, 420, "GST Total: 9.00"),
                (50, 400, "Grand Total: 159.00"),
            ],
            "commercial_documents",
            "invoice",
        ),
    ]

    agents_cli_cases: list[dict] = []
    evalset_cases: list[dict] = []
    itemized_single_line = {"sg_gst_invoice_single", "my_sst_invoice_single", "receipt_single"}
    for case_id, filename, lines, expected_file_kind, expected_document_kind in specs:
        pdf_path = _save_pdf(filename, lines)
        agents_cli_cases.append(_case(case_id, pdf_path))
        eval_kwargs: dict = {
            "expected_file_kind": expected_file_kind,
            "expected_document_kind": expected_document_kind,
        }
        if case_id == "sg_invoice_sr_zr_split":
            eval_kwargs["expect_itemized_lines"] = True
            eval_kwargs["min_bookable_lines"] = 2
        elif case_id == "sg_gst_invoice_multiline":
            eval_kwargs["expect_itemized_lines"] = True
            eval_kwargs["min_bookable_lines"] = 4
        elif case_id in itemized_single_line:
            eval_kwargs["expect_itemized_lines"] = True
            eval_kwargs["min_bookable_lines"] = 1
        elif case_id == "multi_doc_two_invoices":
            eval_kwargs["expected_document_count"] = 2
        elif case_id == "telco_sr_zr_summary":
            eval_kwargs["expect_hierarchy_scope"] = True
            eval_kwargs["expect_tax_buckets"] = True
            eval_kwargs["max_bookable_lines"] = 3
            eval_kwargs["min_bookable_lines"] = 2
        evalset_cases.append(_evalset_case(case_id, pdf_path, **eval_kwargs))

    # Multipage telco bill: summary page + detail appendix pages (trap for over-extraction).
    telco_pages = [
        [
            (50, 740, "TAX INVOICE"),
            (50, 710, "From: Fictional Telco Pte Ltd"),
            (50, 690, "GST Reg No: 202000099Z"),
            (50, 670, "Bill To: Playground Client"),
            (50, 650, "Bill No: TEL-2026-MP-001"),
            (50, 630, "Date: 2026-06-15"),
            (50, 610, "Currency: SGD"),
            (50, 580, "Charge Summary"),
            (50, 560, "Internet Services    169.42"),
            (50, 540, "Mobile Services      1041.93"),
            (50, 520, "Switch Services      12.00"),
            (50, 490, "Subtotal: 1223.35"),
            (50, 470, "GST 9%: 110.10"),
            (50, 450, "Grand Total: 1333.45"),
            (50, 420, "See appendix pages for usage details."),
        ],
    ]
    for page_num in (2, 3):
        detail_lines: list[tuple[float, float, str]] = [
            (50, 740, f"Usage Details — Page {page_num}"),
            (50, 710, "Date        Time     Number          Duration  Charge"),
        ]
        y = 690
        for row in range(1, 26):
            detail_lines.append(
                (50, y, f"01 Jun 2026  10:{row:02d}  +659000{row:04d}  00:05:00  4.{row:02d}")
            )
            y -= 18
        telco_pages.append(detail_lines)

    telco_mp_path = _save_pdf("telco_multipage_bill.pdf", pages=telco_pages)
    agents_cli_cases.append(_case("telco_multipage_bill", telco_mp_path))
    evalset_cases.append(
        _evalset_case(
            "telco_multipage_bill",
            telco_mp_path,
            expected_file_kind="commercial_documents",
            expected_document_kind="invoice",
            expect_hierarchy_scope=True,
            max_bookable_lines=3,
        )
    )

    # Generic hierarchy trap (non-telco): summary charges + freight detail appendix.
    utility_pages = [
        [
            (50, 740, "TAX INVOICE"),
            (50, 710, "From: Fictional Utilities Pte Ltd"),
            (50, 690, "Bill To: Playground Client"),
            (50, 670, "Invoice No: UTIL-2026-001"),
            (50, 650, "Date: 2026-06-18"),
            (50, 630, "Currency: SGD"),
            (50, 590, "Charge Summary"),
            (50, 570, "Electricity Supply     450.00"),
            (50, 550, "Water Supply           120.00"),
            (50, 530, "Waste Collection        30.00"),
            (50, 500, "Subtotal: 600.00"),
            (50, 480, "GST 9%: 54.00"),
            (50, 460, "Grand Total: 654.00"),
            (50, 430, "See appendix for meter readings."),
        ],
        [
            (50, 740, "Meter Reading Details"),
            (50, 710, "Date        Meter ID    kWh    Charge"),
        ]
        + [
            (50, 690 - (i * 18), f"01 Jun 2026  MTR-{i:04d}  {10 + i}.0  {5 + i}.00")
            for i in range(1, 26)
        ],
    ]
    utility_path = _save_pdf("utility_hierarchy_bill.pdf", pages=utility_pages)
    agents_cli_cases.append(_case("utility_hierarchy_bill", utility_path))
    evalset_cases.append(
        _evalset_case(
            "utility_hierarchy_bill",
            utility_path,
            expected_file_kind="commercial_documents",
            expected_document_kind="invoice",
            expect_hierarchy_scope=True,
            max_bookable_lines=5,
        )
    )
    for idx, case in enumerate(evalset_cases):
        if case.get("eval_id") == "telco_summary_bill":
            evalset_cases[idx] = {
                **case,
                "expect_hierarchy_scope": True,
                "max_bookable_lines": 3,
            }
            break

    standalone_soa_path = _save_pdf(
        "standalone_soa.pdf",
        [
            (50, 740, "DEBTOR STATEMENT"),
            (50, 710, "From: Fictional Supplies Sdn Bhd"),
            (50, 690, "Debtor: Fictional Buyer Sdn Bhd"),
            (50, 670, "Account No: SOA-2026-001"),
            (50, 640, "Currency: MYR"),
            (50, 600, "Ref       Amount"),
            (50, 580, "IA-001    100.00"),
            (50, 560, "IA-002    250.00"),
            (50, 540, "IA-003    150.00"),
            (50, 500, "Balance Due: 500.00"),
        ],
    )
    agents_cli_cases.append(_case("standalone_soa", standalone_soa_path))
    evalset_cases.append(
        _evalset_case(
            "standalone_soa",
            standalone_soa_path,
            expected_file_kind="commercial_documents",
            expected_document_kind="statement_of_account",
            expected_document_count=1,
        )
    )

    def _invoice_page(
        vendor: str,
        invoice_no: str,
        date: str,
        description: str,
        net: str,
        tax: str,
        total: str,
    ) -> list[tuple[float, float, str]]:
        return [
            (50, 740, "INVOICE"),
            (50, 710, f"From: {vendor}"),
            (50, 690, "Bill To: Fictional Buyer Sdn Bhd"),
            (50, 670, f"Invoice No: {invoice_no}"),
            (50, 650, f"Date: {date}"),
            (50, 630, "Currency: MYR"),
            (50, 590, f"{description}    {net}"),
            (50, 570, f"SST 6%: {tax}"),
            (50, 550, f"Grand Total: {total}"),
        ]

    def _multiline_invoice_page(
        vendor: str,
        invoice_no: str,
        date: str,
        items: list[tuple[str, str, str, str]],
        subtotal: str,
        tax: str,
        total: str,
    ) -> list[tuple[float, float, str]]:
        page: list[tuple[float, float, str]] = [
            (50, 740, "INVOICE"),
            (50, 710, f"From: {vendor}"),
            (50, 690, "Bill To: Fictional Buyer Sdn Bhd"),
            (50, 670, f"Invoice No: {invoice_no}"),
            (50, 650, f"Date: {date}"),
            (50, 630, "Currency: MYR"),
            (50, 600, "Item    Qty  Unit    Net"),
        ]
        y = 580
        for item, qty, unit, net in items:
            page.append((50, y, f"{item}  {qty}  {unit}  {net}"))
            y -= 20
        page.extend(
            [
                (50, y - 10, f"Subtotal: {subtotal}"),
                (50, y - 30, f"SST 6%: {tax}"),
                (50, y - 50, f"Grand Total: {total}"),
            ]
        )
        return page

    soa_plus_pages = [
        [
            (50, 740, "DEBTOR STATEMENT"),
            (50, 710, "From: Fictional Supplies Sdn Bhd"),
            (50, 690, "Debtor: Fictional Buyer Sdn Bhd"),
            (50, 670, "Statement Date: 2026-02-01"),
            (50, 640, "Currency: MYR"),
            (50, 600, "Date        Ref       Description    DR       Balance"),
            (50, 580, "01/02/2026  IA-100    INVOICE        100.00   100.00"),
            (50, 560, "02/02/2026  IA-101    INVOICE        200.00   300.00"),
            (50, 540, "03/02/2026  IA-102    INVOICE        150.00   450.00"),
            (50, 500, "Balance Due: 450.00"),
            (50, 470, "See attached pages for full invoices."),
        ],
        _invoice_page(
            "Fictional Supplies Sdn Bhd",
            "IA-100",
            "2026-02-01",
            "Widget A",
            "100.00",
            "6.00",
            "106.00",
        ),
        _invoice_page(
            "Fictional Supplies Sdn Bhd",
            "IA-101",
            "2026-02-02",
            "Widget B",
            "200.00",
            "12.00",
            "212.00",
        ),
        _invoice_page(
            "Fictional Supplies Sdn Bhd",
            "IA-102",
            "2026-02-03",
            "Widget C",
            "150.00",
            "9.00",
            "159.00",
        ),
    ]
    soa_plus_path = _save_pdf("soa_plus_invoices.pdf", pages=soa_plus_pages)
    agents_cli_cases.append(_case("soa_plus_invoices", soa_plus_path))
    evalset_cases.append(
        _evalset_case(
            "soa_plus_invoices",
            soa_plus_path,
            expected_file_kind="commercial_documents",
            expected_document_kind="invoice",
            expected_document_count=3,
            forbid_document_kinds=["statement_of_account"],
        )
    )

    soa_ml_pages = [
        [
            (50, 740, "DEBTOR STATEMENT"),
            (50, 710, "From: Fictional Parts Sdn Bhd"),
            (50, 690, "Debtor: Fictional Buyer Sdn Bhd"),
            (50, 670, "Statement Date: 2026-03-01"),
            (50, 640, "Currency: MYR"),
            (50, 600, "Ref       Amount"),
            (50, 580, "IA-200    300.00"),
            (50, 560, "IA-201    450.00"),
            (50, 500, "Balance Due: 750.00"),
            (50, 470, "See attached pages for full invoices."),
        ],
        _multiline_invoice_page(
            "Fictional Parts Sdn Bhd",
            "IA-200",
            "2026-03-01",
            [
                ("Widget X", "2", "50.00", "100.00"),
                ("Widget Y", "1", "80.00", "80.00"),
                ("Widget Z", "3", "40.00", "120.00"),
            ],
            "300.00",
            "18.00",
            "318.00",
        ),
        _multiline_invoice_page(
            "Fictional Parts Sdn Bhd",
            "IA-201",
            "2026-03-02",
            [
                ("Cable A", "5", "30.00", "150.00"),
                ("Cable B", "2", "75.00", "150.00"),
                ("Cable C", "1", "150.00", "150.00"),
            ],
            "450.00",
            "27.00",
            "477.00",
        ),
    ]
    soa_ml_path = _save_pdf("soa_plus_multiline_invoices.pdf", pages=soa_ml_pages)
    agents_cli_cases.append(_case("soa_plus_multiline_invoices", soa_ml_path))
    evalset_cases.append(
        _evalset_case(
            "soa_plus_multiline_invoices",
            soa_ml_path,
            expected_file_kind="commercial_documents",
            expected_document_kind="invoice",
            expected_document_count=2,
            expect_itemized_lines=True,
            min_bookable_lines=3,
            forbid_document_kinds=["statement_of_account"],
        )
    )

    soa_cn_pages = [
        [
            (50, 740, "DEBTOR STATEMENT"),
            (50, 710, "From: Fictional Audio Sdn Bhd"),
            (50, 690, "Debtor: Fictional Buyer Sdn Bhd"),
            (50, 670, "Statement Date: 2026-04-01"),
            (50, 640, "Currency: MYR"),
            (50, 600, "Ref       Amount"),
            (50, 580, "IA-300    200.00"),
            (50, 560, "CNA-01   -50.00"),
            (50, 500, "Balance Due: 150.00"),
        ],
        _multiline_invoice_page(
            "Fictional Audio Sdn Bhd",
            "IA-300",
            "2026-04-01",
            [
                ("Speaker A", "1", "100.00", "100.00"),
                ("Speaker B", "2", "50.00", "100.00"),
            ],
            "200.00",
            "12.00",
            "212.00",
        ),
        [
            (50, 740, "CREDIT NOTE"),
            (50, 710, "From: Fictional Audio Sdn Bhd"),
            (50, 690, "Bill To: Fictional Buyer Sdn Bhd"),
            (50, 670, "Credit Note No: CNA-01"),
            (50, 650, "Date: 2026-04-02"),
            (50, 630, "Currency: MYR"),
            (50, 600, "Item    Qty  Unit    Net"),
            (50, 580, "Speaker C  1  50.00  50.00"),
            (50, 560, "Subtotal: 50.00"),
            (50, 540, "SST 6%: 0.00"),
            (50, 520, "Grand Total: 50.00"),
        ],
    ]
    soa_cn_path = _save_pdf("soa_plus_credit_note.pdf", pages=soa_cn_pages)
    agents_cli_cases.append(_case("soa_plus_credit_note", soa_cn_path))
    evalset_cases.append(
        _evalset_case(
            "soa_plus_credit_note",
            soa_cn_path,
            expected_file_kind="commercial_documents",
            expected_document_kinds=["invoice", "credit_note"],
            expected_document_count=2,
            expect_itemized_lines=True,
            min_bookable_lines=1,
            forbid_document_kinds=["statement_of_account"],
        )
    )

    bank_reverse_lines = [
        (50, 740, "Bank Statement"),
        (50, 710, "CIMB Bank"),
        (50, 690, "Account No: 8001234567"),
        (50, 670, "Currency: SGD"),
        (50, 650, "Period: 01 JAN 2025 - 31 JAN 2025"),
        (50, 620, "Opening Balance: 1000.00"),
        (50, 590, "Date        Description              Withdrawal  Deposit   Balance"),
        (50, 570, "04 Jan 2025 Interest Credit                      12.50     1212.50"),
        (50, 550, "03 Jan 2025 ATM Withdrawal            100.00              1200.00"),
        (50, 530, "02 Jan 2025 GIRO Payment              200.00              1300.00"),
        (50, 510, "01 Jan 2025 Transfer In                          500.00    1500.00"),
        (50, 480, "Closing Balance: 1212.50"),
    ]
    bank_reverse_path = _save_pdf("bank_reverse_chron.pdf", bank_reverse_lines)
    agents_cli_cases.append(_case("bank_reverse_chron", bank_reverse_path))
    evalset_cases.append(
        _evalset_case(
            "bank_reverse_chron",
            bank_reverse_path,
            expected_file_kind="bank_statement",
        )
    )

    multi_receipt_pages: list[list[tuple[float, float, str]]] = []
    for page_idx in range(1, 9):
        base_y = 740
        page_lines: list[tuple[float, float, str]] = [
            (50, base_y, f"RECEIPT BUNDLE — Page {page_idx}"),
            (50, base_y - 30, "From: Fictional Retail Pte Ltd"),
        ]
        for receipt_idx in range(1, 7):
            y = base_y - 60 - (receipt_idx * 90)
            rid = (page_idx - 1) * 6 + receipt_idx
            page_lines.extend(
                [
                    (50, y, f"Receipt No: RCP-{rid:04d}"),
                    (50, y - 18, f"Date: 2026-06-{min(rid, 28):02d}"),
                    (50, y - 36, f"Item purchase #{rid}    {10 + rid}.50"),
                    (50, y - 54, f"Total: {10 + rid}.50"),
                ]
            )
        multi_receipt_pages.append(page_lines)
    multi_receipt_path = _save_pdf("multi_receipt_large.pdf", pages=multi_receipt_pages)
    agents_cli_cases.append(_case("multi_receipt_large", multi_receipt_path))
    evalset_cases.append(
        _evalset_case(
            "multi_receipt_large",
            multi_receipt_path,
            expected_file_kind="commercial_documents",
            expected_document_kind="receipt",
            expected_document_count=48,
        )
    )

    starhub_dir = os.environ.get("LEDGR_TEST_DOC_DIR")
    if starhub_dir:
        starhub_path = pathlib.Path(starhub_dir) / "BV-0002830 Starhub 8.20057598B bill 122025.pdf"
        if starhub_path.is_file():
            agents_cli_cases.append(_case("starhub_real", starhub_path))
            evalset_cases.append(
                _evalset_case(
                    "starhub_real",
                    starhub_path,
                    expected_file_kind="commercial_documents",
                    expected_document_kind="invoice",
                    expect_hierarchy_scope=True,
                    expect_tax_buckets=True,
                    max_bookable_lines=3,
                    min_bookable_lines=2,
                )
            )

    _DATASET.parent.mkdir(parents=True, exist_ok=True)
    _DATASET.write_text(
        json.dumps({"eval_cases": agents_cli_cases}, indent=2) + "\n",
        encoding="utf-8",
    )
    _EVALSET.write_text(
        json.dumps(
            {
                "eval_set_id": "ledgr_light_v1",
                "name": "Ledgr light agent — reference-free financial doc eval",
                "description": (
                    "Synthetic fictional invoices, receipts, and bank statements. "
                    "No golden/reference fields."
                ),
                "eval_cases": evalset_cases,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(agents_cli_cases)} cases to {_DATASET} and {_EVALSET}")


if __name__ == "__main__":
    main()
