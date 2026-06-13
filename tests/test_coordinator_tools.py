"""Unit tests for the ledgr_coordinator dispatch tools.

These cover the bug that made the playground fail: a tool must read an UPLOADED
file from the ToolContext (inline message parts + session artifacts), not from a
path the model invents. We do NOT assert on classify/process *output* here -- that
is a live LLM call and belongs in eval, not pytest. We test only the deterministic
file-gathering and the no-file guards.
"""

from __future__ import annotations

import asyncio

from google.genai import types

from ledgr_coordinator.tools import (
    _gather_documents,
    inspect_document,
    process_documents,
)


class _StubCtx:
    """Minimal stand-in for ADK ToolContext (only what the tools touch)."""

    def __init__(self, parts=None, artifacts=None):
        self.user_content = (
            types.Content(role="user", parts=parts) if parts is not None else None
        )
        self._artifacts = artifacts or {}

    async def list_artifacts(self):
        return list(self._artifacts.keys())

    async def load_artifact(self, filename, version=None):
        return self._artifacts.get(filename)


def test_gather_reads_inline_uploaded_file():
    pdf = b"%PDF-1.4 fake invoice bytes"
    parts = [
        types.Part(text="please process this"),
        types.Part(inline_data=types.Blob(mime_type="application/pdf", data=pdf)),
    ]
    got = asyncio.run(_gather_documents(_StubCtx(parts=parts)))
    assert len(got) == 1
    _name, data, mime = got[0]
    assert data == pdf
    assert mime == "application/pdf"


def test_gather_reads_persisted_artifact():
    png = b"\x89PNG\r\n fake receipt"
    artifacts = {
        "receipt.png": types.Part(inline_data=types.Blob(mime_type="image/png", data=png))
    }
    parts = [types.Part(text="book this")]
    got = asyncio.run(_gather_documents(_StubCtx(parts=parts, artifacts=artifacts)))
    assert ("receipt.png", png, "image/png") in got


def test_gather_reads_explicit_local_path(tmp_path):
    f = tmp_path / "bill.pdf"
    f.write_bytes(b"%PDF local path bytes")
    got = asyncio.run(_gather_documents(_StubCtx(parts=None), file_paths=[str(f)]))
    assert got == [("bill.pdf", b"%PDF local path bytes", "application/pdf")]


def test_gather_dedupes_same_file_from_two_sources():
    pdf = b"%PDF identical bytes"
    parts = [types.Part(inline_data=types.Blob(mime_type="application/pdf", data=pdf))]
    artifacts = {
        "upload_0": types.Part(inline_data=types.Blob(mime_type="application/pdf", data=pdf))
    }
    got = asyncio.run(_gather_documents(_StubCtx(parts=parts, artifacts=artifacts)))
    assert len(got) == 1  # same (name, size) collapses


def test_no_file_returns_status_not_crash():
    ctx = _StubCtx(parts=[types.Part(text="hello there")])
    assert asyncio.run(inspect_document(ctx))["status"] == "no_file"
    assert asyncio.run(process_documents(ctx))["status"] == "no_file"
