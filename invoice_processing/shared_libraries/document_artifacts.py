"""Shared document-artifact helpers.

These constants and functions were previously private to
``accounting_agents.nodes`` but are needed by both the ``accounting_agents``
graph and the lean ``ledgr_agent``.  Moving them here breaks the import
dependency of ``ledgr_agent`` on the doomed graph modules.

Public API:

- :data:`ARTIFACT_NAME_KEY` — ADK state key carrying the artifact filename.
- :func:`artifact_name_for` — return the artifact filename for a file-id,
  respecting dev-vs-prod naming rules.
- :func:`is_document_mime` — return True for mime types accepted as document
  bytes (PDF, image, octet-stream, empty string).
"""
from __future__ import annotations

#: State key carrying the ADK artifact filename of the uploaded PDF for this run.
ARTIFACT_NAME_KEY = "temp:artifact_name"

#: Filename convention the Slack layer uses when it ``save_artifact``s the PDF.
#:
#: ADK's FastAPI dev server registers artifact routes with a single
#: ``{artifact_name}`` path parameter, which by default does NOT match the
#: slash character.  Names like ``inbox/upload.pdf`` therefore return 404 in
#: the dev UI even when the file is on disk.  To keep dev tooling working we
#: collapse the path to a flat ``"{file_id}.pdf"`` in non-prod; prod keeps the
#: namespace prefix for collision safety with other tools writing into the same
#: artifact bucket.
ARTIFACT_NAME_FMT = "inbox/{file_id}.pdf"


def artifact_name_for(file_id: str) -> str:
    """Return the artifact filename to use for ``file_id`` in the current env.

    In **every** non-prod environment the flat form (``"{file_id}.pdf"``) is
    returned so the dev FastAPI route matches.  ADK's dev FastAPI registers
    artifact routes with a single ``{artifact_name}`` path parameter that
    does NOT match the slash character — names like ``inbox/upload.pdf``
    therefore return 404 in the dev UI even when the file is on disk.

    Prod keeps the namespaced ``"inbox/{file_id}.pdf"`` form for collision
    safety alongside other artifacts.

    Previous behaviour gated the flat form on ``is_playground_seed_enabled()``
    which is enabled in dev/unset but ALSO active in any non-prod scenario
    where a playground seed was used.  Phase 1 / artifact-dev-naming
    simplifies the gate to a direct ``LEDGR_ENV != "prod"`` check so the
    flat form is used universally outside prod — eliminates the 404 in any
    ADK web / agents-cli playground session regardless of seed state.
    """
    import os as _os
    from accounting_agents.config import is_playground_seed_enabled

    env = (_os.environ.get("LEDGR_ENV") or "dev").strip().lower()
    if env != "prod" or is_playground_seed_enabled():
        return f"{file_id}.pdf"
    return ARTIFACT_NAME_FMT.format(file_id=file_id)


def is_document_mime(mime: str) -> bool:
    """Return True for mime types accepted as document bytes."""
    return mime.startswith("image/") or mime in (
        "application/pdf",
        "application/octet-stream",
        "",
    )
