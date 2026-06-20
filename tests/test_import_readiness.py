"""Tests for collect_import_readiness and format_import_readiness_note.

Covers:
- collect_import_readiness for AutoCount purchase export with mixed mapped/unmapped vendors
- format_import_readiness_note rendering
- Regression: QBS/Xero delivery note does NOT contain readiness text
"""

from __future__ import annotations

from datetime import date

from invoice_processing.export.client_context import EntityMemoryEntry
from invoice_processing.export.exporters import (
    AutoCountExporter,
    QbsLedgerExporter,
    SqlAccountExporter,
    XeroLedgerExporter,
    collect_export_unmapped_summary,
    collect_import_readiness,
    format_import_readiness_note,
)
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice, PartyInfo
from invoice_processing.export.tax_classifier import get_tax_classifier

MY_CLF = get_tax_classifier("my_sst.yaml")
SG_CLF = get_tax_classifier("sg_gst.yaml")


def _purchase_inv(
    *,
    inv_date: date = date(2024, 6, 1),
    vendor: str = "Acme Sdn Bhd",
    reg_no: str | None = "123456-A",
    net: float = 1000.0,
    gst: float = 80.0,
    treatment: str = "SR",
    account_code: str = "6100",
    vendor_code: str = "",
) -> NormalizedInvoice:
    inv = NormalizedInvoice(
        doc_type="purchase",
        invoice_number="INV-001",
        invoice_date=inv_date,
        our_gst_registered=True,
        currency="MYR",
        supplier=PartyInfo(name=vendor, gst_regno=reg_no, country="MY", vendor_code=vendor_code),
    )
    inv.lines.append(
        InvoiceLine(
            description="Consulting",
            net_amount=net,
            gst_amount=gst,
            tax_treatment=treatment,
            account_code=account_code,
        )
    )
    return inv


class TestCollectImportReadinessAutoCount:
    def _build_batches(self, exporter, invoices):
        """Run invoices through exporter and return batches list."""
        batches = []
        for inv in invoices:
            rows = exporter.rows([inv], "purchase")
            batches.append({"sheet": "AP Invoice", "rows": rows})
        return batches

    def test_two_vendors_one_mapped_one_not(self):
        """Mapped vendor → party_codes includes their code; unmapped → count >= 1."""
        memory = [
            EntityMemoryEntry(
                name="Acme Sdn Bhd",
                reg_no="123456-A",
                creditor_code="400-G0001",
            )
        ]
        exporter = AutoCountExporter(classifier=MY_CLF)
        exporter.configure_client_context(entity_memory=memory)

        inv_mapped = _purchase_inv(
            vendor="Acme Sdn Bhd",
            reg_no="123456-A",
            gst=80.0,      # 8% → SV-8
        )
        inv_unmapped = _purchase_inv(
            vendor="Unknown Vendor Sdn Bhd",
            reg_no="999-X",
            gst=60.0,      # 6% → SV-6
            account_code="6200",
        )
        batches = self._build_batches(exporter, [inv_mapped, inv_unmapped])
        unmapped = collect_export_unmapped_summary(batches, exporter)
        readiness = collect_import_readiness(batches, exporter, unmapped=unmapped)

        assert readiness["software"] == "AutoCount"
        assert "SV-8" in readiness["tax_codes"]
        assert "SV-6" in readiness["tax_codes"]
        assert "400-G0001" in readiness["party_codes"]
        assert readiness["unmapped"]["count"] >= 1

    def test_account_codes_collected(self):
        """GL account codes from AccNo column are captured."""
        memory = [
            EntityMemoryEntry(
                name="Acme Sdn Bhd",
                reg_no="123456-A",
                creditor_code="400-T0001",
            )
        ]
        exporter = AutoCountExporter(classifier=MY_CLF)
        exporter.configure_client_context(entity_memory=memory)
        inv = _purchase_inv(account_code="610-0000")
        batches = self._build_batches(exporter, [inv])
        readiness = collect_import_readiness(batches, exporter)
        assert "610-0000" in readiness["account_codes"]

    def test_codes_are_sorted(self):
        """Returned code lists are sorted."""
        exporter = AutoCountExporter(classifier=MY_CLF)
        inv_a = _purchase_inv(gst=80.0, account_code="ZZZ")
        inv_b = _purchase_inv(gst=60.0, account_code="AAA", vendor="Other", reg_no="X")
        batches = self._build_batches(exporter, [inv_a, inv_b])
        readiness = collect_import_readiness(batches, exporter)
        assert readiness["tax_codes"] == sorted(readiness["tax_codes"])
        assert readiness["account_codes"] == sorted(readiness["account_codes"])

    def test_sql_account_purchase(self):
        """SQL Account exporter uses CODE(10) for creditor, _ACCOUNT(10) for GL."""
        memory = [
            EntityMemoryEntry(
                name="Acme Sdn Bhd",
                reg_no="123456-A",
                creditor_code="SVE-001",
            )
        ]
        exporter = SqlAccountExporter(classifier=MY_CLF)
        exporter.configure_client_context(entity_memory=memory)
        inv = _purchase_inv(account_code="6100")
        rows = exporter.rows([inv], "purchase")
        batches = [{"sheet": "SLPH_Invoice_Cash_Debit_Credit", "rows": rows}]
        readiness = collect_import_readiness(batches, exporter)
        assert "SVE-001" in readiness["party_codes"]
        assert "6100" in readiness["account_codes"]


