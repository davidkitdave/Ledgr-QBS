from __future__ import annotations

import asyncio
from types import SimpleNamespace

from invoice_processing.classify.document_classifier import ClassificationResult
from invoice_processing.extract.invoice_extractor import ExtractedInvoice, ExtractedLine
from ledgr_agent.tools.document_tools import process_document_batch
from ledgr_agent.tools.playground_uploads import materialize_playground_uploads, resolve_document_paths


PDF_BYTES = b"%PDF-1.4 fake playground upload"


def _part(data: bytes, mime: str, *, file_name: str | None = None) -> SimpleNamespace:
    inline = SimpleNamespace(data=data, mime_type=mime)
    metadata = {"file_name": file_name} if file_name else None
    return SimpleNamespace(inline_data=inline, part_metadata=metadata)


class FakeToolContext:
    def __init__(
        self,
        *,
        state: dict | None = None,
        artifacts: dict | None = None,
        artifact_keys: list[str] | None = None,
        user_content_parts: list | None = None,
        session_id: str = "session-test",
    ) -> None:
        self.state = dict(state or {})
        self._artifacts = dict(artifacts or {})
        self._artifact_keys = artifact_keys if artifact_keys is not None else list(self._artifacts.keys())
        if user_content_parts is not None:
            self.user_content = SimpleNamespace(parts=user_content_parts)
        else:
            self.user_content = None
        self.session = SimpleNamespace(id=session_id)
        self.invocation_id = "invocation-test"

    async def load_artifact(self, filename: str, version=None):
        return self._artifacts.get(filename)

    async def list_artifacts(self) -> list[str]:
        return list(self._artifact_keys)

    async def save_artifact(self, filename: str, artifact) -> int:
        self._artifacts[filename] = artifact
        return 1


def test_resolve_document_paths_uses_disk_file(tmp_path) -> None:
    pdf = tmp_path / "real-invoice.pdf"
    pdf.write_bytes(PDF_BYTES)

    existing, missing, resolution = resolve_document_paths(None, [str(pdf)])

    assert existing == [pdf]
    assert missing == []
    assert resolution["source_resolution"] == "disk_paths"


def test_materialize_playground_uploads_from_inline_data() -> None:
    ctx = FakeToolContext(
        user_content_parts=[
            _part(
                PDF_BYTES,
                "application/pdf",
                file_name="AAA-25-011 Jan 26 Management fees PG Paid.pdf",
            )
        ],
    )

    staged = asyncio.run(materialize_playground_uploads(ctx))

    assert len(staged) == 1
    assert staged[0].is_file()
    assert staged[0].read_bytes() == PDF_BYTES
    assert staged[0].name.endswith(".pdf")


def test_resolve_document_paths_ignores_wrong_guess_and_uses_upload() -> None:
    ctx = FakeToolContext(
        user_content_parts=[
            _part(
                PDF_BYTES,
                "application/pdf",
                file_name="AAA-25-011 Jan 26 Management fees PG Paid.pdf",
            )
        ],
    )

    existing, missing, resolution = resolve_document_paths(ctx, ["invoice.png"])

    assert len(existing) == 1
    assert existing[0].is_file()
    assert missing == []
    assert resolution["source_resolution"] == "playground_upload"
    assert resolution["ignored_paths"] == ["invoice.png"]


def test_process_document_batch_recovers_empty_paths_playground_upload() -> None:
    ctx = FakeToolContext(
        user_content_parts=[
            _part(PDF_BYTES, "application/pdf", file_name="uploaded-invoice.pdf")
        ],
    )

    def _classify(path, **_kw):
        return ClassificationResult(
            doc_type="invoice",
            confidence=0.99,
            issuer_name="Supplier Inc",
            bill_to_name="Playground Client",
            reason="test",
        )

    def _direction(cls, **_kw):
        return "purchase"

    def _extract_stub(path, **_kw):
        return ExtractedInvoice(
            doc_type="invoice",
            invoice_number="INV-PLAY-2",
            invoice_date="2026-06-24",
            currency="SGD",
            issuer_name="Supplier Inc",
            issuer_gst_regno="200012345A",
            bill_to_name="Playground Client",
            lines=[
                ExtractedLine(
                    description="Office supplies",
                    net_amount=100.0,
                    gst_amount=9.0,
                    tax_label="SR",
                )
            ],
            subtotal=100.0,
            gst_total=9.0,
            total=109.0,
            issuer_tax_system="NONE",
        )

    def stub_cat(inv, **kw):
        if inv.lines:
            inv.lines[0].account_code = "6100"

    result = process_document_batch(
        ctx,
        paths=[],
        classify_fn=_classify,
        direction_fn=_direction,
        extract_fn=_extract_stub,
        categorize_fn=stub_cat,
    )

    assert result["status"] == "success"
    assert result["validation_summary"]["source_resolution"] == "playground_upload"


def test_process_document_batch_recovers_playground_upload(tmp_path) -> None:
    ctx = FakeToolContext(
        user_content_parts=[
            _part(PDF_BYTES, "application/pdf", file_name="uploaded-invoice.pdf")
        ],
    )

    def _classify(path, **_kw):
        return ClassificationResult(
            doc_type="invoice",
            confidence=0.99,
            issuer_name="Supplier Inc",
            bill_to_name="Playground Client",
            reason="test",
        )

    def _direction(cls, **_kw):
        return "purchase"

    def _extract_stub(path, **_kw):
        return ExtractedInvoice(
            doc_type="invoice",
            invoice_number="INV-PLAY-1",
            invoice_date="2026-06-24",
            currency="SGD",
            issuer_name="Supplier Inc",
            issuer_gst_regno="200012345A",
            bill_to_name="Playground Client",
            lines=[
                ExtractedLine(
                    description="Office supplies",
                    net_amount=100.0,
                    gst_amount=9.0,
                    tax_label="SR",
                )
            ],
            subtotal=100.0,
            gst_total=9.0,
            total=109.0,
            issuer_tax_system="NONE",
        )

    def stub_cat(inv, **kw):
        if inv.lines:
            inv.lines[0].account_code = "6100"

    result = process_document_batch(
        ctx,
        paths=["invoice.png"],
        classify_fn=_classify,
        direction_fn=_direction,
        extract_fn=_extract_stub,
        categorize_fn=stub_cat,
    )

    assert result["status"] == "success"
    assert result["documents_processed"] == 1
    assert result["validation_summary"]["source_resolution"] == "playground_upload"
    assert result["validation_summary"]["ignored_paths"] == ["invoice.png"]
    assert result["posted_documents"][0]["invoice_number"] == "INV-PLAY-1"


def test_resolve_document_paths_empty_when_no_upload_and_bad_paths() -> None:
    ctx = FakeToolContext(user_content_parts=[])

    existing, missing, resolution = resolve_document_paths(ctx, ["invoice.png"])

    assert existing == []
    assert missing == ["invoice.png"]
    assert resolution == {}
