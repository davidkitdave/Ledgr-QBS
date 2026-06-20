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
    CROSS_BORDER_KEY,
    FLAG_FOR_HUMAN_KEY,
    JURISDICTION_AMBIGUOUS,
    JURISDICTION_CROSS_BORDER,
    REGION_MALAYSIA,
    REGION_REGISTRY,
    REGION_SINGAPORE,
    TAX_JURISDICTION_KEY,
    TAX_SYSTEM_GST,
    TAX_SYSTEM_HINT_KEY,
    TAX_SYSTEM_OUT_OF_SCOPE,
    TAX_SYSTEM_SST,
    registration_threshold_for_region,
    resolve_jurisdiction,
    resolution_from_state,
    supported_regions,
    write_to_state,
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

    def test_cross_border_sg_client_my_supplier_auto_books(self):
        """SG client + MY supplier → CROSS_BORDER, auto-book (flag_for_human=False)."""
        res = resolve_jurisdiction(
            {"region": "SINGAPORE", "base_currency": "SGD", "supplier_country": "MY"}
        )
        assert res.jurisdiction.code == JURISDICTION_CROSS_BORDER
        assert res.jurisdiction.cross_border is True
        assert res.jurisdiction.flag_for_human is False
        assert res.jurisdiction.tax_system == TAX_SYSTEM_OUT_OF_SCOPE
        assert "GST" in res.jurisdiction.notes
        assert "not claimable" in res.jurisdiction.notes

    def test_cross_border_my_client_sg_supplier_auto_books(self):
        """MY client + SG supplier → CROSS_BORDER, auto-book (flag_for_human=False)."""
        res = resolve_jurisdiction(
            {"region": "MALAYSIA", "base_currency": "MYR", "supplier_country": "SG"}
        )
        assert res.jurisdiction.code == JURISDICTION_CROSS_BORDER
        assert res.jurisdiction.cross_border is True
        assert res.jurisdiction.flag_for_human is False
        assert res.jurisdiction.tax_system == TAX_SYSTEM_OUT_OF_SCOPE
        assert "SST" in res.jurisdiction.notes
        assert "not claimable" in res.jurisdiction.notes

    def test_cross_border_sg_partial_exempt_flags(self):
        """SG partially-exempt client + foreign supplier → flag_for_human=True (RC review)."""
        res = resolve_jurisdiction(
            {
                "region": "SINGAPORE",
                "base_currency": "SGD",
                "supplier_country": "MY",
                "partial_exempt": True,
            }
        )
        assert res.jurisdiction.code == JURISDICTION_CROSS_BORDER
        assert res.jurisdiction.cross_border is True
        assert res.jurisdiction.flag_for_human is True
        assert "reverse-charge" in res.jurisdiction.notes.lower()

    def test_cross_border_write_read_roundtrip_flag_preserved(self):
        """write_to_state + resolution_from_state preserves flag_for_human=False."""
        state: dict = {
            "region": "MALAYSIA",
            "base_currency": "MYR",
            "supplier_country": "SG",
        }
        res = resolve_jurisdiction(state)
        assert res.jurisdiction.flag_for_human is False
        write_to_state(state, res)
        # The persisted keys must be present.
        assert state[FLAG_FOR_HUMAN_KEY] is False
        assert state[CROSS_BORDER_KEY] is True
        # Round-trip must NOT re-derive True from the code.
        rebuilt = resolution_from_state(state)
        assert rebuilt.jurisdiction.flag_for_human is False
        assert rebuilt.jurisdiction.cross_border is True

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
# WS1 characterization — SG/MY must match pre-refactor branches exactly
# --------------------------------------------------------------------------- #
class TestResolveJurisdictionCharacterization:
    """Explicit snapshots of every supported resolve_jurisdiction branch."""

    def test_registry_covers_sg_and_my(self):
        assert set(supported_regions()) == {REGION_SINGAPORE, REGION_MALAYSIA}
        assert REGION_REGISTRY[REGION_SINGAPORE]["yaml"] == "sg_gst.yaml"
        assert REGION_REGISTRY[REGION_MALAYSIA]["yaml"] == "my_sst.yaml"

    def test_sg_domestic_full_rule(self):
        res = resolve_jurisdiction(
            {"region": "SINGAPORE", "base_currency": "SGD", "supplier_country": "SG"}
        )
        j = res.jurisdiction
        assert j.code == REGION_SINGAPORE
        assert j.region == REGION_SINGAPORE
        assert j.tax_system == TAX_SYSTEM_GST
        assert j.reference_yaml == "sg_gst.yaml"
        assert j.standard_rate == pytest.approx(0.09)
        assert j.rate_band_label == "9% GST"
        assert j.cross_border is False
        assert j.flag_for_human is False
        assert res.client_currency == "SGD"

    def test_my_domestic_full_rule(self):
        res = resolve_jurisdiction(
            {"region": "MALAYSIA", "base_currency": "MYR", "supplier_country": "MY"}
        )
        j = res.jurisdiction
        assert j.code == REGION_MALAYSIA
        assert j.region == REGION_MALAYSIA
        assert j.tax_system == TAX_SYSTEM_SST
        assert j.reference_yaml == "my_sst.yaml"
        assert j.standard_rate == pytest.approx(0.08)
        assert j.rate_band_label == "8% SST"
        assert j.cross_border is False
        assert j.flag_for_human is False
        assert res.client_currency == "MYR"

    def test_sg_cross_border_auto_book_notes(self):
        res = resolve_jurisdiction(
            {"region": "SINGAPORE", "base_currency": "SGD", "supplier_country": "MY"}
        )
        j = res.jurisdiction
        assert j.code == JURISDICTION_CROSS_BORDER
        assert j.tax_system == TAX_SYSTEM_OUT_OF_SCOPE
        assert j.reference_yaml == "sg_gst.yaml"
        assert j.standard_rate is None
        assert j.cross_border is True
        assert j.flag_for_human is False
        assert "Foreign counterparty" in (j.notes or "")
        assert "not claimable" in (j.notes or "")

    def test_sg_cross_border_partial_exempt_flags(self):
        res = resolve_jurisdiction(
            {
                "region": "SINGAPORE",
                "base_currency": "SGD",
                "supplier_country": "MY",
                "partial_exempt": True,
            }
        )
        assert res.jurisdiction.flag_for_human is True
        assert "reverse-charge" in (res.jurisdiction.notes or "").lower()

    def test_my_cross_border_auto_book_notes(self):
        res = resolve_jurisdiction(
            {"region": "MALAYSIA", "base_currency": "MYR", "supplier_country": "SG"}
        )
        j = res.jurisdiction
        assert j.code == JURISDICTION_CROSS_BORDER
        assert j.tax_system == TAX_SYSTEM_OUT_OF_SCOPE
        assert j.reference_yaml == "my_sst.yaml"
        assert j.flag_for_human is False
        assert "SST" in (j.notes or "")

    def test_ambiguous_no_region_no_sgd_fallback(self):
        """C10: missing region + currency must not silently default to SGD."""
        res = resolve_jurisdiction({})
        assert res.jurisdiction.code == JURISDICTION_AMBIGUOUS
        assert res.client_currency is None
        assert res.jurisdiction.flag_for_human is True

    def test_registration_threshold_from_yaml(self):
        """C7: thresholds live in jurisdiction YAML, not Python constants."""
        sg_amount, sg_cur, sg_label = registration_threshold_for_region(REGION_SINGAPORE)
        assert sg_amount == pytest.approx(1_000_000.0)
        assert sg_cur == "SGD"
        assert "GST" in sg_label

        my_amount, my_cur, my_label = registration_threshold_for_region(REGION_MALAYSIA)
        assert my_amount == pytest.approx(500_000.0)
        assert my_cur == "MYR"
        assert "SST" in my_label


