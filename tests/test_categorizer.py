"""Tests for invoice_processing.export.categorizer — deterministic path.

All tests are hermetic: no network, no Gemini, no file I/O.
The LLM seam (_llm_match_lines) is never called from the deterministic tests
(that is the §9 cost guardrail: happy-path spends zero LLM tokens).
"""

from __future__ import annotations


from invoice_processing.export.categorizer import (
    _norm,
    _split_keywords,
    categorize_invoice,
    resolve_account,
)
from invoice_processing.export.client_context import CoaAccount, EntityMemoryEntry
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice, PartyInfo


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _coa(*items: tuple[str, str, str]) -> list[CoaAccount]:
    """Build a minimal COA list from (code, description, keywords) triples."""
    return [CoaAccount(code=c, description=d, keywords=k) for c, d, k in items]


def _entity(name: str, code: str, reg_no: str = "") -> EntityMemoryEntry:
    return EntityMemoryEntry(name=name, reg_no=reg_no, mapping_code=code)


def _inv(description: str, vendor: str = "Some Vendor") -> NormalizedInvoice:
    """Minimal purchase invoice with one line."""
    return NormalizedInvoice(
        doc_type="purchase",
        supplier=PartyInfo(name=vendor),
        lines=[InvoiceLine(description=description)],
    )


# --------------------------------------------------------------------------- #
# _norm / _split_keywords
# --------------------------------------------------------------------------- #


def test_norm_strips_spaces_and_case():
    assert _norm("Hello World") == "helloworld"


def test_norm_none_returns_empty():
    assert _norm(None) == ""


def test_split_keywords_semicolon_and_slash():
    result = _split_keywords("office; supplies / stationery")
    assert "office" in result
    assert "supplies" in result
    assert "stationery" in result


def test_split_keywords_empty():
    assert _split_keywords(None) == []
    assert _split_keywords("") == []


# --------------------------------------------------------------------------- #
# resolve_account — entity_memory path
# --------------------------------------------------------------------------- #


def test_resolve_entity_memory_name_hit():
    mem = [_entity("Acme Supplier", "6001")]
    res = resolve_account(
        "Office supplies",
        "Acme Supplier",
        coa=[],
        category_mapping={},
        entity_memory=mem,
    )
    assert res.source == "entity_memory"
    assert res.account_code == "6001"
    assert res.confidence == 0.95
    assert res.flagged is False


def test_resolve_entity_memory_reg_no_hit():
    mem = [_entity("Any Name", "6002", reg_no="200012345A")]
    res = resolve_account(
        "Invoice",
        "Different Name",
        coa=[],
        category_mapping={},
        entity_memory=mem,
        reg_no="200012345A",
    )
    assert res.source == "entity_memory"
    assert res.account_code == "6002"


def test_resolve_entity_memory_short_name_no_hit():
    """Names with <=3 chars should not trigger an entity match."""
    mem = [_entity("AB", "6001")]
    res = resolve_account(
        "Something",
        "AB",
        coa=[],
        category_mapping={},
        entity_memory=mem,
    )
    assert res.source != "entity_memory"


# --------------------------------------------------------------------------- #
# resolve_account — category_mapping path
# --------------------------------------------------------------------------- #


def test_resolve_category_mapping_hit():
    cat_map = {"office_supplies": "6100"}
    res = resolve_account(
        "Pens",
        "Stationery Co",
        coa=[],
        category_mapping=cat_map,
        entity_memory=[],
        category="office_supplies",
    )
    assert res.source == "category_mapping"
    assert res.account_code == "6100"
    assert res.confidence == 0.9


def test_resolve_category_mapping_none_value_falls_through():
    """A mapping with None value should not count as resolved."""
    cat_map = {"office_supplies": None}
    res = resolve_account(
        "Pens",
        "Stationery Co",
        coa=[],
        category_mapping=cat_map,
        entity_memory=[],
        category="office_supplies",
    )
    assert res.source != "category_mapping"


# --------------------------------------------------------------------------- #
# resolve_account — COA keyword path
# --------------------------------------------------------------------------- #


def test_resolve_coa_keyword_hit():
    coa = _coa(("6200", "Travel Expenses", "airfare,hotel,transport"))
    res = resolve_account(
        "Hotel accommodation",
        "Marriott",
        coa=coa,
        category_mapping={},
        entity_memory=[],
    )
    assert res.source == "coa_keyword"
    assert res.account_code == "6200"
    assert res.confidence == 0.8


def test_resolve_coa_no_keyword_match_unresolved():
    coa = _coa(("6200", "Travel Expenses", "airfare"))
    res = resolve_account(
        "Stationery purchase",
        "Office Depot",
        coa=coa,
        category_mapping={},
        entity_memory=[],
    )
    assert res.source == "unresolved"
    assert res.account_code is None
    assert res.flagged is True


# --------------------------------------------------------------------------- #
# resolve_account — priority order
# --------------------------------------------------------------------------- #


def test_entity_memory_beats_category_mapping():
    """Entity memory (conf=0.95) must win over category_mapping (conf=0.9)."""
    mem = [_entity("Acme Supplier", "entity_code")]
    cat_map = {"office_supplies": "cat_code"}
    coa = _coa(("kw_code", "Office Stuff", "office"))
    res = resolve_account(
        "Supplies",
        "Acme Supplier",
        coa=coa,
        category_mapping=cat_map,
        entity_memory=mem,
        category="office_supplies",
    )
    assert res.source == "entity_memory"
    assert res.account_code == "entity_code"


# --------------------------------------------------------------------------- #
# categorize_invoice — deterministic short-circuit (LLM never called)
# §9 cost guardrail: when all lines resolve deterministically, _llm_match_lines
# must NOT be invoked.
# --------------------------------------------------------------------------- #


def test_deterministic_shortcircuit_no_llm_called(monkeypatch):
    """All lines resolve via entity_memory → LLM seam never invoked."""
    called = []

    def fake_llm(*args, **kwargs):
        called.append(True)
        return {}

    monkeypatch.setattr(
        "invoice_processing.export.categorizer._llm_match_lines", fake_llm
    )

    mem = [_entity("Acme Supplier", "6001")]
    inv = _inv("Office stuff", vendor="Acme Supplier")
    result = categorize_invoice(
        inv,
        coa=_coa(("6001", "Expenses", "")),
        category_mapping={},
        entity_memory=mem,
        use_llm=True,
    )

    assert called == [], "LLM was called despite all lines being resolved deterministically"
    assert result.lines[0].account_code == "6001"


def test_use_llm_false_never_calls_llm(monkeypatch):
    """use_llm=False must skip the LLM path entirely even for unresolved lines."""
    called = []

    def fake_llm(*args, **kwargs):
        called.append(True)
        return {}

    monkeypatch.setattr(
        "invoice_processing.export.categorizer._llm_match_lines", fake_llm
    )

    inv = _inv("Unknown service")
    categorize_invoice(
        inv,
        coa=_coa(("6200", "Travel", "airfare")),
        category_mapping={},
        entity_memory=[],
        use_llm=False,
    )

    assert called == [], "LLM called despite use_llm=False"
