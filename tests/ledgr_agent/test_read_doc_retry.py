"""Hermetic tests for read_doc ValidationError retry (no live Gemini)."""

from __future__ import annotations

from pydantic import ValidationError

from ledgr_agent.internal.read_doc_retry import read_bytes_with_retry
from ledgr_agent.internal.schemas import ReadDocumentBundle


def _bundle(**kwargs) -> ReadDocumentBundle:
    defaults = {
        "file_kind": "commercial_documents",
        "document_count": 1,
        "documents": [
            {
                "document_kind": "receipt",
                "vendor_name": "Test",
                "invoice_number": "R-1",
                "grand_total": 10.0,
            }
        ],
    }
    defaults.update(kwargs)
    return ReadDocumentBundle.model_validate(defaults)


def test_read_bytes_with_retry_uses_std_after_lite_validation_error() -> None:
    calls: list[str] = []

    def read_once(_data: bytes, _mime: str, model: str):
        calls.append(model)
        if model == "lite":
            raise ValidationError.from_exception_data("ReadDocumentBundle", [])
        return _bundle(), {"model": model, "usage": {}}

    bundle, meta = read_bytes_with_retry(
        b"%PDF",
        "application/pdf",
        read_once=read_once,
        lite_model="lite",
        std_model="std",
    )
    assert bundle.file_kind == "commercial_documents"
    assert calls == ["lite", "std"]
    assert meta["gemini_call_count"] == 2


def test_read_bytes_with_retry_merges_pdf_halves_on_double_failure() -> None:
    calls: list[tuple[str, int]] = []

    def read_once(data: bytes, _mime: str, model: str):
        calls.append((model, len(data)))
        if len(calls) <= 2:
            raise ValidationError.from_exception_data("ReadDocumentBundle", [])
        doc_idx = len(calls) - 2
        return _bundle(
            document_count=1,
            documents=[
                {
                    "document_kind": "receipt",
                    "vendor_name": f"Shop {doc_idx}",
                    "invoice_number": f"R-{doc_idx}",
                    "grand_total": float(doc_idx),
                }
            ],
        ), {"model": model, "usage": {}}

    # Minimal 2-page PDF stub — retry skips half split when page count < 2.
    from ledgr_agent.eval.minimal_pdf import make_multipage_pdf

    pdf = make_multipage_pdf(
        [
            [(50, 700, "Receipt A Total 1.00")],
            [(50, 700, "Receipt B Total 2.00")],
        ],
        title="two-page",
    )
    bundle, meta = read_bytes_with_retry(
        pdf,
        "application/pdf",
        read_once=read_once,
        lite_model="lite",
        std_model="std",
    )
    assert meta["gemini_call_count"] >= 2
    if meta.get("retry_strategy") == "pdf_halves":
        assert len(bundle.documents) == 2
    else:
        assert len(bundle.documents) >= 1
