#!/usr/bin/env python3
"""Verify SG tax export: SR/ZR split (GST-reg) vs No Tax + absorb (non-reg)."""

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

from invoice_processing.export.client_context import ClientContext, CoaAccount
from invoice_processing.export.exporters import QbsLedgerExporter, XeroLedgerExporter
from invoice_processing.export.tax_classifier import TaxClassifier
from invoice_processing.extract.document_extractor import extract_document_file
from invoice_processing.extract.document_normalizer import normalize_document_bundle
from invoice_processing.extract.record_merge import merge_document_records

_HOME = Path.home()
_TEST_ROOT = _HOME / "Desktop/LocalTest/TestDoc"

ACME_CLIENT = ClientContext(
    client_id="acme-client",
    client_name="Acme Client Pte. Ltd.",
    fye_month=3,
    accounting_software="Xero",
    tax_registered=False,
    base_currency="SGD",
    coa=[CoaAccount(code="500", description="Professional Fees", account_type="Expense")],
)

GST_REG = ClientContext(
    client_id="gst-reg-test",
    client_name="GST Reg Test Pte Ltd",
    fye_month=3,
    accounting_software="Xero",
    tax_registered=True,
    base_currency="SGD",
    coa=[CoaAccount(code="6220", description="Telephone", account_type="Expense")],
)


def _discover_telco_pdfs() -> list[Path]:
    candidates = [
        _TEST_ROOT / "GST SR:ZR",
        _TEST_ROOT / "Sample Test Group",
    ]
    out: list[Path] = []
    for base in candidates:
        if not base.is_dir():
            continue
        for pdf in sorted(base.rglob("*.pdf")):
            name = pdf.name.lower()
            if any(k in name for k in ("telco", "m1", "mobile", "broadband", "telecommunications")):
                out.append(pdf)
            elif "gst sr" in str(pdf.parent).lower():
                out.append(pdf)
    seen: set[str] = set()
    unique: list[Path] = []
    for p in out:
        key = str(p)
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique[:3]


def _fixture_pdfs() -> list[tuple[str, ClientContext, Path]]:
    fixtures: list[tuple[str, ClientContext, Path]] = []
    acme_client_dir = (
        _TEST_ROOT / "Sample Test Group/Acme Client Pte. Ltd./Purchase"
    )
    for name in (
        "FY2025/MGT-2025-011-sample.pdf",
        "FY2026/INV-2026-003-sample.pdf",
    ):
        path = acme_client_dir / name
        if path.exists():
            fixtures.append(("acme_client_non_reg", ACME_CLIENT, path))
    for pdf in _discover_telco_pdfs():
        fixtures.append(("telco_gst_reg", GST_REG, pdf))
    telco_sample = _TEST_ROOT / "GST SR:ZR/TELCO-BILL-001-sample.pdf"
    if telco_sample.exists():
        fixtures.insert(0, ("telco_bill_a_sample", GST_REG, telco_sample))
    return fixtures


def _process(path: Path, client: ClientContext) -> dict:
    bundle = merge_document_records(extract_document_file(path))
    invoices = normalize_document_bundle(
        bundle,
        direction="purchase",
        our_gst_registered=client.tax_registered,
        base_currency=client.base_currency,
        client_name=client.client_name,
    )
    tax = TaxClassifier()
    xero = XeroLedgerExporter(tax)
    qbs = QbsLedgerExporter(tax)

    invoice_reports = []
    for inv in invoices:
        for line in inv.lines:
            tax.classify_line(line, inv)
        x_rows = xero.rows([inv], "purchase")
        q_rows = qbs.rows([inv], "purchase")
        invoice_reports.append({
            "invoice_number": inv.invoice_number,
            "our_gst_registered": inv.our_gst_registered,
            "lines": [
                {
                    "description": line.description,
                    "net": line.net_amount,
                    "gst": line.gst_amount,
                    "tax_treatment": line.tax_treatment,
                    "tax_reason": line.tax_reason,
                }
                for line in inv.lines
            ],
            "xero": [
                {
                    "*TaxType": r["*TaxType"],
                    "*UnitAmount": r["*UnitAmount"],
                    "TaxAmount": r["TaxAmount"],
                    "Total": r["Total"],
                }
                for r in x_rows
            ],
            "qbs": [
                {
                    "Sub Total": r["Sub Total"],
                    "Tax Amount": r["Tax Amount"],
                    "Total Amount": r["Total Amount"],
                }
                for r in q_rows
            ],
        })

    return {
        "file": path.name,
        "client": client.client_name,
        "tax_registered": client.tax_registered,
        "invoice_count": len(invoices),
        "invoices": invoice_reports,
    }


def main() -> None:
    fixtures = _fixture_pdfs()
    if not fixtures:
        print(json.dumps({"error": f"No PDFs found under {_TEST_ROOT}"}, indent=2))
        sys.exit(1)

    report = {"fixtures": []}
    for label, client, pdf in fixtures:
        print(f"[{label}] {pdf.name}...", file=sys.stderr)
        try:
            report["fixtures"].append({"label": label, **_process(pdf, client)})
        except Exception as exc:
            report["fixtures"].append({
                "label": label,
                "file": pdf.name,
                "error": str(exc),
            })

    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
