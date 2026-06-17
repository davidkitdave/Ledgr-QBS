"""Production FastAPI server for Ledgr-QBS.

Architecture
------------
- Plain FastAPI (no A2A).
- ADK ``Runner`` is constructed by :func:`accounting_agents.slack_runner.build_runner`,
  which injects ``FirestoreSessionService`` (persistent HITL resume) and
  ``InMemoryArtifactService`` (PDFs are transient; re-fetched from Slack on resume).
- Slack Bolt ``AsyncApp`` is wired by
  :func:`accounting_agents.slack_runner.build_async_app` and mounted at
  ``/slack/events`` via Bolt's ASGI handler.
- No import-time network calls: ``google.auth.default()`` is NOT called here;
  Firestore / Gemini clients are created lazily on first use inside their
  respective modules.
- ``GOOGLE_GENAI_USE_VERTEXAI=FALSE`` is set by ``accounting_agents.config``
  (imported transitively through ``slack_runner → agent → config``).

Cloud Run entry
---------------
``uvicorn accounting_agents.fast_api_app:app --host 0.0.0.0 --port $PORT``
"""
from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy globals — populated in lifespan so import never touches the network.
# ---------------------------------------------------------------------------
_bolt_handler = None  # slack_bolt.adapter.fastapi.async_handler.AsyncSlackRequestHandler


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    global _bolt_handler
    from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler

    from accounting_agents.ledger_store import SlackLedgerStore
    from accounting_agents.sessions import FirestoreSessionService
    from accounting_agents.slack_runner import build_async_app, build_runner

    db = FirestoreSessionService().client
    runner = build_runner()
    ledger_store = SlackLedgerStore(db)
    bolt_app = build_async_app(runner=runner, ledger_store=ledger_store, db=db)
    _bolt_handler = AsyncSlackRequestHandler(bolt_app)
    logger.info("Ledgr FastAPI server ready (runner app=%s)", runner.app_name)
    yield
    logger.info("Ledgr FastAPI server shutting down.")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Ledgr-QBS",
    description="Ledgr accounting document agent — Slack + ADK 2.0",
    lifespan=lifespan,
)


@app.get("/healthz")
async def healthz() -> dict:
    """Liveness probe for Cloud Run."""
    return {"status": "ok"}


@app.post("/slack/events")
async def slack_events(request: Request) -> Response:
    """Forward all Slack event / action / command payloads to the Bolt app."""
    if _bolt_handler is None:
        return Response(content="server not ready", status_code=503)
    return await _bolt_handler.handle(request)


@app.get("/slack/install")
async def slack_install(request: Request) -> Response:
    """Start the multi-workspace OAuth flow (the public "Add to Slack" link).

    Delegates to the Bolt handler, which serves the install page / redirect to
    Slack's authorize URL when the app is built in OAuth mode. Returns 503 until
    the handler is initialized by the lifespan.
    """
    if _bolt_handler is None:
        return Response(content="server not ready", status_code=503)
    return await _bolt_handler.handle(request)


@app.get("/slack/oauth_redirect")
async def slack_oauth_redirect(request: Request) -> Response:
    """OAuth callback endpoint Slack redirects to after the user approves.

    Must match the redirect URL registered in the Slack app config
    (``…/slack/oauth_redirect``). Delegates to the Bolt handler, which exchanges
    the ``code`` for a bot token and persists the install via the configured
    ``installation_store``. Returns 503 until the handler is initialized.
    """
    if _bolt_handler is None:
        return Response(content="server not ready", status_code=503)
    return await _bolt_handler.handle(request)


# ---------------------------------------------------------------------------
# Local dev entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
