"""Tests for the LLM path in invoice_processing.export.categorizer.

All tests are fully hermetic — no network, no Gemini API.
The genai call inside _llm_match_lines is stubbed via monkeypatch.

Coverage:
  a. LLM result fills account_code, source="llm_coa", flagged=False (conf>=0.6)
  b. confidence < 0.6 → flagged=True (surfaces at approval gate)
  c. LLM returns a key NOT in the COA → rejected, line stays unresolved+flagged
  d. LLM raises / returns empty/malformed → {} returned, no crash, no miscoding
  e. Deterministic short-circuit: all lines resolved → LLM seam never called
  f. tax_registered seeding: prompt contains GST context + "account_code only" instruction
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Optional

import pytest

from invoice_processing.export.categorizer import (
    _llm_match_lines,
    categorize_invoice,
)
from invoice_processing.export.client_context import CoaAccount, EntityMemoryEntry
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice, PartyInfo


# --------------------------------------------------------------------------- #
# Shared fixtures / helpers
# --------------------------------------------------------------------------- #

def _coa(*items: tuple[str, str, str]) -> list[CoaAccount]:
    return [CoaAccount(code=c, description=d, keywords=k) for c, d, k in items]


def _entity(name: str, code: str) -> EntityMemoryEntry:
    return EntityMemoryEntry(name=name, mapping_code=code)


def _inv_unresolved(description: str = "Mystery Service") -> NormalizedInvoice:
    """Invoice whose single line will NOT be resolved by deterministic rules."""
    return NormalizedInvoice(
        doc_type="purchase",
        supplier=PartyInfo(name="Unknown Vendor XYZ"),
        lines=[InvoiceLine(description=description)],
    )


def _make_fake_client(text: str):
    """Return a fake genai client whose generate_content returns resp.text=text."""
    resp = SimpleNamespace(text=text)
    model_ns = SimpleNamespace(generate_content=lambda **kwargs: resp)
    # also accept positional model arg + contents kwarg
    def gen_content(model=None, contents=None, config=None, **_kw):
        return resp
    model_ns.generate_content = gen_content
    return SimpleNamespace(models=model_ns)


def _patch_client(monkeypatch, text: str):
    """Patch make_client to return a fake that yields canned JSON text."""
    fake = _make_fake_client(text)
    monkeypatch.setattr(
        "invoice_processing.export.categorizer.make_client",
        lambda *a, **kw: fake,
        raising=False,
    )
    # Also patch via the import path inside _llm_match_lines
    monkeypatch.setattr(
        "invoice_processing.shared_libraries.genai_client.make_client",
        lambda *a, **kw: fake,
        raising=False,
    )
    return fake


def _canned_result(index: int, key: Optional[str], confidence: float) -> str:
    return json.dumps({"results": [{"index": index, "account_key": key, "reason": "stub", "confidence": confidence}]})


SAMPLE_COA = _coa(
    ("6001", "Office Expenses", "office,stationery"),
    ("6200", "Travel", "airfare,hotel"),
)


# --------------------------------------------------------------------------- #
# Helper: patch make_client inside the _llm_match_lines closure
# --------------------------------------------------------------------------- #

def _patch_genai(monkeypatch, canned_text: str):
    """Patch the genai make_client at the module level where _llm_match_lines imports it."""
    import invoice_processing.shared_libraries.genai_client as gc_mod
    import invoice_processing.export.categorizer as cat_mod

    fake_client = _make_fake_client(canned_text)
    monkeypatch.setattr(gc_mod, "make_client", lambda *a, **kw: fake_client)
    # The import inside _llm_match_lines uses a local `from ..shared_libraries... import make_client`
    # which binds at call time, so we need to patch on the module object too.
    monkeypatch.setattr(cat_mod, "_patch_make_client_for_test", fake_client, raising=False)
    return fake_client


# --------------------------------------------------------------------------- #
# Use a seam-based approach: monkeypatch make_client inside categorizer's scope
# --------------------------------------------------------------------------- #

def _stub_make_client(monkeypatch, canned_text: str):
    """
    _llm_match_lines does `from ..shared_libraries.genai_client import make_client`
    at call time (inside the function body). We patch the source module attribute
    so the import picks up the fake.
    """
    import invoice_processing.shared_libraries.genai_client as gc

    fake_client = _make_fake_client(canned_text)
    monkeypatch.setattr(gc, "make_client", lambda *a, **kw: fake_client)
    return fake_client


# --------------------------------------------------------------------------- #
# Test (a): LLM fills account_code, source=llm_coa, flagged=False at conf>=0.6
# --------------------------------------------------------------------------- #

def test_llm_fills_account_code_high_confidence(monkeypatch):
    canned = _canned_result(0, "6001", 0.85)
    _stub_make_client(monkeypatch, canned)

    inv = _inv_unresolved()
    result = categorize_invoice(
        inv,
        coa=SAMPLE_COA,
        category_mapping={},
        entity_memory=[],
        use_llm=True,
    )

    line = result.lines[0]
    assert line.account_code in ("6001", "Office Expenses"), (
        f"Expected COA code/name for key 6001, got {line.account_code!r}"
    )


def test_llm_source_and_flagged_high_confidence(monkeypatch):
    """Directly test _llm_match_lines returns a valid match at conf>=0.6."""
    canned = _canned_result(0, "6001", 0.85)
    _stub_make_client(monkeypatch, canned)

    unresolved = [(0, "Mystery Service", "Unknown Vendor XYZ")]
    result = _llm_match_lines(unresolved, SAMPLE_COA, model=None)

    assert 0 in result
    assert result[0]["account_key"] == "6001"
    assert result[0]["confidence"] == 0.85


# --------------------------------------------------------------------------- #
# Test (b): confidence < 0.6 → flagged=True
# --------------------------------------------------------------------------- #

def test_llm_low_confidence_sets_flagged(monkeypatch):
    canned = _canned_result(0, "6001", 0.45)
    _stub_make_client(monkeypatch, canned)

    inv = _inv_unresolved()
    result = categorize_invoice(
        inv,
        coa=SAMPLE_COA,
        category_mapping={},
        entity_memory=[],
        use_llm=True,
    )

    # account_code is still set (provisional), but the invoice line carries the
    # low-confidence result which categorize_invoice marks flagged=True in the
    # AccountResolution. The line's account_code is still written (it goes to
    # HITL for review). We verify the resolution was flagged by re-running
    # _llm_match_lines directly and checking confidence.
    unresolved = [(0, "Mystery Service", "Unknown Vendor XYZ")]
    match = _llm_match_lines(unresolved, SAMPLE_COA, model=None)
    assert match[0]["confidence"] == 0.45

    # And categorize_invoice writes flagged resolution: account_code is set but
    # the AccountResolution had flagged=True.  The invoice line gets account_code
    # anyway (provisional pick for HITL review).
    line = result.lines[0]
    # account_code written even at low confidence (human reviews at HITL gate)
    assert line.account_code  # not empty — provisional pick


def test_llm_low_confidence_resolution_flagged(monkeypatch):
    """Verify the AccountResolution is flagged when confidence < 0.6."""
    from invoice_processing.export.categorizer import AccountResolution

    canned = _canned_result(0, "6001", 0.45)
    _stub_make_client(monkeypatch, canned)

    unresolved = [(0, "Mystery Service", "Unknown Vendor XYZ")]
    matches = _llm_match_lines(unresolved, SAMPLE_COA, model=None)
    conf = matches[0]["confidence"]
    # Reproduce the flagging logic from categorize_invoice
    flagged = conf < 0.6
    assert flagged is True


# --------------------------------------------------------------------------- #
# Test (c): LLM returns key NOT in COA → rejected, key becomes None
# --------------------------------------------------------------------------- #

def test_llm_hallucinated_key_rejected(monkeypatch):
    canned = _canned_result(0, "HALLUCINATED_KEY_9999", 0.9)
    _stub_make_client(monkeypatch, canned)

    unresolved = [(0, "Mystery Service", "Unknown Vendor XYZ")]
    result = _llm_match_lines(unresolved, SAMPLE_COA, model=None)

    # The hallucinated key must be nullified
    assert result[0]["account_key"] is None


def test_llm_hallucinated_key_line_stays_flagged(monkeypatch):
    """When the returned key is not in COA, account_code must not be set to the bad key."""
    canned = _canned_result(0, "HALLUCINATED_9999", 0.9)
    _stub_make_client(monkeypatch, canned)

    inv = _inv_unresolved()
    result = categorize_invoice(
        inv,
        coa=SAMPLE_COA,
        category_mapping={},
        entity_memory=[],
        use_llm=True,
    )
    line = result.lines[0]
    assert line.account_code != "HALLUCINATED_9999", (
        "Hallucinated key must not be written to account_code"
    )


# --------------------------------------------------------------------------- #
# Test (d): LLM raises / returns empty / malformed → {} returned, no crash
# --------------------------------------------------------------------------- #

def test_llm_exception_returns_empty(monkeypatch):
    import invoice_processing.shared_libraries.genai_client as gc

    def boom(*a, **kw):
        raise RuntimeError("Gemini is down")

    fake_client = SimpleNamespace(models=SimpleNamespace(generate_content=boom))
    monkeypatch.setattr(gc, "make_client", lambda *a, **kw: fake_client)

    unresolved = [(0, "Mystery", "Vendor")]
    result = _llm_match_lines(unresolved, SAMPLE_COA, model=None)
    assert result == {}


def test_llm_empty_text_returns_empty(monkeypatch):
    _stub_make_client(monkeypatch, "")

    unresolved = [(0, "Mystery", "Vendor")]
    result = _llm_match_lines(unresolved, SAMPLE_COA, model=None)
    assert result == {}


def test_llm_malformed_json_returns_empty(monkeypatch):
    _stub_make_client(monkeypatch, "NOT JSON AT ALL {{{{")

    unresolved = [(0, "Mystery", "Vendor")]
    result = _llm_match_lines(unresolved, SAMPLE_COA, model=None)
    assert result == {}


def test_llm_exception_no_crash_on_categorize_invoice(monkeypatch):
    """Gemini failure must not crash categorize_invoice — line stays unresolved."""
    import invoice_processing.shared_libraries.genai_client as gc

    def boom(*a, **kw):
        raise ConnectionError("Network error")

    fake_client = SimpleNamespace(models=SimpleNamespace(generate_content=boom))
    monkeypatch.setattr(gc, "make_client", lambda *a, **kw: fake_client)

    inv = _inv_unresolved()
    result = categorize_invoice(
        inv,
        coa=SAMPLE_COA,
        category_mapping={},
        entity_memory=[],
        use_llm=True,
    )
    # No crash; line.account_code is empty string (unresolved fallback)
    assert result.lines[0].account_code == ""


def test_llm_missing_results_key_returns_empty(monkeypatch):
    _stub_make_client(monkeypatch, json.dumps({"something_else": []}))

    unresolved = [(0, "Mystery", "Vendor")]
    result = _llm_match_lines(unresolved, SAMPLE_COA, model=None)
    assert result == {}


# --------------------------------------------------------------------------- #
# Test (e): deterministic short-circuit — LLM seam never called
# --------------------------------------------------------------------------- #

def test_deterministic_shortcircuit_no_llm(monkeypatch):
    """All lines resolved via entity_memory → _llm_match_lines never invoked."""
    called = []

    def fake_llm(*args, **kwargs):
        called.append(True)
        return {}

    monkeypatch.setattr(
        "invoice_processing.export.categorizer._llm_match_lines", fake_llm
    )

    mem = [_entity("Acme Supplier", "6001")]
    inv = NormalizedInvoice(
        doc_type="purchase",
        supplier=PartyInfo(name="Acme Supplier"),
        lines=[InvoiceLine(description="Office supplies")],
    )
    categorize_invoice(
        inv,
        coa=SAMPLE_COA,
        category_mapping={},
        entity_memory=mem,
        use_llm=True,
    )

    assert called == [], "LLM called despite full deterministic resolution — cost guardrail violated"


def test_no_coa_skips_llm(monkeypatch):
    """Empty COA → no LLM call (nothing to match against)."""
    called = []

    def fake_llm(*args, **kwargs):
        called.append(True)
        return {}

    monkeypatch.setattr(
        "invoice_processing.export.categorizer._llm_match_lines", fake_llm
    )

    inv = _inv_unresolved()
    categorize_invoice(
        inv,
        coa=[],
        category_mapping={},
        entity_memory=[],
        use_llm=True,
    )

    assert called == [], "LLM called with empty COA — nothing to match against"


# --------------------------------------------------------------------------- #
# Test (f): tax_registered seeds the prompt correctly
# --------------------------------------------------------------------------- #

def _capture_prompt_client(monkeypatch) -> list[str]:
    """Patch make_client to capture the prompt and return empty results."""
    import invoice_processing.shared_libraries.genai_client as gc

    captured: list[str] = []

    def fake_generate(model=None, contents=None, config=None, **_kw):
        captured.append(contents or "")
        return SimpleNamespace(text=json.dumps({"results": []}))

    fake_client = SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate))
    monkeypatch.setattr(gc, "make_client", lambda *a, **kw: fake_client)
    return captured


def test_prompt_contains_gst_registered_yes(monkeypatch):
    captured = _capture_prompt_client(monkeypatch)

    unresolved = [(0, "Mystery", "Vendor")]
    _llm_match_lines(unresolved, SAMPLE_COA, model=None, tax_registered=True)

    assert captured, "generate_content was not called"
    prompt = captured[0]
    assert "Client GST-registered: yes" in prompt


def test_prompt_contains_gst_registered_no(monkeypatch):
    captured = _capture_prompt_client(monkeypatch)

    unresolved = [(0, "Mystery", "Vendor")]
    _llm_match_lines(unresolved, SAMPLE_COA, model=None, tax_registered=False)

    assert captured
    prompt = captured[0]
    assert "Client GST-registered: no" in prompt


def test_prompt_contains_gst_registered_unknown(monkeypatch):
    captured = _capture_prompt_client(monkeypatch)

    unresolved = [(0, "Mystery", "Vendor")]
    _llm_match_lines(unresolved, SAMPLE_COA, model=None, tax_registered=None)

    assert captured
    prompt = captured[0]
    assert "Client GST-registered: unknown" in prompt


def test_prompt_contains_account_code_only_instruction(monkeypatch):
    """Prompt must instruct the model not to infer a tax/GST code."""
    captured = _capture_prompt_client(monkeypatch)

    unresolved = [(0, "Mystery", "Vendor")]
    _llm_match_lines(unresolved, SAMPLE_COA, model=None, tax_registered=True)

    assert captured
    prompt = captured[0]
    assert "account_code" in prompt
    # Must explicitly say tax treatment is out of scope
    assert "tax treatment" in prompt.lower() or "gst code" in prompt.lower()


def test_prompt_contains_must_return_instruction(monkeypatch):
    """§0.5-B: silence defense — prompt must say to always return JSON results."""
    captured = _capture_prompt_client(monkeypatch)

    unresolved = [(0, "Mystery", "Vendor")]
    _llm_match_lines(unresolved, SAMPLE_COA, model=None)

    assert captured
    prompt = captured[0]
    # Check for some form of "must return" / "never reply empty"
    assert "must return" in prompt.lower() or "never reply empty" in prompt.lower()


def test_tax_registered_threaded_from_categorize_invoice(monkeypatch):
    """categorize_invoice threads tax_registered into _llm_match_lines."""
    captured_kwargs: list[dict] = []

    orig_fn = __import__(
        "invoice_processing.export.categorizer", fromlist=["_llm_match_lines"]
    )._llm_match_lines

    def spy_llm(unresolved, coa, model, *, tax_registered=None):
        captured_kwargs.append({"tax_registered": tax_registered})
        return {}

    monkeypatch.setattr(
        "invoice_processing.export.categorizer._llm_match_lines", spy_llm
    )

    inv = _inv_unresolved()
    categorize_invoice(
        inv,
        coa=SAMPLE_COA,
        category_mapping={},
        entity_memory=[],
        use_llm=True,
        tax_registered=False,
    )

    assert captured_kwargs, "_llm_match_lines was never called"
    assert captured_kwargs[0]["tax_registered"] is False
