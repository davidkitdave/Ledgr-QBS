"""WS5 — Multi-ERP exporter tests (AutoCount, SQL Account).

Synthetic fixtures only — no real client golden file yet.
"""

from __future__ import annotations

from datetime import date

import pytest

from invoice_processing.export.client_context import EntityMemoryEntry
from invoice_processing.export.code_resolver import (
    resolve_creditor_code,
    resolve_tax_code,
)
from invoice_processing.export.exporters import (
    AutoCountExporter,
    SqlAccountExporter,
    collect_export_unmapped_summary,
    get_exporter,
)
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice, PartyInfo
from invoice_processing.export.tax_classifier import get_tax_classifier


MY_CLF = get_tax_classifier("my_sst.yaml")


def _purchase_inv(
    *,
    inv_date: date,
    vendor: str = "Acme Sdn Bhd",
    reg_no: str | None = "123456-A",
    net: float = 1000.0,
    gst: float = 80.0,
    treatment: str = "SR",
) -> NormalizedInvoice:
    inv = NormalizedInvoice(
        doc_type="purchase",
        invoice_number="INV-001",
        invoice_date=inv_date,
        our_gst_registered=True,
        currency="MYR",
        supplier=PartyInfo(name=vendor, gst_regno=reg_no, country="MY"),
    )
    inv.lines.append(
        InvoiceLine(
            description="Consulting",
            net_amount=net,
            gst_amount=gst,
            tax_treatment=treatment,
            account_code="6100",
        )
    )
    return inv


class TestAutoCountTaxCodes:
    def test_autocount_sv8_after_march_2024(self):
        inv = _purchase_inv(inv_date=date(2024, 6, 1), gst=80.0)
        line = inv.lines[0]
        rate = MY_CLF.standard_rate_for_date(inv.invoice_date)
        code = resolve_tax_code(
            line.tax_treatment,
            rate=rate,
            doc_type="purchase",
            software="autocount",
            client_tax_codes=None,
            classifier=MY_CLF,
        )
        assert code == "SV-8"

    def test_autocount_sv6_before_march_2024(self):
        inv = _purchase_inv(inv_date=date(2023, 6, 1), net=1000.0, gst=60.0)
        line = inv.lines[0]
        rate = MY_CLF.standard_rate_for_date(inv.invoice_date)
        code = resolve_tax_code(
            line.tax_treatment,
            rate=rate,
            doc_type="purchase",
            software="autocount",
            client_tax_codes=None,
            classifier=MY_CLF,
        )
        assert code == "SV-6"


class TestSqlAccountTaxCodes:
    def test_sql_account_flat_sv_code(self):
        inv = _purchase_inv(inv_date=date(2024, 6, 1))
        line = inv.lines[0]
        rate = MY_CLF.standard_rate_for_date(inv.invoice_date)
        code = resolve_tax_code(
            line.tax_treatment,
            rate=rate,
            doc_type="purchase",
            software="sql_account",
            client_tax_codes=None,
            classifier=MY_CLF,
        )
        assert code == "SV"


class TestClientTaxCodeOverride:
    def test_client_tax_codes_override_yaml_seed(self):
        inv = _purchase_inv(inv_date=date(2024, 6, 1))
        line = inv.lines[0]
        rate = MY_CLF.standard_rate_for_date(inv.invoice_date)
        client_codes = [
            {"code": "CUSTOM-8", "description": "Client service tax 8%", "treatment": "SR:0.08"},
        ]
        code = resolve_tax_code(
            line.tax_treatment,
            rate=rate,
            doc_type="purchase",
            software="autocount",
            client_tax_codes=client_codes,
            classifier=MY_CLF,
        )
        assert code == "CUSTOM-8"
        assert code != "SV-8"


class TestCreditorCodeResolution:
    def test_unmapped_vendor_returns_blank_creditor(self):
        memory = [
            EntityMemoryEntry(
                name="Known Vendor",
                reg_no="999",
                mapping_code="6100",
                creditor_code="C001",
            )
        ]
        assert resolve_creditor_code("Mystery Vendor", "111", memory) == ""

    def test_entity_memory_match_returns_creditor_code(self):
        memory = [
            EntityMemoryEntry(
                name="Acme Sdn Bhd",
                reg_no="123456-A",
                mapping_code="6100",
                creditor_code="C-ACME",
            )
        ]
        assert resolve_creditor_code("Acme Sdn Bhd", "123456-A", memory) == "C-ACME"


class TestExporterSelection:
    def test_get_exporter_autocount(self):
        assert isinstance(get_exporter("AutoCount", classifier=MY_CLF), AutoCountExporter)
        assert isinstance(get_exporter("autocount", classifier=MY_CLF), AutoCountExporter)

    def test_get_exporter_sql_account(self):
        assert isinstance(get_exporter("SQL Account", classifier=MY_CLF), SqlAccountExporter)
        assert isinstance(get_exporter("sql_account", classifier=MY_CLF), SqlAccountExporter)


class TestAutoCountExportRows:
    def test_export_row_includes_tax_and_creditor_columns(self):
        inv = _purchase_inv(inv_date=date(2024, 6, 1))
        memory = [
            EntityMemoryEntry(
                name="Acme Sdn Bhd",
                reg_no="123456-A",
                creditor_code="C-ACME",
            )
        ]
        exporter = AutoCountExporter(classifier=MY_CLF)
        exporter.configure_client_context(entity_memory=memory)
        rows = exporter.rows([inv], "purchase")
        assert rows[0]["TaxType"] == "SV-8"
        assert rows[0]["CreditorCode"] == "C-ACME"

    def test_unmapped_vendor_blank_creditor_in_summary(self):
        inv = _purchase_inv(inv_date=date(2024, 6, 1), vendor="Unknown Vendor")
        exporter = AutoCountExporter(classifier=MY_CLF)
        rows = exporter.rows([inv], "purchase")
        assert rows[0]["CreditorCode"] == ""
        summary = collect_export_unmapped_summary(
            [{"sheet": "Purchase", "rows": rows}],
            exporter,
        )
        assert summary["count"] >= 1
        assert any("CreditorCode" in d.get("missing", []) for d in summary["details"])
