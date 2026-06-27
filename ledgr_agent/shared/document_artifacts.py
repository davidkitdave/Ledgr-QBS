"""ADK document artifact helpers for ledgr_agent."""

from __future__ import annotations

import os

ARTIFACT_NAME_KEY = "temp:artifact_name"
ARTIFACT_NAME_FMT = "inbox/{file_id}.pdf"


def artifact_name_for(file_id: str) -> str:
    env = (os.environ.get("LEDGR_ENV") or "dev").strip().lower()
    if env != "prod":
        return f"{file_id}.pdf"
    return ARTIFACT_NAME_FMT.format(file_id=file_id)


def is_document_mime(mime: str) -> bool:
    return mime.startswith("image/") or mime in (
        "application/pdf",
        "application/octet-stream",
        "",
    )
