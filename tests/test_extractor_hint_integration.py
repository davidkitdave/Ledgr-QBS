"""Integration-style test: extract_invoice_bundle hint wiring (mocked Gemini)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from invoice_processing.extract.invoice_extractor import (
    ExtractedInvoiceBundle,
    extract_invoice_bundle,
)


@pytest.fixture
def mock_genai():
    bundle = ExtractedInvoiceBundle(invoices=[])
    mock_resp = MagicMock()
    mock_resp.text = bundle.model_dump_json()
    with patch("invoice_processing.extract.invoice_extractor.make_client") as mk:
        client = MagicMock()
        mk.return_value = client
        client.models.generate_content.return_value = mock_resp
        yield client


def test_extract_invoice_bundle_passes_hint_to_prompt(mock_genai):
    extract_invoice_bundle(b"pdf-bytes", "application/pdf", hint="read as credit note")
    call = mock_genai.models.generate_content.call_args
    contents = call.kwargs.get("contents") or call[1].get("contents") or call[0][1]
    prompt = contents[1] if isinstance(contents, list) and len(contents) > 1 else str(contents)
    assert "credit note" in prompt
