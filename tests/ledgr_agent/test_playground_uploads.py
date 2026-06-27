from __future__ import annotations

import asyncio
from types import SimpleNamespace

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

    def _bundle_stub(_path, **_kw):
        return {
            "documents": [
                {
                    "doc_type": "purchase",
                    "document_kind": "invoice",
                    "vendor_name": "Supplier Inc",
                    "invoice_number": "INV-PLAY-2",
                    "invoice_date": "2026-06-24",
                    "currency": "SGD",
                    "lines": [
                        {
                            "description": "Office supplies",
                            "net_amount": 100.0,
                            "tax_amount": 9.0,
                            "total_amount": 109.0,
                        }
                    ],
                }
            ],
            "document_count": 1,
            "extraction_meta": {"gemini_call_count": 1, "model": "gemini-2.5-flash-lite"},
        }

    result = process_document_batch(
        ctx,
        paths=[],
        read_bundle_fn=_bundle_stub,
    )

    assert result["status"] == "success"
    assert result["validation_summary"]["source_resolution"] == "playground_upload"


def test_process_document_batch_recovers_playground_upload(tmp_path) -> None:
    ctx = FakeToolContext(
        user_content_parts=[
            _part(PDF_BYTES, "application/pdf", file_name="uploaded-invoice.pdf")
        ],
    )

    def _bundle_stub(_path, **_kw):
        return {
            "documents": [
                {
                    "doc_type": "purchase",
                    "document_kind": "invoice",
                    "vendor_name": "Supplier Inc",
                    "invoice_number": "INV-PLAY-1",
                    "invoice_date": "2026-06-24",
                    "currency": "SGD",
                    "lines": [
                        {
                            "description": "Office supplies",
                            "net_amount": 100.0,
                            "tax_amount": 9.0,
                            "total_amount": 109.0,
                        }
                    ],
                }
            ],
            "document_count": 1,
            "extraction_meta": {"gemini_call_count": 1, "model": "gemini-2.5-flash-lite"},
        }

    result = process_document_batch(
        ctx,
        paths=["invoice.png"],
        read_bundle_fn=_bundle_stub,
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
