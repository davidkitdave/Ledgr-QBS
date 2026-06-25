"""WS2 — Malaysia SST rate truth + YAML-driven rate narrative (tax_reasoning)."""

from __future__ import annotations

from datetime import date

from accounting_agents.tax_reasoning import _tax_prompt, _validate_rate_allowed
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice
from invoice_processing.export.tax_classifier import get_tax_classifier


class TestTaxPromptRateNarrative:
    def test_my_prompt_uses_yaml_rates_not_hardcoded_8pct_string(self):
        prompt = _tax_prompt(
            NormalizedInvoice(doc_type="purchase", invoice_date=date(2024, 6, 1)),
            client_region="MALAYSIA",
            client_currency="MYR",
            tax_jurisdiction="MALAYSIA",
            tax_system="SST",
            rate_band_label="8% SST",
            standard_rate=0.08,
            supplier_country="MY",
            customer_country=None,
            our_tax_registered=True,
            reference_yaml="my_sst.yaml",
        )
        assert "The standard rate for MY is 8% SST" not in prompt
        assert "8% SST" in prompt or "0.08" in prompt
        assert "6%" in prompt or "0.06" in prompt


class TestValidateRateAllowed:
    def test_my_service_8pct_passes(self):
        line = InvoiceLine(description="labour", net_amount=100.0, gst_amount=8.0)
        suffix, flag = _validate_rate_allowed(
            line,
            allowed_rates=[0.06, 0.08],
            tolerance=0.01,
            jurisdiction_code="MALAYSIA",
        )
        assert flag is False
        assert suffix is None

    def test_my_carve_out_6pct_passes(self):
        line = InvoiceLine(description="telecom", net_amount=100.0, gst_amount=6.0)
        suffix, flag = _validate_rate_allowed(
            line,
            allowed_rates=[0.06, 0.08],
            tolerance=0.01,
            jurisdiction_code="MALAYSIA",
        )
        assert flag is False

    def test_my_goods_5_and_10_pct_pass(self):
        for gst in (5.0, 10.0):
            line = InvoiceLine(description="goods", net_amount=100.0, gst_amount=gst)
            suffix, flag = _validate_rate_allowed(
                line,
                allowed_rates=[0.05, 0.10],
                tolerance=0.01,
                jurisdiction_code="MALAYSIA",
            )
            assert flag is False, f"gst={gst} should pass 5%/10% guard"


class TestMySstRateKeywordsFromYaml:
    def test_my_classifier_rate_keywords_include_6_and_8(self):
        clf = get_tax_classifier("my_sst.yaml")
        keywords = clf.rate_keyword_strings()
        assert "6%" in keywords
        assert "8%" in keywords
        assert "5%" in keywords
        assert "10%" in keywords

    def test_sg_classifier_rate_keywords_from_yaml(self):
        clf = get_tax_classifier("sg_gst.yaml")
        keywords = clf.rate_keyword_strings()
        assert "9%" in keywords
        assert "8%" in keywords
        assert "7%" in keywords
