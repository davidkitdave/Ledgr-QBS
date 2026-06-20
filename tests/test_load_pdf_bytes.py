"""Tests for _load_pdf_bytes fallback chain (playground / adk-web support).

TDD: tests were written FIRST against the new spec, then the implementation
was added.  Hermetic — no Gemini, no network, no real artifact service.

Covers:
(a) Slack path unchanged: state has ARTIFACT_NAME_KEY -> load_artifact called,
    returns bytes+mime; no user_content used.
(b) Playground path: no key, ctx.user_content has a Part with inline_data pdf
    bytes -> returns bytes+mime AND heals state (sets ARTIFACT_NAME_KEY AND
    calls save_artifact).
(c) list_artifacts fallback: no key, no usable user_content part,
    list_artifacts returns a pdf key that load_artifact resolves -> returns bytes.
(d) Nothing available -> raises ValueError.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Optional

import pytest

from accounting_agents import nodes
from accounting_agents.nodes import ARTIFACT_NAME_KEY, _load_pdf_bytes


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

PDF_BYTES = b"%PDF-1.4 fake"
IMG_BYTES = b"\x89PNG fake"


def _part(data: bytes, mime: str) -> SimpleNamespace:
    """Build a fake types.Part with inline_data."""
    inline = SimpleNamespace(data=data, mime_type=mime)
    return SimpleNamespace(inline_data=inline)


def _part_no_inline() -> SimpleNamespace:
    """A Part that carries no inline_data (e.g. a text part)."""
    return SimpleNamespace(inline_data=None)


class FakeContext:
    """Duck-typed stand-in for google.adk.agents.context.Context.

    Mirrors the verified ADK API:
      - ctx.state          mutable dict
      - ctx.user_content   Optional with .parts list (may be None)
      - ctx.load_artifact  async, returns Part | None
      - ctx.list_artifacts async, returns list[str]
      - ctx.save_artifact  async, returns int (version)
    """

    def __init__(
        self,
        *,
        state: Optional[dict] = None,
        artifacts: Optional[dict] = None,   # filename -> Part
        artifact_keys: Optional[list[str]] = None,  # for list_artifacts
        user_content_parts: Optional[list] = None,   # None = no user_content
    ):
        self.state: dict = dict(state or {})
        self._artifacts: dict = dict(artifacts or {})
        self._artifact_keys: list[str] = artifact_keys if artifact_keys is not None else list(self._artifacts.keys())
        # Build user_content only if parts were supplied
        if user_content_parts is not None:
            self.user_content = SimpleNamespace(parts=user_content_parts)
        else:
            self.user_content = None

        # Track calls for assertion
        self.load_artifact_calls: list[str] = []
        self.save_artifact_calls: list[tuple] = []  # (filename, part)

    async def load_artifact(self, filename: str, version=None):
        self.load_artifact_calls.append(filename)
        return self._artifacts.get(filename)

    async def list_artifacts(self) -> list[str]:
        return list(self._artifact_keys)

    async def save_artifact(self, filename: str, artifact) -> int:
        self.save_artifact_calls.append((filename, artifact))
        # Store it so subsequent load_artifact works
        self._artifacts[filename] = artifact
        return 1


# --------------------------------------------------------------------------- #
# (a) Slack path: state key present -> load_artifact, no heal
# --------------------------------------------------------------------------- #

class TestSlackPath:
    """Existing Slack path must be completely unchanged."""

    def test_returns_bytes_and_mime(self):
        art_key = nodes.ARTIFACT_NAME_FMT.format(file_id="F001")
        art = _part(PDF_BYTES, "application/pdf")
        ctx = FakeContext(
            state={ARTIFACT_NAME_KEY: art_key},
            artifacts={art_key: art},
        )
        data, mime = asyncio.run(_load_pdf_bytes(ctx))
        assert data == PDF_BYTES
        assert mime == "application/pdf"

    def test_load_artifact_called_with_correct_name(self):
        art_key = nodes.ARTIFACT_NAME_FMT.format(file_id="F002")
        art = _part(PDF_BYTES, "application/pdf")
        ctx = FakeContext(
            state={ARTIFACT_NAME_KEY: art_key},
            artifacts={art_key: art},
        )
        asyncio.run(_load_pdf_bytes(ctx))
        assert ctx.load_artifact_calls == [art_key]

    def test_no_save_artifact_called(self):
        """Slack path must NOT re-save / re-heal — idempotent."""
        art_key = nodes.ARTIFACT_NAME_FMT.format(file_id="F003")
        art = _part(PDF_BYTES, "application/pdf")
        ctx = FakeContext(
            state={ARTIFACT_NAME_KEY: art_key},
            artifacts={art_key: art},
        )
        asyncio.run(_load_pdf_bytes(ctx))
        assert ctx.save_artifact_calls == []

    def test_missing_artifact_raises_value_error(self):
        """Artifact key in state but artifact service returns None -> ValueError."""
        ctx = FakeContext(
            state={ARTIFACT_NAME_KEY: "inbox/ghost.pdf"},
            artifacts={},  # artifact missing
        )
        with pytest.raises(ValueError, match="missing or has no inline bytes"):
            asyncio.run(_load_pdf_bytes(ctx))

    def test_missing_key_no_fallback_raises_if_no_user_content(self):
        """No key AND no user_content AND empty artifacts -> ValueError."""
        ctx = FakeContext(state={}, artifacts={}, artifact_keys=[])
        with pytest.raises(ValueError):
            asyncio.run(_load_pdf_bytes(ctx))


# --------------------------------------------------------------------------- #
# (b) Playground path: inline_data in user_content.parts
# --------------------------------------------------------------------------- #

class TestPlaygroundInlineData:
    """When ARTIFACT_NAME_KEY is absent but user_content has a PDF part."""

    def test_returns_bytes_and_mime(self):
        part = _part(PDF_BYTES, "application/pdf")
        ctx = FakeContext(
            state={},
            user_content_parts=[part],
        )
        data, mime = asyncio.run(_load_pdf_bytes(ctx))
        assert data == PDF_BYTES
        assert mime == "application/pdf"

    def test_heals_artifact_name_key_in_state(self):
        """After recovery via inline_data, ARTIFACT_NAME_KEY must be set."""
        part = _part(PDF_BYTES, "application/pdf")
        ctx = FakeContext(state={}, user_content_parts=[part])
        asyncio.run(_load_pdf_bytes(ctx))
        assert ARTIFACT_NAME_KEY in ctx.state, (
            "Healing must set ARTIFACT_NAME_KEY so downstream nodes behave like Slack path."
        )

    def test_save_artifact_called_once(self):
        """Healing must call save_artifact to persist bytes for downstream nodes."""
        part = _part(PDF_BYTES, "application/pdf")
        ctx = FakeContext(state={}, user_content_parts=[part])
        asyncio.run(_load_pdf_bytes(ctx))
        assert len(ctx.save_artifact_calls) == 1, (
            "save_artifact must be called exactly once during healing."
        )

    def test_save_artifact_filename_matches_state_key(self):
        """The filename saved must match what's stored in state."""
        part = _part(PDF_BYTES, "application/pdf")
        ctx = FakeContext(state={}, user_content_parts=[part])
        asyncio.run(_load_pdf_bytes(ctx))
        saved_filename, _ = ctx.save_artifact_calls[0]
        assert ctx.state[ARTIFACT_NAME_KEY] == saved_filename

    def test_skips_parts_without_inline_data(self):
        """Parts with no inline_data (text, etc.) must be skipped."""
        text_part = _part_no_inline()
        pdf_part = _part(PDF_BYTES, "application/pdf")
        ctx = FakeContext(state={}, user_content_parts=[text_part, pdf_part])
        data, _ = asyncio.run(_load_pdf_bytes(ctx))
        assert data == PDF_BYTES

    def test_image_mime_accepted(self):
        """image/* mime types are accepted as document bytes."""
        img_part = _part(IMG_BYTES, "image/png")
        ctx = FakeContext(state={}, user_content_parts=[img_part])
        data, mime = asyncio.run(_load_pdf_bytes(ctx))
        assert data == IMG_BYTES
        assert mime == "image/png"

    def test_octet_stream_treated_as_pdf(self):
        """application/octet-stream with non-empty bytes should be accepted."""
        part = _part(PDF_BYTES, "application/octet-stream")
        ctx = FakeContext(state={}, user_content_parts=[part])
        data, mime = asyncio.run(_load_pdf_bytes(ctx))
        assert data == PDF_BYTES
        # mime should be normalised to application/pdf (per spec: treat as pdf)
        assert mime == "application/pdf"

    def test_empty_inline_data_skipped(self):
        """A part with inline_data.data == b'' must be skipped (not usable)."""
        empty_part = _part(b"", "application/pdf")
        pdf_part = _part(PDF_BYTES, "application/pdf")
        ctx = FakeContext(state={}, user_content_parts=[empty_part, pdf_part])
        data, _ = asyncio.run(_load_pdf_bytes(ctx))
        assert data == PDF_BYTES

    def test_user_content_none_falls_through(self):
        """user_content=None must not crash — falls through to list_artifacts."""
        ctx = FakeContext(state={}, artifact_keys=[], artifacts={})
        # user_content is None (default), no artifacts -> ValueError
        with pytest.raises(ValueError):
            asyncio.run(_load_pdf_bytes(ctx))

    def test_parts_none_falls_through(self):
        """user_content.parts=None must not crash — falls through."""
        ctx = FakeContext(state={})
        ctx.user_content = SimpleNamespace(parts=None)
        ctx._artifact_keys = []
        with pytest.raises(ValueError):
            asyncio.run(_load_pdf_bytes(ctx))

    def test_parts_empty_falls_through(self):
        """user_content.parts=[] must not crash — falls through."""
        ctx = FakeContext(state={}, user_content_parts=[])
        with pytest.raises(ValueError):
            asyncio.run(_load_pdf_bytes(ctx))


