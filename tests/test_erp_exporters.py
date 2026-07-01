"""WS5 — Multi-ERP exporter tests (AutoCount, SQL Account).

Synthetic fixtures only — no real client golden file yet.

Not on the live Slack hot path (light path uses ledgr_agent/internal/export.py).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.legacy

from datetime import date


from ledgr_slack.client_context import EntityMemoryEntry
from ledgr_slack.export.code_resolver import (
    resolve_creditor_code,
    resolve_tax_code,
)
from ledgr_slack.export.exporters import UNMAPPED_ACCOUNT_CODE
from ledgr_slack.export.exporters import (
    AutoCountExporter,
    QbsLedgerExporter,
    SqlAccountExporter,
    XeroLedgerExporter,
    collect_export_unmapped_summary,
    get_exporter,
    validate_export_account_code,
)
from ledgr_slack.export.models import InvoiceLine, NormalizedInvoice, PartyInfo
from ledgr_slack.export.tax_classifier import get_tax_classifier


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

    def test_empty_client_tax_list_returns_blank_not_yaml(self):
        inv = _purchase_inv(inv_date=date(2024, 6, 1))
        line = inv.lines[0]
        rate = MY_CLF.standard_rate_for_date(inv.invoice_date)
        code = resolve_tax_code(
            line.tax_treatment,
            rate=rate,
            doc_type="purchase",
            software="autocount",
            client_tax_codes=[],
            classifier=MY_CLF,
        )
        assert code == ""
        assert code != "SV-8"

    def test_none_client_tax_list_still_uses_yaml_seed(self):
        """When no client master is configured (None), YAML seed is allowed."""
        inv = _purchase_inv(inv_date=date(2024, 6, 1))
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


class TestXeroTaxCodeMaster:
    def test_xero_uses_client_tax_codes_not_yaml(self):
        inv = _purchase_inv(inv_date=date(2024, 6, 1))
        client_codes = [
            {"code": "CLIENT-SR", "description": "Client SR", "treatment": "SR:0.08"},
        ]
        exporter = XeroLedgerExporter(classifier=MY_CLF)
        exporter.configure_client_context(tax_codes=client_codes)
        rows = exporter.rows([inv], "purchase")
        assert rows[0]["*TaxType"] == "CLIENT-SR"
        assert rows[0]["*TaxType"] != "SV-8"

    def test_xero_empty_tax_codes_blanks_tax_type(self):
        inv = _purchase_inv(inv_date=date(2024, 6, 1))
        exporter = XeroLedgerExporter(classifier=MY_CLF)
        exporter.configure_client_context(tax_codes=[])
        rows = exporter.rows([inv], "purchase")
        assert rows[0]["*TaxType"] == ""

    def test_xero_empty_tax_codes_surfaces_in_unmapped_summary(self):
        inv = _purchase_inv(inv_date=date(2024, 6, 1))
        exporter = XeroLedgerExporter(classifier=MY_CLF)
        exporter.configure_client_context(tax_codes=[])
        rows = exporter.rows([inv], "purchase")
        summary = collect_export_unmapped_summary(
            [{"sheet": "Purchase", "rows": rows}],
            exporter,
        )
        assert summary["count"] >= 1
        assert any("*TaxType" in d.get("missing", []) for d in summary["details"])


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


class TestExportCoaZeroTolerance:
    """WS-3.2 — exported rows must never carry out-of-COA account codes."""

    _COA_KEYS = {"6100", "6200", "7100"}

    def test_validate_export_account_code_valid_passes(self):
        result = validate_export_account_code("6100", coa_keys=self._COA_KEYS)
        assert result.account_code == "6100"
        assert result.flagged is False
        assert result.reason is None

    def test_validate_export_account_code_hallucinated_blanked_and_flagged(self):
        result = validate_export_account_code("999-FAKE", coa_keys=self._COA_KEYS)
        assert result.account_code == ""
        assert result.flagged is True
        assert result.reason is not None
        assert "999-FAKE" in result.reason

    def test_validate_export_account_code_unmapped_is_abstention(self):
        result = validate_export_account_code(
            UNMAPPED_ACCOUNT_CODE, coa_keys=self._COA_KEYS
        )
        assert result.account_code == ""
        assert result.flagged is True

    def test_qbs_exporter_valid_coa_code_passes_through(self):
        inv = _purchase_inv(inv_date=date(2024, 6, 1))
        inv.lines[0].account_code = "6100"
        exporter = QbsLedgerExporter(classifier=MY_CLF)
        exporter.configure_client_context(coa_keys=self._COA_KEYS)
        rows = exporter.rows([inv], "purchase")
        assert rows[0]["Account Code / COA"] == "6100"

    def test_qbs_exporter_hallucinated_code_blanked_at_export(self):
        inv = _purchase_inv(inv_date=date(2024, 6, 1))
        inv.lines[0].account_code = "999-FAKE"
        exporter = QbsLedgerExporter(classifier=MY_CLF)
        exporter.configure_client_context(coa_keys=self._COA_KEYS)
        rows = exporter.rows([inv], "purchase")
        assert rows[0]["Account Code / COA"] == ""

    def test_xero_exporter_hallucinated_code_blanked_at_export(self):
        inv = _purchase_inv(inv_date=date(2024, 6, 1))
        inv.lines[0].account_code = "XXX-INVALID"
        exporter = XeroLedgerExporter(classifier=MY_CLF)
        exporter.configure_client_context(coa_keys=self._COA_KEYS)
        rows = exporter.rows([inv], "purchase")
        assert rows[0]["*AccountCode"] == ""

    def test_autocount_exporter_hallucinated_code_blanked_at_export(self):
        inv = _purchase_inv(inv_date=date(2024, 6, 1))
        inv.lines[0].account_code = "XXX-INVALID"
        exporter = AutoCountExporter(classifier=MY_CLF)
        exporter.configure_client_context(coa_keys=self._COA_KEYS)
        rows = exporter.rows([inv], "purchase")
        account_col = exporter.column_for_field("account_code", "purchase")
        assert account_col is not None
        assert rows[0][account_col] == ""
