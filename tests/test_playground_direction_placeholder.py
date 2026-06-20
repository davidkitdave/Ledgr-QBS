"""Tests for the playground-placeholder detection in direction inference.

The ``adk web`` / agents-cli playground seeds a synthetic client name
(``"Playground Client"`` by default). Treating that name as a real client
makes the direction classifier always resolve to ``"unknown"`` (the document
will never name a real "Playground Client"), which defeats the purpose of
running the playground on real invoices. The fix is a small helper that
recognises placeholder names and switches the LLM prompt to a
"reason from document alone" mode.

These tests are hermetic — no Gemini, no network, no Firestore. They only
verify the placeholder-detection branch of the prompt-builder.
"""
from __future__ import annotations

from accounting_agents.nodes import _is_playground_placeholder


class TestPlaygroundPlaceholderDetection:
    def test_default_playground_name_is_placeholder(self):
        assert _is_playground_placeholder("Playground Client") is True

    def test_lowercase_playground_name_is_placeholder(self):
        assert _is_playground_placeholder("playground client") is True

    def test_bare_playground_is_placeholder(self):
        assert _is_playground_placeholder("Playground") is True

    def test_test_client_is_placeholder(self):
        assert _is_playground_placeholder("Test Client") is True

    def test_demo_client_is_placeholder(self):
        assert _is_playground_placeholder("Demo Client") is True

    def test_playground_with_suffix_is_placeholder(self):
        assert _is_playground_placeholder("Playground Acme") is True

    def test_test_with_suffix_is_placeholder(self):
        assert _is_playground_placeholder("Test Org") is True

    def test_empty_string_is_placeholder(self):
        assert _is_playground_placeholder("") is True

    def test_none_is_not_placeholder(self):
        # ``None`` is handled by the caller (it returns "unknown" before even
        # reaching the prompt builder). The helper's job is to recognise string
        # placeholders, not coerce other types.
        assert _is_playground_placeholder(None) is False

    def test_real_client_name_is_not_placeholder(self):
        assert _is_playground_placeholder("Acme Pte Ltd") is False

    def test_real_company_with_playground_substring_is_not_placeholder(self):
        # "playground" appears as a substring but it's not the seed token.
        # Conservative: only exact / prefix matches count as placeholders.
        assert _is_playground_placeholder("Foo Playground Solutions") is False

    def test_non_string_types_are_handled(self):
        assert _is_playground_placeholder(123) is False
        assert _is_playground_placeholder(["Playground Client"]) is False
        assert _is_playground_placeholder({"name": "Playground Client"}) is False
