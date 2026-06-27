from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import re
from pathlib import Path
import tempfile
from typing import Any

from ledgr_agent.shared.document_artifacts import (
    ARTIFACT_NAME_KEY,
    artifact_name_for,
    is_document_mime as _is_document_mime,
)

DocumentPayload = tuple[bytes, str, str | None]

_MIME_EXTENSIONS: dict[str, str] = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/tiff": ".tiff",
}

_STAGING_ROOT = Path(tempfile.gettempdir()) / "ledgr_qbs_playground"


def _extension_for_mime(mime: str) -> str:
    normalized = mime.strip().lower()
    if normalized in ("application/octet-stream", ""):
        return ".pdf"
    return _MIME_EXTENSIONS.get(normalized, ".pdf")


def _safe_staged_name(*, suggested_name: str | None, mime: str, index: int) -> str:
    if suggested_name:
        stem = Path(suggested_name).stem
        cleaned = re.sub(r"[^\w.\- ]+", "_", stem).strip("._ ")
        if cleaned:
            return f"{cleaned}{_extension_for_mime(mime)}"
    return f"upload_{index + 1}{_extension_for_mime(mime)}"


def _payload_fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_mime(mime: str) -> str:
    normalized = (mime or "").strip().lower()
    if normalized in ("application/octet-stream", ""):
        return "application/pdf"
    return normalized or "application/pdf"


def _append_document(
    documents: list[DocumentPayload],
    seen: set[str],
    *,
    data: bytes,
    mime: str,
    suggested_name: str | None = None,
) -> None:
    if not data:
        return
    normalized_mime = _normalize_mime(mime)
    if not _is_document_mime(normalized_mime):
        return
    fingerprint = _payload_fingerprint(data)
    if fingerprint in seen:
        return
    seen.add(fingerprint)
    documents.append((data, normalized_mime, suggested_name))


def _inline_part_name(part: Any) -> str | None:
    for attr in ("file_name", "filename", "display_name"):
        value = getattr(part, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    metadata = getattr(part, "part_metadata", None)
    if isinstance(metadata, dict):
        for key in ("file_name", "filename", "display_name", "name"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


async def _collect_document_payloads(ctx: Any) -> list[DocumentPayload]:
    """Recover uploaded document bytes from ADK session context."""

    documents: list[DocumentPayload] = []
    seen: set[str] = set()

    state = getattr(ctx, "state", None) or {}
    artifact_name = state.get(ARTIFACT_NAME_KEY)
    if isinstance(artifact_name, str) and artifact_name:
        part = await ctx.load_artifact(artifact_name)
        inline = getattr(part, "inline_data", None) if part is not None else None
        data = getattr(inline, "data", None) if inline is not None else None
        mime = getattr(inline, "mime_type", None) if inline is not None else None
        if data:
            _append_document(
                documents,
                seen,
                data=data,
                mime=str(mime or "application/pdf"),
                suggested_name=artifact_name,
            )

    user_content = getattr(ctx, "user_content", None)
    parts = getattr(user_content, "parts", None) if user_content is not None else None
    if parts:
        for part in parts:
            inline = getattr(part, "inline_data", None)
            if inline is None:
                continue
            data = getattr(inline, "data", None)
            mime = getattr(inline, "mime_type", None) or ""
            _append_document(
                documents,
                seen,
                data=data or b"",
                mime=str(mime),
                suggested_name=_inline_part_name(part),
            )

    artifact_keys: list[str] = []
    try:
        artifact_keys = list(await ctx.list_artifacts() or [])
    except Exception:
        artifact_keys = []

    def _artifact_score(key: str) -> int:
        key_lower = key.lower()
        if key_lower.endswith(".pdf"):
            return 2
        if any(key_lower.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".tiff", ".webp")):
            return 1
        return 0

    for artifact_key in sorted(artifact_keys, key=_artifact_score, reverse=True):
        part = await ctx.load_artifact(artifact_key)
        inline = getattr(part, "inline_data", None) if part is not None else None
        data = getattr(inline, "data", None) if inline is not None else None
        mime = getattr(inline, "mime_type", None) if inline is not None else None
        if data:
            _append_document(
                documents,
                seen,
                data=data,
                mime=str(mime or "application/pdf"),
                suggested_name=artifact_key,
            )

    return documents


def _staging_dir_for_context(ctx: Any) -> Path:
    session = getattr(ctx, "session", None)
    session_id = getattr(session, "id", None) if session is not None else None
    if not session_id:
        session_id = getattr(ctx, "invocation_id", None) or "anonymous"
    target_dir = _STAGING_ROOT / str(session_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir


async def materialize_playground_uploads(ctx: Any) -> list[Path]:
    """Write recovered playground uploads to a session-scoped temp directory."""

    payloads = await _collect_document_payloads(ctx)
    if not payloads:
        return []

    target_dir = _staging_dir_for_context(ctx)
    staged_paths: list[Path] = []
    for index, (data, mime, suggested_name) in enumerate(payloads):
        file_name = _safe_staged_name(
            suggested_name=suggested_name,
            mime=mime,
            index=index,
        )
        path = target_dir / file_name
        path.write_bytes(data)
        staged_paths.append(path)

        # Heal Slack/playground artifact state for downstream tools in the same session.
        if not ctx.state.get(ARTIFACT_NAME_KEY):
            heal_name = artifact_name_for("upload")
            try:
                from google.genai import types as genai_types

                saved_part = genai_types.Part(
                    inline_data=genai_types.Blob(data=data, mime_type=mime),
                )
                await ctx.save_artifact(heal_name, saved_part)
                ctx.state[ARTIFACT_NAME_KEY] = heal_name
            except Exception:
                pass

    return staged_paths


def _run_async(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


def resolve_document_paths(
    tool_context: Any,
    paths: list[str],
) -> tuple[list[Path], list[str], dict[str, object]]:
    """Resolve on-disk paths, falling back to ADK playground uploads when needed."""
    import os

    existing: list[Path] = []
    missing: list[str] = []
    for raw in paths:
        expanded = os.path.expandvars(raw)
        expanded = os.path.expanduser(expanded)
        path = Path(expanded)
        if path.is_file():
            existing.append(path)
        elif raw:
            missing.append(raw)

    if existing:
        return existing, missing, {"source_resolution": "disk_paths"}

    if tool_context is None:
        return existing, missing, {}

    staged_paths = _run_async(materialize_playground_uploads(tool_context))
    if not staged_paths:
        return existing, missing, {}

    resolution: dict[str, object] = {
        "source_resolution": "playground_upload",
        "playground_upload_count": len(staged_paths),
        "staged_paths": [str(path) for path in staged_paths],
    }
    if missing:
        resolution["ignored_paths"] = list(missing)
    return staged_paths, [], resolution
