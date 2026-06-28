from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import ledgr_agent.billing as billing
from ledgr_agent.internal.schemas import ReadDocument, ReadDocumentBundle
from ledgr_agent.billing import CreditService, InMemoryCreditStore, configure_shared_credit_service
from ledgr_agent.internal.uploads import materialize_playground_uploads, resolve_document_paths
from ledgr_agent.tools.read_doc import read_doc


@pytest.fixture(autouse=True)
def _credit_setup() -> None:
    billing._shared_credit_service = None
    service = CreditService(InMemoryCreditStore())
    service.ensure_firm("T_TEST")
    service.grant("T_TEST", 10, note="test")
    configure_shared_credit_service(service)
    yield
    billing._shared_credit_service = None


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


@patch("ledgr_agent.tools.read_doc.make_client")
def test_read_doc_recovers_empty_paths_playground_upload(mock_make_client) -> None:
    ctx = FakeToolContext(
        state={"firm_id": "T_TEST"},
        user_content_parts=[
            _part(PDF_BYTES, "application/pdf", file_name="uploaded-invoice.pdf")
        ],
    )
    bundle = ReadDocumentBundle(
        file_kind="commercial_documents",
        documents=[
            ReadDocument(
                doc_type="purchase",
                vendor_name="Supplier Inc",
                invoice_number="INV-PLAY-2",
                lines=[{"description": "Office supplies", "net_amount": 100.0}],
            )
        ],
        document_count=1,
    )
    mock_resp = MagicMock()
    mock_resp.text = bundle.model_dump_json()
    mock_make_client.return_value.models.generate_content.return_value = mock_resp

    result = read_doc(ctx, paths=[])

    assert result["status"] == "success"
    assert result["vendor_name"] == "Supplier Inc"


@patch("ledgr_agent.tools.read_doc.make_client")
def test_read_doc_recovers_playground_upload(mock_make_client) -> None:
    ctx = FakeToolContext(
        state={"firm_id": "T_TEST"},
        user_content_parts=[
            _part(PDF_BYTES, "application/pdf", file_name="uploaded-invoice.pdf")
        ],
    )
    bundle = ReadDocumentBundle(
        file_kind="commercial_documents",
        documents=[
            ReadDocument(
                doc_type="purchase",
                vendor_name="Supplier Inc",
                invoice_number="INV-PLAY-1",
                lines=[{"description": "Office supplies", "net_amount": 100.0}],
            )
        ],
        document_count=1,
    )
    mock_resp = MagicMock()
    mock_resp.text = bundle.model_dump_json()
    mock_make_client.return_value.models.generate_content.return_value = mock_resp

    result = read_doc(ctx, paths=["invoice.png"])

    assert result["status"] == "success"
    assert result["invoice_number"] == "INV-PLAY-1"
