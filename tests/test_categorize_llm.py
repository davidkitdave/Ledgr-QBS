"""Tests for the LLM path in invoice_processing.export.categorizer.

All tests are fully hermetic — no network, no Gemini API.
The genai call inside _llm_match_lines is stubbed via monkeypatch.

Coverage:
  a. LLM result fills account_code, source="llm_coa", flagged=False (good logprobs)
  b. WS-3.3 logprob gate — low avgLogprobs / narrow margin → flagged=True
  c. LLM returns a key NOT in the COA → rejected, line stays unresolved+flagged
  d. LLM raises / returns empty/malformed → {} returned, no crash, no miscoding
  e. Deterministic short-circuit: all lines resolved → LLM seam never called
  f. tax_registered seeding: prompt contains GST context + "account_code only" instruction
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Optional


from invoice_processing.export.categorizer import (
    UNMAPPED_ACCOUNT_CODE,
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


def _make_logprob_response(
    text: str,
    *,
    avg_logprobs: float = -0.1,
    margin: float = 1.5,
):
    """Build a fake Gemini response with avgLogprobs + top-1→top-2 margin."""
    lp1 = -0.2
    lp2 = lp1 - margin
    top_step = SimpleNamespace(
        candidates=[
            SimpleNamespace(log_probability=lp1, token="6001"),
            SimpleNamespace(log_probability=lp2, token="6200"),
        ]
    )
    candidate = SimpleNamespace(
        avg_logprobs=avg_logprobs,
        logprobs_result=SimpleNamespace(top_candidates=[top_step]),
        text=text,
    )
    return SimpleNamespace(candidates=[candidate], text=text)


def _make_fake_client(
    text: str,
    *,
    avg_logprobs: float = -0.1,
    margin: float = 1.5,
    include_logprobs: bool = True,
):
    """Return a fake genai client whose generate_content returns resp.text=text."""
    if include_logprobs:
        resp = _make_logprob_response(text, avg_logprobs=avg_logprobs, margin=margin)
    else:
        resp = SimpleNamespace(text=text, candidates=[])
    model_ns = SimpleNamespace(generate_content=lambda **kwargs: resp)

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


def _canned_result(
    index: int,
    account_code: Optional[str],
    confidence: float,
    *,
    reasoning: str = "stub",
    alternative_codes: Optional[list[str]] = None,
) -> str:
    return json.dumps(
        {
            "results": [
                {
                    "index": index,
                    "account_code": account_code,
                    "reasoning": reasoning,
                    "confidence": confidence,
                    "alternative_codes": alternative_codes or [],
                }
            ]
        }
    )


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

def _stub_make_client(
    monkeypatch,
    canned_text: str,
    *,
    avg_logprobs: float = -0.1,
    margin: float = 1.5,
    include_logprobs: bool = True,
):
    """
    _llm_match_lines does `from ..shared_libraries.genai_client import make_client`
    at call time (inside the function body). We patch the source module attribute
    so the import picks up the fake.
    """
    import invoice_processing.shared_libraries.genai_client as gc

    fake_client = _make_fake_client(
        canned_text,
        avg_logprobs=avg_logprobs,
        margin=margin,
        include_logprobs=include_logprobs,
    )
    monkeypatch.setattr(gc, "make_client", lambda *a, **kw: fake_client)
    return fake_client


# --------------------------------------------------------------------------- #
# Test (a): LLM fills account_code, source=llm_coa, flagged=False with good logprobs
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
    """Directly test _llm_match_lines returns a valid match with good logprobs."""
    canned = _canned_result(0, "6001", 0.85)
    _stub_make_client(monkeypatch, canned)

    unresolved = [(0, "Mystery Service", "Unknown Vendor XYZ")]
    result = _llm_match_lines(unresolved, SAMPLE_COA, model=None)

    assert 0 in result
    assert result[0]["account_code"] == "6001"
    assert result[0]["confidence"] == 0.85
    assert result[0]["flagged"] is False


def test_categorize_invoice_propagates_account_flagged_from_logprob_gate(monkeypatch):
    """WS-3.4: weak logprobs → line.account_flagged=True with reason on InvoiceLine."""
    canned = _canned_result(0, "6001", 0.99)
    _stub_make_client(monkeypatch, canned, avg_logprobs=-2.0, margin=1.5)

    inv = _inv_unresolved()
    result = categorize_invoice(
        inv,
        coa=SAMPLE_COA,
        category_mapping={},
        entity_memory=[],
        use_llm=True,
    )

    line = result.lines[0]
    assert line.account_code in ("6001", "Office Expenses")
    assert line.account_flagged is True
    assert "low_avg_logprobs" in (line.account_flag_reason or "")


def test_categorize_invoice_confident_llm_pick_not_account_flagged(monkeypatch):
    """WS-3.4: strong logprobs → account_flagged=False on InvoiceLine."""
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

    assert result.lines[0].account_flagged is False
    assert result.lines[0].account_flag_reason is None


def test_entity_memory_resolution_not_account_flagged():
    """Deterministic entity-memory hit must not set account_flagged."""
    mem = [_entity("Acme Supplier", "6001")]
    inv = NormalizedInvoice(
        doc_type="purchase",
        supplier=PartyInfo(name="Acme Supplier"),
        lines=[InvoiceLine(description="Office supplies")],
    )
    result = categorize_invoice(
        inv,
        coa=SAMPLE_COA,
        category_mapping={},
        entity_memory=mem,
        use_llm=False,
    )
    assert result.lines[0].account_flagged is False


# --------------------------------------------------------------------------- #
# Test (b): WS-3.3 logprob gate — self-reported confidence is advisory only
# --------------------------------------------------------------------------- #

def test_llm_low_self_reported_confidence_good_logprobs_not_flagged(monkeypatch):
    """Self-reported confidence 0.3 but strong logprobs → not flagged."""
    canned = _canned_result(0, "6001", 0.3)
    _stub_make_client(monkeypatch, canned, avg_logprobs=-0.1, margin=1.5)

    unresolved = [(0, "Mystery Service", "Unknown Vendor XYZ")]
    match = _llm_match_lines(unresolved, SAMPLE_COA, model=None)
    assert match[0]["confidence"] == 0.3
    assert match[0]["flagged"] is False


def test_llm_high_self_reported_confidence_bad_logprobs_flagged(monkeypatch):
    """Self-reported confidence 0.99 but weak logprobs → flagged."""
    canned = _canned_result(0, "6001", 0.99)
    _stub_make_client(monkeypatch, canned, avg_logprobs=-2.0, margin=1.5)

    unresolved = [(0, "Mystery Service", "Unknown Vendor XYZ")]
    match = _llm_match_lines(unresolved, SAMPLE_COA, model=None)
    assert match[0]["flagged"] is True
    assert "low_avg_logprobs" in match[0]["logprob_flag_reason"]


def test_llm_narrow_logprob_margin_flagged(monkeypatch):
    """Ambiguous top-1 vs top-2 margin → flagged (ambiguous-two-accounts case)."""
    canned = _canned_result(0, "6001", 0.95)
    _stub_make_client(monkeypatch, canned, avg_logprobs=-0.1, margin=0.05)

    unresolved = [(0, "Bank transaction fee", "Generic Bank")]
    match = _llm_match_lines(unresolved, SAMPLE_COA, model=None)
    assert match[0]["flagged"] is True
    assert "narrow_margin" in match[0]["logprob_flag_reason"]


def test_llm_missing_logprobs_flagged(monkeypatch):
    """Missing logprob data → conservative flag."""
    canned = _canned_result(0, "6001", 0.95)
    import invoice_processing.shared_libraries.genai_client as gc

    resp = SimpleNamespace(text=canned, candidates=[])
    fake_client = SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda **kw: resp)
    )
    monkeypatch.setattr(gc, "make_client", lambda *a, **kw: fake_client)

    unresolved = [(0, "Mystery Service", "Unknown Vendor XYZ")]
    match = _llm_match_lines(unresolved, SAMPLE_COA, model=None)
    assert match[0]["flagged"] is True
    assert "missing_logprobs" in match[0]["logprob_flag_reason"]


def test_generate_content_requests_logprobs(monkeypatch):
    """GenerateContentConfig must request response_logprobs and logprobs=5."""
    import invoice_processing.shared_libraries.genai_client as gc

    captured_configs: list[object] = []

    def fake_generate(model=None, contents=None, config=None, **_kw):
        captured_configs.append(config)
        return _make_logprob_response(json.dumps({"results": []}))

    fake_client = SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate))
    monkeypatch.setattr(gc, "make_client", lambda *a, **kw: fake_client)

    unresolved = [(0, "Mystery", "Vendor")]
    _llm_match_lines(unresolved, SAMPLE_COA, model=None)

    assert captured_configs
    config = captured_configs[0]
    assert config.response_logprobs is True
    assert config.logprobs == 5


# --------------------------------------------------------------------------- #
# Test (c): LLM returns key NOT in COA → rejected, key becomes None
# --------------------------------------------------------------------------- #

def test_llm_hallucinated_key_rejected(monkeypatch):
    canned = _canned_result(0, "HALLUCINATED_KEY_9999", 0.9)
    _stub_make_client(monkeypatch, canned)

    unresolved = [(0, "Mystery Service", "Unknown Vendor XYZ")]
    result = _llm_match_lines(unresolved, SAMPLE_COA, model=None)

    # The hallucinated key must be nullified
    assert result[0]["account_code"] is None
    assert result[0]["flagged"] is True


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
# WS-3.1: UNMAPPED sentinel + abstention
# --------------------------------------------------------------------------- #

def test_llm_unmapped_abstention_returns_none(monkeypatch):
    canned = _canned_result(0, UNMAPPED_ACCOUNT_CODE, 0.92)
    _stub_make_client(monkeypatch, canned)

    unresolved = [(0, "Staff salary payment", "Payroll Co")]
    result = _llm_match_lines(unresolved, SAMPLE_COA, model=None)

    assert result[0]["account_code"] is None
    assert result[0]["flagged"] is True


def test_llm_null_account_code_abstention(monkeypatch):
    canned = _canned_result(0, None, 0.5)
    _stub_make_client(monkeypatch, canned)

    unresolved = [(0, "Staff salary payment", "Payroll Co")]
    result = _llm_match_lines(unresolved, SAMPLE_COA, model=None)

    assert result[0]["account_code"] is None
    assert result[0]["flagged"] is True


def test_llm_unmapped_abstention_blank_account_code(monkeypatch):
    canned = _canned_result(0, UNMAPPED_ACCOUNT_CODE, 0.95)
    _stub_make_client(monkeypatch, canned)

    inv = _inv_unresolved("Staff salary payment for December 2025")
    result = categorize_invoice(
        inv,
        coa=SAMPLE_COA,
        category_mapping={},
        entity_memory=[],
        use_llm=True,
    )

    assert result.lines[0].account_code == ""


def test_prompt_instructs_unmapped_abstention(monkeypatch):
    captured = _capture_prompt_client(monkeypatch)

    unresolved = [(0, "Staff salary payment", "Payroll Co")]
    _llm_match_lines(unresolved, SAMPLE_COA, model=None)

    assert captured
    prompt = captured[0]
    assert UNMAPPED_ACCOUNT_CODE in prompt
    assert "abstain" in prompt.lower() or "no account" in prompt.lower()


def test_response_schema_includes_unmapped_enum(monkeypatch):
    import invoice_processing.shared_libraries.genai_client as gc

    captured_configs: list[object] = []

    def fake_generate(model=None, contents=None, config=None, **_kw):
        captured_configs.append(config)
        return SimpleNamespace(text=json.dumps({"results": []}))

    fake_client = SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate))
    monkeypatch.setattr(gc, "make_client", lambda *a, **kw: fake_client)

    unresolved = [(0, "Mystery", "Vendor")]
    _llm_match_lines(unresolved, SAMPLE_COA, model=None)

    assert captured_configs
    schema = captured_configs[0].response_schema
    item_props = schema["properties"]["results"]["items"]["properties"]
    enum_values = item_props["account_code"]["enum"]
    assert UNMAPPED_ACCOUNT_CODE in enum_values
    assert "6001" in enum_values
    assert "6200" in enum_values
    alt_enum = item_props["alternative_codes"]["items"]["enum"]
    assert UNMAPPED_ACCOUNT_CODE not in alt_enum


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

    orig_fn = __import__(  # noqa: F841 — captured for reference; not called in this spy pattern
        "invoice_processing.export.categorizer", fromlist=["_llm_match_lines"]
    )._llm_match_lines

    def spy_llm(unresolved, coa, model, *, tax_registered=None, **kwargs):
        captured_kwargs.append({"tax_registered": tax_registered, **kwargs})
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


# --------------------------------------------------------------------------- #
# WS4.5 — contact-master name canonicalization
# --------------------------------------------------------------------------- #
from invoice_processing.export.categorizer import canonical_party_name  # noqa: E402


def _make_party(name: str, reg_no: str | None = None):
    return PartyInfo(name=name, gst_regno=reg_no)


def _em(name: str, reg_no: str | None = None) -> EntityMemoryEntry:
    return EntityMemoryEntry(name=name, reg_no=reg_no)


class TestCanonicalPartyName:
    """Unit tests for canonical_party_name helper."""

    def test_exact_normalized_name_returns_canonical(self):
        """Lowercase variant 'acme pte ltd' matches entry 'Acme Pte Ltd'."""
        party = _make_party("acme pte ltd")
        result = canonical_party_name(party, [_em("Acme Pte Ltd")])
        assert result == "Acme Pte Ltd"

    def test_uppercase_dotted_variant_matches_via_reg_no(self):
        """'ACME PTE. LTD.' has a different normalized form but same reg_no → canonical."""
        party = _make_party("ACME PTE. LTD.", reg_no="200012345A")
        result = canonical_party_name(party, [_em("Acme Pte Ltd", reg_no="200012345A")])
        assert result == "Acme Pte Ltd"

    def test_partial_overlap_not_merged(self):
        """'Acme Industries' partially overlaps 'Acme Pte Ltd' but must NOT match."""
        party = _make_party("Acme Industries")
        result = canonical_party_name(party, [_em("Acme Pte Ltd")])
        assert result is None

    def test_no_match_returns_none(self):
        """Unknown vendor → None (name left unchanged downstream)."""
        party = _make_party("Totally Unknown Vendor")
        result = canonical_party_name(party, [_em("Acme Pte Ltd")])
        assert result is None

    def test_empty_entity_memory_returns_none(self):
        party = _make_party("Acme Pte Ltd")
        result = canonical_party_name(party, [])
        assert result is None

    def test_already_canonical_returns_none(self):
        """No-op when party.name already matches entry.name exactly."""
        party = _make_party("Acme Pte Ltd")
        result = canonical_party_name(party, [_em("Acme Pte Ltd")])
        assert result is None  # no overwrite needed

    def test_empty_entry_name_skipped(self):
        """Entry with empty name is skipped; no false positive."""
        party = _make_party("acmeptelTD")
        result = canonical_party_name(party, [_em("")])
        assert result is None

    def test_reg_no_match_different_printed_name(self):
        """Same reg_no but completely different printed name → canonical applied."""
        party = _make_party("Acme (S) Pte Ltd", reg_no="199900001Z")
        result = canonical_party_name(party, [_em("Acme Pte Ltd", reg_no="199900001Z")])
        assert result == "Acme Pte Ltd"

    def test_reg_no_empty_on_party_no_false_positive(self):
        """party.gst_regno is None → reg_no path never fires."""
        party = _make_party("Acme Pte Ltd", reg_no=None)
        result = canonical_party_name(party, [_em("Different Name", reg_no="200012345A")])
        assert result is None


class TestCategorizeInvoiceCanonical:
    """Integration tests: canonical name written into inv.supplier / inv.customer."""

    _EM = [EntityMemoryEntry(name="Acme Pte Ltd", reg_no="200012345A")]

    def _purchase(self, name: str, reg_no: str | None = None) -> NormalizedInvoice:
        return NormalizedInvoice(
            doc_type="purchase",
            supplier=PartyInfo(name=name, gst_regno=reg_no),
            lines=[InvoiceLine(description="Service Fee")],
        )

    def _sales(self, name: str) -> NormalizedInvoice:
        return NormalizedInvoice(
            doc_type="sales",
            customer=PartyInfo(name=name),
            lines=[InvoiceLine(description="Consulting")],
        )

    def test_variant_name_normalized_on_purchase(self):
        """Uppercase dotted variant resolved via reg_no → canonical in inv.supplier.name."""
        inv = self._purchase("ACME PTE. LTD.", reg_no="200012345A")
        categorize_invoice(inv, coa=[], category_mapping={}, entity_memory=self._EM, use_llm=False)
        assert inv.supplier.name == "Acme Pte Ltd"

    def test_lowercase_variant_normalized_on_purchase(self):
        """Lowercase 'acme pte ltd' → canonical 'Acme Pte Ltd' via name match."""
        inv = self._purchase("acme pte ltd")
        categorize_invoice(inv, coa=[], category_mapping={}, entity_memory=self._EM, use_llm=False)
        assert inv.supplier.name == "Acme Pte Ltd"

    def test_unknown_vendor_name_unchanged(self):
        """No match → supplier.name left untouched."""
        inv = self._purchase("Unknown Vendor Co")
        categorize_invoice(inv, coa=[], category_mapping={}, entity_memory=self._EM, use_llm=False)
        assert inv.supplier.name == "Unknown Vendor Co"

    def test_empty_entity_memory_name_unchanged(self):
        """Empty entity_memory → name left untouched."""
        inv = self._purchase("ACME PTE. LTD.", reg_no="200012345A")
        categorize_invoice(inv, coa=[], category_mapping={}, entity_memory=[], use_llm=False)
        assert inv.supplier.name == "ACME PTE. LTD."

    def test_partial_overlap_not_merged_in_categorize(self):
        """'Acme Industries' shares only a partial token with 'Acme Pte Ltd' — must NOT merge."""
        inv = self._purchase("Acme Industries")
        categorize_invoice(inv, coa=[], category_mapping={}, entity_memory=self._EM, use_llm=False)
        assert inv.supplier.name == "Acme Industries"

    def test_sales_customer_canonicalized(self):
        """For doc_type='sales', inv.customer.name is canonicalized."""
        em = [EntityMemoryEntry(name="Global Corp Pte Ltd")]
        inv = self._sales("global corp pte ltd")
        categorize_invoice(inv, coa=[], category_mapping={}, entity_memory=em, use_llm=False)
        assert inv.customer.name == "Global Corp Pte Ltd"