# --------------------------------------------------------------------------- #
# (c) list_artifacts fallback
# --------------------------------------------------------------------------- #

class TestListArtifactsFallback:
    """When no key in state and no usable user_content, fall back to list_artifacts."""

    def test_picks_pdf_artifact_and_returns_bytes(self):
        art_key = "inbox/upload.pdf"
        art = _part(PDF_BYTES, "application/pdf")
        ctx = FakeContext(
            state={},
            user_content_parts=[],   # empty parts -> skip inline path
            artifacts={art_key: art},
            artifact_keys=[art_key],
        )
        data, mime = asyncio.run(_load_pdf_bytes(ctx))
        assert data == PDF_BYTES
        assert mime == "application/pdf"

    def test_pdf_key_preferred_over_other_keys(self):
        """When multiple artifacts exist, the .pdf-ending key should be preferred."""
        pdf_key = "inbox/scan.pdf"
        other_key = "inbox/meta.json"
        pdf_art = _part(PDF_BYTES, "application/pdf")
        other_art = _part(b"{}", "application/json")
        ctx = FakeContext(
            state={},
            user_content_parts=[],
            artifacts={pdf_key: pdf_art, other_key: other_art},
            artifact_keys=[other_key, pdf_key],  # pdf listed second
        )
        data, _ = asyncio.run(_load_pdf_bytes(ctx))
        assert data == PDF_BYTES

    def test_heals_state_key_on_list_fallback(self):
        """list_artifacts path must also heal ARTIFACT_NAME_KEY in state."""
        art_key = "inbox/upload.pdf"
        art = _part(PDF_BYTES, "application/pdf")
        ctx = FakeContext(
            state={},
            user_content_parts=[],
            artifacts={art_key: art},
            artifact_keys=[art_key],
        )
        asyncio.run(_load_pdf_bytes(ctx))
        assert ctx.state.get(ARTIFACT_NAME_KEY) == art_key


# --------------------------------------------------------------------------- #
# (d) Nothing available -> ValueError
# --------------------------------------------------------------------------- #

class TestNothingAvailable:
    """All three paths exhausted -> actionable ValueError."""

    def test_raises_value_error(self):
        ctx = FakeContext(state={}, user_content_parts=[], artifact_keys=[], artifacts={})
        with pytest.raises(ValueError):
            asyncio.run(_load_pdf_bytes(ctx))

    def test_error_message_is_actionable(self):
        """The error message must describe what was tried."""
        ctx = FakeContext(state={}, user_content_parts=[], artifact_keys=[], artifacts={})
        with pytest.raises(ValueError, match=r"(artifact|user_content|list_artifact|No PDF|nothing|tried)"):
            asyncio.run(_load_pdf_bytes(ctx))
