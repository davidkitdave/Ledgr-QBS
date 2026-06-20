"""Focused tests for the bytes-native hybrid path in bank_statement_extractor.

Verifies:
1. A digital PDF delivered as raw bytes uses pdfplumber (NOT vision).
2. A scanned/no-text PDF delivered as raw bytes falls back to vision.
3. The ``extract_bank_node`` (nodes.py) correctly passes bytes from the ADK
   artifact and benefits from the hybrid path end-to-end.

No Gemini / network calls are made — the LLM callables are monkeypatched.
``pdfplumber`` itself is NOT mocked for the digital case; we build a minimal
real PDF with a text layer using reportlab (if available) or by crafting a
raw PDF bytestring that pdfplumber can parse as digital.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any


import invoice_processing.extract.bank_statement_extractor as bse
from accounting_agents import nodes
from invoice_processing.extract.bank_statement_extractor import (
    ExtractedAccount,
    ExtractedBankStatement,
    ExtractedBankTxn,
    extract_bank_statement,
)


# --------------------------------------------------------------------------- #
# Minimal PDF helpers
# --------------------------------------------------------------------------- #

def _make_digital_pdf_bytes() -> bytes:
    """Return bytes of a minimal, real single-page PDF with an embedded text layer.

    We use a hand-crafted but spec-valid PDF so we don't need reportlab.
    pdfplumber can extract the BT/ET text stream from it.  The text must be
    >=200 chars and contain >=5 digits to pass _is_digital's threshold.
    """
    # A PDF whose content stream contains a text block >200 chars with digits.
    body = (
        "BT /F1 12 Tf 50 750 Td "
        "(Bank Statement 2025-01-01 Account 12345678 SGD) Tj "
        "0 -20 Td (Opening Balance 1000.00) Tj "
        "0 -20 Td (01 Jan 2025 Transfer In 500.00 1500.00) Tj "
        "0 -20 Td (02 Jan 2025 GIRO Payment 200.00 1300.00) Tj "
        "0 -20 Td (03 Jan 2025 ATM Withdrawal 100.00 1200.00) Tj "
        "0 -20 Td (04 Jan 2025 Interest Credit 12.50 1212.50) Tj "
        "0 -20 Td (Closing Balance 1212.50) Tj "
        "ET"
    )
    content = body.encode()
    content_len = len(content)

    pdf = (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
        b" /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n"
        + f"4 0 obj\n<< /Length {content_len} >>\nstream\n".encode()
        + content
        + b"\nendstream\nendobj\n"
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"
        b"xref\n0 6\n"
        b"0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000058 00000 n \n"
        b"0000000115 00000 n \n"
        b"0000000266 00000 n \n"
        b"0000000999 00000 n \n"
        b"trailer\n<< /Size 6 /Root 1 0 R >>\n"
        b"startxref\n1099\n%%EOF\n"
    )
    return pdf


def _make_scanned_pdf_bytes() -> bytes:
    """Return bytes that look like a PDF header but have NO text layer.

    pdfplumber will open it but extract_text() returns '' on every page,
    so _is_digital returns False and extract_bank_statement falls back to vision.
    We embed a minimal image-only PDF (no BT stream).
    """
    content = b""  # empty content stream — no text layer  # noqa: F841 — paired probe; sibling list is asserted
    pdf = (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
        b" /Contents 4 0 R /Resources << >> >>\nendobj\n"
        b"4 0 obj\n<< /Length 0 >>\nstream\n\nendstream\nendobj\n"
        b"xref\n0 5\n"
        b"0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000058 00000 n \n"
        b"0000000115 00000 n \n"
        b"0000000245 00000 n \n"
        b"trailer\n<< /Size 5 /Root 1 0 R >>\n"
        b"startxref\n345\n%%EOF\n"
    )
    return pdf


# --------------------------------------------------------------------------- #
# Fake Extracted result (returned by our patched LLM callables)
# --------------------------------------------------------------------------- #

_FAKE_DIGITAL_RESULT = ExtractedBankStatement(
    accounts=[
        ExtractedAccount(
            bank_name="OCBC - 1234",
            account_number="12345678",
            currency="SGD",
            opening_balance=1000.0,
            closing_balance=1212.50,
            transactions=[
                ExtractedBankTxn(date="2025-01-01", description="Transfer In", deposit=500.0, balance=1500.0),
                ExtractedBankTxn(date="2025-01-02", description="GIRO Payment", withdrawal=200.0, balance=1300.0),
            ],
        )
    ]
)

_FAKE_VISION_RESULT = ExtractedBankStatement(
    accounts=[
        ExtractedAccount(
            bank_name="DBS - 9999",
            account_number="9999",
            currency="SGD",
            opening_balance=0.0,
            closing_balance=100.0,
            transactions=[
                ExtractedBankTxn(date="2025-02-01", description="Vision-extracted txn", deposit=100.0, balance=100.0),
            ],
        )
    ]
)


# --------------------------------------------------------------------------- #
# Test 1 — digital PDF bytes → pdfplumber path (vision must NOT be called)
# --------------------------------------------------------------------------- #

def test_digital_pdf_bytes_uses_pdfplumber_not_vision(monkeypatch):
    """When bytes are from a digital PDF, _extract_digital is called; _extract_vision must NOT be."""
    digital_called = []
    vision_called = []  # noqa: F841 — paired probe; sibling list is asserted

    monkeypatch.setattr(bse, "_extract_digital", lambda text, **kw: (digital_called.append(text), _FAKE_DIGITAL_RESULT)[1])
    monkeypatch.setattr(bse, "_extract_vision", lambda data, mime, **kw: (_ for _ in ()).throw(AssertionError("vision should NOT be called for a digital PDF")))

    pdf_bytes = _make_digital_pdf_bytes()
    result, mode = extract_bank_statement(pdf_bytes, "application/pdf")

    assert mode == "digital", f"Expected mode='digital', got {mode!r}"
    assert len(digital_called) == 1, "Expected exactly one _extract_digital call"
    assert result is _FAKE_DIGITAL_RESULT


# --------------------------------------------------------------------------- #
# Test 2 — scanned/no-text PDF bytes → vision path
# --------------------------------------------------------------------------- #

def test_scanned_pdf_bytes_falls_back_to_vision(monkeypatch):
    """When bytes have no text layer, _extract_vision is called; _extract_digital must NOT be."""
    digital_called = []  # noqa: F841 — paired probe; sibling list is asserted
    vision_called = []

    monkeypatch.setattr(bse, "_extract_digital", lambda text, **kw: (_ for _ in ()).throw(AssertionError("digital should NOT be called for a scanned PDF")))
    monkeypatch.setattr(bse, "_extract_vision", lambda data, mime, **kw: (vision_called.append(True), _FAKE_VISION_RESULT)[1])

    pdf_bytes = _make_scanned_pdf_bytes()
    result, mode = extract_bank_statement(pdf_bytes, "application/pdf")

    assert mode == "vision", f"Expected mode='vision', got {mode!r}"
    assert len(vision_called) == 1
    assert result is _FAKE_VISION_RESULT


# --------------------------------------------------------------------------- #
# Test 3 — non-PDF mime type → always vision (no pdfplumber attempt)
# --------------------------------------------------------------------------- #

def test_image_mime_type_always_uses_vision(monkeypatch):
    """image/jpeg bytes → vision path; pdfplumber is never invoked."""
    vision_called = []

    monkeypatch.setattr(bse, "_extract_digital", lambda text, **kw: (_ for _ in ()).throw(AssertionError("digital should NOT be called for an image")))
    monkeypatch.setattr(bse, "_extract_vision", lambda data, mime, **kw: (vision_called.append(True), _FAKE_VISION_RESULT)[1])

    result, mode = extract_bank_statement(b"\xff\xd8\xff", "image/jpeg")

    assert mode == "vision"
    assert len(vision_called) == 1


def test_bank_model_routing_digital_lite_vision_std(monkeypatch):
    """Digital path receives digital_model; vision path receives vision_model."""
    models_seen: dict[str, str] = {}

    def _digital(text, **kw):
        models_seen["digital"] = kw.get("model")
        return _FAKE_DIGITAL_RESULT

    def _vision(data, mime, **kw):
        models_seen["vision"] = kw.get("model")
        return _FAKE_VISION_RESULT

    monkeypatch.setattr(bse, "_extract_digital", _digital)
    monkeypatch.setattr(bse, "_extract_vision", _vision)

    pdf_bytes = _make_digital_pdf_bytes()
    extract_bank_statement(
        pdf_bytes,
        "application/pdf",
        digital_model="gemini-2.5-flash-lite",
        vision_model="gemini-2.5-flash",
    )
    assert models_seen["digital"] == "gemini-2.5-flash-lite"

    models_seen.clear()
    pdf_bytes = _make_scanned_pdf_bytes()
    extract_bank_statement(
        pdf_bytes,
        "application/pdf",
        digital_model="gemini-2.5-flash-lite",
        vision_model="gemini-2.5-flash",
    )
    assert models_seen["vision"] == "gemini-2.5-flash"


# --------------------------------------------------------------------------- #
# Test 4 — extract_bank_node end-to-end: digital bytes → pdfplumber path
# --------------------------------------------------------------------------- #


class _FakeCtx:
    def __init__(self, pdf_bytes: bytes, mime: str = "application/pdf"):
        self.state: dict[str, Any] = {
            nodes.ARTIFACT_NAME_KEY: nodes.ARTIFACT_NAME_FMT.format(file_id="F_BANK"),
        }
        self._pdf_bytes = pdf_bytes
        self._mime = mime

    async def load_artifact(self, filename, version=None):
        inline = SimpleNamespace(data=self._pdf_bytes, mime_type=self._mime)
        return SimpleNamespace(inline_data=inline)


def test_extract_bank_node_digital_bytes_uses_pdfplumber(monkeypatch):
    """extract_bank_node passes artifact bytes to extract_bank_statement.

    With a digital PDF, the node should reach the pdfplumber path (mode='digital')
    and NOT call the vision extractor.
    """
    vision_called = []

    monkeypatch.setattr(bse, "_extract_digital", lambda text, **kw: _FAKE_DIGITAL_RESULT)
    monkeypatch.setattr(bse, "_extract_vision", lambda data, mime, **kw: (vision_called.append(True), _FAKE_VISION_RESULT)[1])

    # Restore EXTRACT_BANK_FN to the real function so the node exercises the real code path.
    import invoice_processing.extract.bank_statement_extractor as real_bse
    saved = nodes.EXTRACT_BANK_FN
    nodes.EXTRACT_BANK_FN = real_bse.extract_bank_statement
    try:
        ctx = _FakeCtx(_make_digital_pdf_bytes())
        event = asyncio.run(nodes.extract_bank_node._func(ctx))
    finally:
        nodes.EXTRACT_BANK_FN = saved

    assert event.output["count"] == 1
    statements = ctx.state[nodes.BANK_STATEMENTS_KEY]
    assert statements[0]["currency"] == "SGD"
    assert len(vision_called) == 0, "vision extractor must NOT be called for a digital PDF"


def test_extract_bank_node_scanned_bytes_uses_vision(monkeypatch):
    """extract_bank_node with a scanned PDF reaches the vision path."""
    digital_called = []

    monkeypatch.setattr(bse, "_extract_digital", lambda text, **kw: (digital_called.append(True), _FAKE_DIGITAL_RESULT)[1])
    monkeypatch.setattr(bse, "_extract_vision", lambda data, mime, **kw: _FAKE_VISION_RESULT)

    import invoice_processing.extract.bank_statement_extractor as real_bse
    saved = nodes.EXTRACT_BANK_FN
    nodes.EXTRACT_BANK_FN = real_bse.extract_bank_statement
    try:
        ctx = _FakeCtx(_make_scanned_pdf_bytes())
        event = asyncio.run(nodes.extract_bank_node._func(ctx))
    finally:
        nodes.EXTRACT_BANK_FN = saved

    assert event.output["count"] == 1
    statements = ctx.state[nodes.BANK_STATEMENTS_KEY]
    assert statements[0]["bank_name"] == "DBS - 9999"
    assert len(digital_called) == 0, "digital extractor must NOT be called for a scanned PDF"