class TestCollectImportReadinessNonProfileExporters:
    def test_qbs_exporter_returns_empty(self):
        exporter = QbsLedgerExporter(classifier=SG_CLF)
        readiness = collect_import_readiness(
            [{"sheet": "Purchase", "rows": [{"Account Code": "6100"}]}],
            exporter,
        )
        assert readiness == {}

    def test_xero_exporter_returns_empty(self):
        exporter = XeroLedgerExporter(classifier=SG_CLF)
        readiness = collect_import_readiness(
            [{"sheet": "Purchase", "rows": [{"*AccountCode": "6100"}]}],
            exporter,
        )
        assert readiness == {}


class TestFormatImportReadinessNote:
    def test_renders_all_sections(self):
        readiness = {
            "software": "AutoCount",
            "tax_codes": ["SV-6", "SV-8"],
            "party_codes": ["400-G0001", "400-T0001"],
            "account_codes": ["610-0000"],
            "unmapped": {"count": 2, "details": []},
        }
        note = format_import_readiness_note(readiness)
        assert "AutoCount import" in note
        assert "SV-6" in note
        assert "SV-8" in note
        assert "400-G0001" in note
        assert "610-0000" in note
        assert "⚠️" in note
        assert "2 rows" in note

    def test_no_unmapped_warning_when_count_zero(self):
        readiness = {
            "software": "AutoCount",
            "tax_codes": ["SV-8"],
            "party_codes": ["400-G0001"],
            "account_codes": [],
            "unmapped": {"count": 0, "details": []},
        }
        note = format_import_readiness_note(readiness)
        assert "⚠️" not in note
        assert "SV-8" in note

    def test_returns_empty_for_none(self):
        assert format_import_readiness_note(None) == ""

    def test_returns_empty_for_empty_dict(self):
        assert format_import_readiness_note({}) == ""

    def test_returns_empty_when_no_codes(self):
        readiness = {
            "software": "AutoCount",
            "tax_codes": [],
            "party_codes": [],
            "account_codes": [],
            "unmapped": {"count": 0, "details": []},
        }
        assert format_import_readiness_note(readiness) == ""

    def test_truncates_long_code_list(self):
        many_codes = [f"SV-{i:02d}" for i in range(15)]
        readiness = {
            "software": "SQL Account",
            "tax_codes": many_codes,
            "party_codes": [],
            "account_codes": [],
            "unmapped": {},
        }
        note = format_import_readiness_note(readiness)
        assert "…+" in note

    def test_singular_row_grammar(self):
        readiness = {
            "software": "AutoCount",
            "tax_codes": ["SV-8"],
            "party_codes": [],
            "account_codes": [],
            "unmapped": {"count": 1, "details": []},
        }
        note = format_import_readiness_note(readiness)
        assert "1 row needs" in note


class TestReadinessRegressionQbsXero:
    """QBS and Xero delivery notes must not contain readiness text."""

    def test_qbs_no_readiness_in_note(self):
        readiness = collect_import_readiness(
            [{"sheet": "Purchase", "rows": [{"Account Code / COA": "6100"}]}],
            QbsLedgerExporter(),
        )
        note = format_import_readiness_note(readiness)
        assert note == ""

    def test_xero_no_readiness_in_note(self):
        readiness = collect_import_readiness(
            [{"sheet": "Purchase", "rows": [{"*AccountCode": "ACC001"}]}],
            XeroLedgerExporter(),
        )
        note = format_import_readiness_note(readiness)
        assert note == ""
