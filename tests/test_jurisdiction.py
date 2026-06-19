"""Tests for the multi-country jurisdiction router + tax reasoning.

Covers the YAU LEE Malaysia session gap (plan Phase 5 §P0):

* ``resolve_jurisdiction`` returns SINGAPORE for SG/SGD profile.
* ``resolve_jurisdiction`` returns MALAYSIA for MY/MYR profile (with the
  ``my_sst.yaml`` reference + 0.08 standard rate).
* ``resolve_jurisdiction`` returns CROSS_BORDER when client + counterparty
  countries differ.
* ``resolve_jurisdiction`` returns AMBIGUOUS when region is missing.
* ``tax_reasoning`` (with a stub LLM) maps a 4.81 / 60.19 line to SR + 0%
  tolerance (no SG 9% flag) for the Malaysia jurisdiction.
* ``tax_node`` (in ``nodes.py``) writes ``state["tax_jurisdiction"]`` for
  ADK web visibility.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Optional

import pytest

from accounting_agents import nodes
from accounting_agents.jurisdiction import (
    JURISDICTION_AMBIGUOUS,
    JURISDICTION_CROSS_BORDER,
    REGION_MALAYSIA,
    REGION_SINGAPORE,
    TAX_JURISDICTION_KEY,
    TAX_SYSTEM_GST,
    TAX_SYSTEM_HINT_KEY,
    TAX_SYSTEM_SST,
    resolve_jurisdiction,
)
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice, PartyInfo


# --------------------------------------------------------------------------- #
# Pure-function: resolve_jurisdiction
# --------------------------------------------------------------------------- #
class TestResolveJurisdiction:
    def test_singapore_default_profile(self):
        res = resolve_jurisdiction({"region": "SINGAPORE", "base_currency": "SGD"})
        assert res.jurisdiction.code == REGION_SINGAPORE
        assert res.jurisdiction.tax_system == TAX_SYSTEM_GST
        assert res.jurisdiction.reference_yaml == "sg_gst.yaml"
        assert res.jurisdiction.standard_rate == pytest.approx(0.09)
        assert res.jurisdiction.flag_for_human is False

    def test_malaysia_jbi_plus_profile(self):
        """YAU LEE scenario: client=MALAYSIA, base_currency=MYR."""
        res = resolve_jurisdiction(
            {"region": "MALAYSIA", "base_currency": "MYR"}
        )
        assert res.jurisdiction.code == REGION_MALAYSIA
        assert res.jurisdiction.tax_system == TAX_SYSTEM_SST
        assert res.jurisdiction.reference_yaml == "my_sst.yaml"
        assert res.jurisdiction.standard_rate == pytest.approx(0.08)
        assert res.jurisdiction.rate_band_label == "8% SST"
        assert res.jurisdiction.flag_for_human is False

    def test_cross_border_sg_client_my_supplier(self):
        res = resolve_jurisdiction(
            {"region": "SINGAPORE", "base_currency": "SGD", "supplier_country": "MY"}
        )
        assert res.jurisdiction.code == JURISDICTION_CROSS_BORDER
        assert res.jurisdiction.flag_for_human is True
        assert res.jurisdiction.cross_border is True

    def test_cross_border_my_client_sg_supplier(self):
        res = resolve_jurisdiction(
            {"region": "MALAYSIA", "base_currency": "MYR", "supplier_country": "SG"}
        )
        assert res.jurisdiction.code == JURISDICTION_CROSS_BORDER
        assert res.jurisdiction.flag_for_human is True

    def test_ambiguous_no_region(self):
        res = resolve_jurisdiction({})
        assert res.jurisdiction.code == JURISDICTION_AMBIGUOUS
        assert res.jurisdiction.flag_for_human is True

    def test_ambiguous_currency_mismatch(self):
        """SG region but MYR currency — must NOT silently fall back to SG math."""
        res = resolve_jurisdiction({"region": "SINGAPORE", "base_currency": "MYR"})
        assert res.jurisdiction.code == JURISDICTION_AMBIGUOUS
        assert res.jurisdiction.flag_for_human is True

    def test_region_aliases_normalized(self):
        for raw, expected in [
            ("sg", REGION_SINGAPORE),
            ("SGP", REGION_SINGAPORE),
            ("Malaysia", REGION_MALAYSIA),
            ("MY", REGION_MALAYSIA),
        ]:
            res = resolve_jurisdiction({"region": raw, "base_currency": expected[:2] + ("GD" if expected == "SINGAPORE" else "YR")})
            assert res.jurisdiction.region == expected, f"{raw} should normalise to {expected}"


# --------------------------------------------------------------------------- #
# Pure-function: tax_reasoning (with stubbed LLM)
# --------------------------------------------------------------------------- #
class _StubGenAIPart:
    def __init__(self, text: str) -> None:
        self.text = text


class _StubGenAIResponse:
    def __init__(self, text: str) -> None:
        self.text = text


def _install_stub_llm(monkeypatch, payload: dict[str, Any]):
    """Stub ``genai_client.make_client`` so tax_reasoning returns ``payload``."""
    from google import genai as _genai  # type: ignore[import-not-found]
    # The tax_reasoning module imports make_client lazily; we patch it where
    # it imports from.
    import invoice_processing.shared_libraries.genai_client as _gc

    class _StubModels:
        def generate_content(self, *, model, contents, config):
            return _StubGenAIResponse(json.dumps(payload))

    class _StubClient:
        models = _StubModels()

    def _stub_make_client(*args, **kwargs):
        return _StubClient()

    monkeypatch.setattr(_gc, "make_client", _stub_make_client)
    monkeypatch.setattr(_gc, "lite_model", lambda: "stub-lite")


def _inv_with_one_line(
    net: float = 60.19,
    gst: float = 4.81,
    *,
    inv_date: Optional[date] = None,
    our_gst: bool = True,
    supplier_country: str = "MY",
) -> NormalizedInvoice:
    line = InvoiceLine(
        description="Workshop labour",
        net_amount=net,
        gst_amount=gst,
        tax_keyword=None,
    )
    inv = NormalizedInvoice(
        doc_type="purchase",
        invoice_date=inv_date or date(2024, 6, 1),
        our_gst_registered=our_gst,
        supplier=PartyInfo(
            name="YAU LEE MOTOR",
            gst_regno="202301011111",
            country=supplier_country,
        ),
    )
    inv.lines.append(line)
    return inv


class TestTaxReasoningLLMPath:
    def test_yau_lee_malaysia_8pct_sst_passes(self, monkeypatch):
        """YAU LEE: net=60.19, gst=4.81 (8% SST). Must NOT flag SR 9% mismatch."""
        _install_stub_llm(
            monkeypatch,
            {
                "decisions": [
                    {
                        "line_index": 0,
                        "tax_treatment": "SR",
                        "tax_confidence": 0.92,
                        "tax_reason": "Service Tax 8% per SST Malaysia",
                        "tax_system": "SST",
                    }
                ],
                "overall_reason": "Domestic SST 8% on workshop labour",
            },
        )
        from accounting_agents.tax_reasoning import reason_one_invoice

        state = {
            "region": "MALAYSIA",
            "base_currency": "MYR",
            "supplier_country": "MY",
            TAX_JURISDICTION_KEY: "MALAYSIA",
            TAX_SYSTEM_HINT_KEY: "SST",
            "jurisdiction_rates": {
                "standard_rate": 0.08,
                "rate_tolerance": 0.01,
                "rate_band_label": "8% SST",
                "reference_yaml": "my_sst.yaml",
            },
        }
        inv = _inv_with_one_line(supplier_country="MY")
        outcome = reason_one_invoice(inv, state=state)
        line = inv.lines[0]
        assert line.tax_treatment == "SR"
        # Must NOT be flagged — the math reconciles to MY 8% within tolerance.
        assert line.tax_flagged is False, f"line was flagged: {line.tax_reason}"
        assert outcome.used_llm is True
        assert outcome.flagged_count == 0

    def test_cross_border_forces_os_with_low_confidence(self, monkeypatch):
        """SG client, MY supplier → CROSS_BORDER → OS, flagged, no LLM call."""
        from accounting_agents.tax_reasoning import reason_one_invoice

        state = {
            "region": "SINGAPORE",
            "base_currency": "SGD",
            "supplier_country": "MY",
            TAX_JURISDICTION_KEY: "CROSS_BORDER",
            TAX_SYSTEM_HINT_KEY: "OS",
            "jurisdiction_rates": {
                "standard_rate": None,
                "rate_tolerance": 0.01,
                "rate_band_label": "cross-border",
                "reference_yaml": "sg_gst.yaml",
            },
        }
        inv = _inv_with_one_line(supplier_country="MY")
        outcome = reason_one_invoice(inv, state=state)
        line = inv.lines[0]
        assert line.tax_treatment == "OS"
        assert line.tax_flagged is True
        assert outcome.used_llm is False
        assert outcome.flagged_count == 1

    def test_ambiguous_region_forces_nt_with_flag(self):
        """No region in state → AMBIGUOUS → NT, flagged, no LLM call."""
        from accounting_agents.tax_reasoning import reason_one_invoice

        inv = _inv_with_one_line()
        outcome = reason_one_invoice(inv, state={})
        line = inv.lines[0]
        assert line.tax_treatment == "NT"
        assert line.tax_flagged is True
        assert outcome.used_llm is False

    def test_sg_path_falls_back_to_classifier_when_llm_unavailable(self, monkeypatch):
        """SG invoice + LLM failure → fall back to deterministic SG classifier."""
        import invoice_processing.shared_libraries.genai_client as _gc
        monkeypatch.setattr(_gc, "make_client", lambda *a, **k: None)
        monkeypatch.setattr(_gc, "lite_model", lambda: "stub-lite")

        from accounting_agents.tax_reasoning import reason_one_invoice

        state = {
            "region": "SINGAPORE",
            "base_currency": "SGD",
            TAX_JURISDICTION_KEY: "SINGAPORE",
            TAX_SYSTEM_HINT_KEY: "GST",
            "jurisdiction_rates": {
                "standard_rate": 0.09,
                "rate_tolerance": 0.01,
                "rate_band_label": "9% GST",
                "reference_yaml": "sg_gst.yaml",
            },
        }
        inv = NormalizedInvoice(
            doc_type="purchase",
            invoice_date=date(2024, 6, 1),
            our_gst_registered=True,
            supplier=PartyInfo(
                name="Acme Supplier",
                gst_regno="202401011234",
                country="SG",
            ),
        )
        inv.lines.append(InvoiceLine(
            description="Insurance premium",
            net_amount=1000.0,
            gst_amount=90.0,
        ))
        outcome = reason_one_invoice(inv, state=state)
        line = inv.lines[0]
        # SG fallback uses TaxClassifier: SR + supplier registered + math reconciles.
        assert line.tax_treatment == "SR"
        assert line.tax_flagged is False
        assert outcome.used_fallback is True


# --------------------------------------------------------------------------- #
# tax_node integration: writes state["tax_jurisdiction"]
# --------------------------------------------------------------------------- #
class FakeContext:
    def __init__(self, state):
        # Pass-through: writes through ctx.state propagate back to the
        # caller's dict (matches how ADK session.state behaves in practice:
        # the callback context exposes the same mapping).
        self.state = state


def _build_normalized_for_state(supplier_country: str = "MY"):
    inv = NormalizedInvoice(
        doc_type="purchase",
        invoice_date=date(2024, 6, 1),
        our_gst_registered=True,
        supplier=PartyInfo(
            name="YAU LEE MOTOR", gst_regno="202301011111", country=supplier_country,
        ),
    )
    inv.lines.append(InvoiceLine(description="Workshop labour", net_amount=60.19, gst_amount=4.81))
    return inv


class TestTaxNodeWritesJurisdiction:
    def test_tax_node_writes_tax_jurisdiction_for_my(self, monkeypatch):
        """tax_node should set ``state['tax_jurisdiction']`` to MALAYSIA."""
        # Stub the LLM so the test doesn't hit the network.
        _install_stub_llm(
            monkeypatch,
            {
                "decisions": [
                    {
                        "line_index": 0,
                        "tax_treatment": "SR",
                        "tax_confidence": 0.92,
                        "tax_reason": "SST 8%",
                        "tax_system": "SST",
                    }
                ]
            },
        )

        import asyncio

        from accounting_agents.normalized_invoice_codec import invoice_to_dict

        state = {
            "region": "MALAYSIA",
            "base_currency": "MYR",
            "tax_registered": True,
            "client_id": "jbi-plus-auto",
            "client_name": "JBI PLUS AUTO SDN BHD",
            "normalized_invoices": [invoice_to_dict(_build_normalized_for_state("MY"))],
        }
        ctx = FakeContext(state)
        event = asyncio.run(nodes.tax_node._func(ctx))
        assert event.output["tax_jurisdiction"] == "MALAYSIA"
        assert event.output["tax_system"] == "SST"
        # The tax line must not be flagged for SR 9% mismatch on a valid 8% SST line.
        line = state["normalized_invoices"][0]["lines"][0]
        assert line["tax_flagged"] is False, f"flagged: {line.get('tax_reason')}"
        assert state.get("tax_jurisdiction") == "MALAYSIA"
        assert state.get("tax_system_hint") == "SST"