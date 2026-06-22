#!/usr/bin/env python3
"""Full local path: extract → normalize → tax → categorize → QBS Ledger rows + completeness."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

if "GOOGLE_GENAI_USE_VERTEXAI" not in os.environ:
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "FALSE"

from eval.ledger_eval import TargetCompleteness, _tally_completeness
from invoice_processing.export.categorizer import categorize_invoice
from invoice_processing.export.client_context import ClientContext, CoaAccount
from invoice_processing.export.exporters import QbsLedgerExporter, validate_required_fields
from invoice_processing.export.tax_classifier import TaxClassifier
from invoice_processing.extract.process_invoice_document import process_invoice_document

SAMPLE_TEST_CLIENT_CTX = ClientContext(
    client_id="company-a",
    client_name="Company-A",
    fye_month=3,
    accounting_software="QBS Ledger",
    tax_registered=True,
    base_currency="SGD",
    coa=[
        CoaAccount(code="500", description="Professional Fees", account_type="Expense"),
        CoaAccount(code="510", description="Travel & Entertainment", account_type="Expense"),
    ],
)

SAMPLE_TEST_VENDOR = ClientContext(
    client_id="company-b",
    client_name="Company-B",
    fye_month=12,
    accounting_software="QBS Ledger",
    tax_registered=True,
    base_currency="MYR",
    coa=[
        CoaAccount(code="600", description="Purchases", account_type="Expense"),
        CoaAccount(code="610", description="Motor Vehicle Expenses", account_type="Expense"),
    ],
)

_HOME = Path.home()
FIXTURES = [
    {
        "label": "sample_test_group",
        "client": SAMPLE_TEST_CLIENT_CTX,
        "pdfs": [
            _HOME / "Desktop/LocalTest/TestDoc/Sample Test Group/Company-A/Purchase/FY2026/INV-2026-003-sample.pdf",
            _HOME / "Desktop/LocalTest/TestDoc/Sample Test Group/Company-A/Purchase/FY2025/INV-2025-012-sample.pdf",
            _HOME / "Desktop/LocalTest/TestDoc/Sample Test Group/Company-A/Purchase/FY2025/MGT-2025-011-sample.pdf",
            _HOME / "Desktop/LocalTest/TestDoc/Sample Test Group/Company-A/Purchase/FY2026/EXP-2026-040-sample.pdf",
        ],
    },
    {
        "label": "sample_vendor_soa",
        "client": SAMPLE_TEST_VENDOR,
        "pdfs": [
            _HOME / "Desktop/LocalTest/TestDoc/MYDoc/Company-B/Purchase/SOA-SAMPLE-DEC-2025_.pdf",
        ],
    },
]


def _coa_args(client: ClientContext) -> dict:
    return {
        "coa": client.coa,
        "category_mapping": client.category_mapping,
        "entity_memory": client.entity_memory,
    }


def process_pdf(path: Path, client: ClientContext) -> dict:
    data = path.read_bytes()
    result = process_invoice_document(
        data,
        "application/pdf",
        direction="purchase",
        our_gst_registered=client.tax_registered,
        base_currency=client.base_currency,
        client_name=client.client_name,
    )
    invoices = result.normalized

    tax = TaxClassifier()
    exporter = QbsLedgerExporter()
    completeness = TargetCompleteness(target=exporter.software_name)
    all_rows: list[dict] = []
    invoice_summaries: list[dict] = []

    for inv in invoices:
        for line in inv.lines:
            tax.classify_line(line, inv)
        categorize_invoice(inv, **_coa_args(client))
        missing = validate_required_fields(inv, exporter, "purchase")
        rows = exporter.rows([inv], "purchase")
        all_rows.extend(rows)
        _tally_completeness(completeness, exporter, inv, "purchase")
        invoice_summaries.append({
            "invoice_number": inv.invoice_number,
            "invoice_date": str(inv.invoice_date) if inv.invoice_date else None,
            "currency": inv.currency,
            "doc_total": inv.doc_total,
            "reconciled": inv.reconciled,
            "needs_fx_review": inv.needs_fx_review,
            "line_count": len(inv.lines),
            "missing_required": missing,
            "sample_row": rows[0] if rows else None,
        })

    header_rates = {
        h: {"filled": hf.filled, "total": hf.total, "rate": round(hf.rate * 100, 1)}
        for h, hf in completeness.headers.items()
    }

    return {
        "file": path.name,
        "phase1_documents": len(bundle.documents),
        "invoice_count": len(invoices),
        "qbs_row_count": len(all_rows),
        "qbs_columns": exporter.purchase_cols,
        "header_completeness": header_rates,
        "invoices": invoice_summaries,
        "qbs_rows": all_rows,
    }


def main() -> None:
    report: dict = {"fixtures": []}
    for group in FIXTURES:
        group_out = {"label": group["label"], "client": group["client"].client_name, "files": []}
        for pdf in group["pdfs"]:
            pdf = Path(pdf).expanduser()
            if not pdf.exists():
                group_out["files"].append({"file": pdf.name, "error": f"not found: {pdf}"})
                continue
            print(f"Processing {pdf.name}...", file=sys.stderr)
            try:
                group_out["files"].append(process_pdf(pdf, group["client"]))
            except Exception as exc:
                group_out["files"].append({"file": pdf.name, "error": str(exc)})
        report["fixtures"].append(group_out)

    # Trim full rows in stdout summary — keep first 5 rows per file for readability
    summary = json.loads(json.dumps(report, default=str))
    for group in summary["fixtures"]:
        for f in group["files"]:
            rows = f.pop("qbs_rows", [])
            f["qbs_row_preview"] = rows[:5]
            f["qbs_rows_total"] = len(rows)

    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