# --------------------------------------------------------------------------- #
# C4 — is_overseas relative to client home country
# --------------------------------------------------------------------------- #
class TestPartyIsOverseasFor:
    def test_sg_supplier_domestic_for_sg_client(self):
        party = PartyInfo(country="SG")
        assert party.is_overseas_for("SG") is False

    def test_sg_supplier_overseas_for_my_client(self):
        party = PartyInfo(country="SG")
        assert party.is_overseas_for("MY") is True

    def test_my_supplier_domestic_for_my_client(self):
        party = PartyInfo(country="MY")
        assert party.is_overseas_for("MY") is False

    def test_my_supplier_overseas_for_sg_client(self):
        party = PartyInfo(country="MY")
        assert party.is_overseas_for("SG") is True


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

    def test_cross_border_auto_books_os_no_flag(self):
        """SG client, MY supplier, no partial_exempt → CROSS_BORDER → OS, NOT flagged, no LLM."""
        from accounting_agents.tax_reasoning import reason_one_invoice

        state = {
            "region": "SINGAPORE",
            "base_currency": "SGD",
            "supplier_country": "MY",
            TAX_JURISDICTION_KEY: "CROSS_BORDER",
            TAX_SYSTEM_HINT_KEY: "OS",
            FLAG_FOR_HUMAN_KEY: False,
            CROSS_BORDER_KEY: True,
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
        assert line.tax_flagged is False
        assert line.tax_confidence == pytest.approx(0.9)
        assert outcome.used_llm is False
        assert outcome.flagged_count == 0

    def test_cross_border_partial_exempt_flags(self):
        """SG partially-exempt client + foreign supplier → CROSS_BORDER, flagged (RC review)."""
        from accounting_agents.tax_reasoning import reason_one_invoice

        state = {
            "region": "SINGAPORE",
            "base_currency": "SGD",
            "supplier_country": "MY",
            "partial_exempt": True,
            TAX_JURISDICTION_KEY: "CROSS_BORDER",
            TAX_SYSTEM_HINT_KEY: "OS",
            FLAG_FOR_HUMAN_KEY: True,
            CROSS_BORDER_KEY: True,
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
        # WS2a: resolve_jurisdiction_node is the single authority; run it first
        # (mirrors the real invoice lane order: categorize → resolve_jurisdiction
        # → tax).  This seeds party countries from invoices and writes the five
        # jurisdiction state keys so tax_node can read them via
        # _resolution_from_state.
        asyncio.run(nodes.resolve_jurisdiction_node._func(ctx))
        event = asyncio.run(nodes.tax_node._func(ctx))
        assert event.output["tax_jurisdiction"] == "MALAYSIA"
        assert event.output["tax_system"] == "SST"
        # The tax line must not be flagged for SR 9% mismatch on a valid 8% SST line.
        line = state["normalized_invoices"][0]["lines"][0]
        assert line["tax_flagged"] is False, f"flagged: {line.get('tax_reason')}"
        assert state.get("tax_jurisdiction") == "MALAYSIA"
        assert state.get("tax_system_hint") == "SST"


# --------------------------------------------------------------------------- #
# Cross-border auto-book: MY client + SG supplier, 2-line probe
# --------------------------------------------------------------------------- #
class TestCrossBorderAutoBook:
    """Probes the new intelligent cross-border routing end-to-end."""

    def _my_cross_border_state(self) -> dict:
        return {
            "region": "MALAYSIA",
            "base_currency": "MYR",
            "supplier_country": "SG",
            TAX_JURISDICTION_KEY: JURISDICTION_CROSS_BORDER,
            TAX_SYSTEM_HINT_KEY: TAX_SYSTEM_OUT_OF_SCOPE,
            FLAG_FOR_HUMAN_KEY: False,
            CROSS_BORDER_KEY: True,
            "jurisdiction_rates": {
                "standard_rate": None,
                "rate_tolerance": 0.01,
                "rate_band_label": None,
                "reference_yaml": "my_sst.yaml",
            },
        }

    def test_resolve_my_cross_border_auto_book(self):
        """MY client (MYR) + SG supplier → code=CROSS_BORDER, flag=False, system=OS."""
        res = resolve_jurisdiction({
            "region": "MALAYSIA",
            "base_currency": "MYR",
            "supplier_country": "SG",
        })
        assert res.jurisdiction.code == JURISDICTION_CROSS_BORDER
        assert res.jurisdiction.cross_border is True
        assert res.jurisdiction.flag_for_human is False
        assert res.jurisdiction.tax_system == TAX_SYSTEM_OUT_OF_SCOPE

    def test_roundtrip_flag_preserved_false(self):
        """write_to_state → resolution_from_state: flag_for_human stays False."""
        state: dict = {
            "region": "MALAYSIA",
            "base_currency": "MYR",
            "supplier_country": "SG",
        }
        res = resolve_jurisdiction(state)
        write_to_state(state, res)
        assert state[FLAG_FOR_HUMAN_KEY] is False
        assert state[CROSS_BORDER_KEY] is True
        rebuilt = resolution_from_state(state)
        assert rebuilt.jurisdiction.flag_for_human is False
        assert rebuilt.jurisdiction.cross_border is True

    def test_two_line_invoice_all_os_not_flagged(self):
        """MY client + SG supplier → both lines OS, tax_flagged=False, flagged_count=0."""
        from accounting_agents.tax_reasoning import reason_one_invoice

        state = self._my_cross_border_state()
        inv = NormalizedInvoice(
            doc_type="purchase",
            invoice_date=date(2024, 9, 1),
            our_gst_registered=True,
            supplier=PartyInfo(name="SG Vendor Pte Ltd", country="SG"),
        )
        inv.lines.append(InvoiceLine(description="Consulting fee", net_amount=500.0, gst_amount=45.0))
        inv.lines.append(InvoiceLine(description="Software licence", net_amount=200.0, gst_amount=18.0))

        outcome = reason_one_invoice(inv, state=state)

        assert outcome.flagged_count == 0
        assert outcome.used_llm is False
        assert outcome.used_fallback is False
        for line in inv.lines:
            assert line.tax_treatment == "OS", f"expected OS, got {line.tax_treatment}"
            assert line.tax_flagged is False, f"line flagged: {line.tax_reason}"
            assert line.tax_confidence == pytest.approx(0.9)

    def test_sg_domestic_unchanged(self):
        """SG domestic (region=SINGAPORE, SGD, supplier=SG) → SINGAPORE, GST, flag=False."""
        res = resolve_jurisdiction({
            "region": "SINGAPORE",
            "base_currency": "SGD",
            "supplier_country": "SG",
        })
        assert res.jurisdiction.code == REGION_SINGAPORE
        assert res.jurisdiction.tax_system == TAX_SYSTEM_GST
        assert res.jurisdiction.flag_for_human is False

    def test_ambiguous_no_region_still_flags(self):
        """AMBIGUOUS (no region) → flag_for_human=True unchanged."""
        res = resolve_jurisdiction({})
        assert res.jurisdiction.code == JURISDICTION_AMBIGUOUS
        assert res.jurisdiction.flag_for_human is True