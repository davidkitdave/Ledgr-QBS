"""Regression: FastAPI route handlers must INJECT the Request object properly.

Guards the `from __future__ import annotations` pitfall: with PEP 563 string
annotations, FastAPI resolves `req: Request` against the MODULE globals — so
`Request` must be imported at module level, not inside the factory function.
When it was a local import, every Slack route returned HTTP 422
("missing query param 'req'").

Tests build_fastapi_app() from accounting_agents.slack_runner (the new prod
entry point after ADK consolidation Tasks 3+4).

Uses pytest monkeypatch (not with-patch context managers) so that patches stay
active during lazy _get_handler() construction triggered by TestClient requests.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient


@pytest.fixture()
def fastapi_app(monkeypatch):
    """Build build_fastapi_app() with all network/Slack deps monkeypatched."""
    import accounting_agents.sessions as _sessions_mod
    import accounting_agents.slack_runner as _runner_mod
    import invoice_processing.export.client_context as _ctx_mod
    import slack_bolt.adapter.fastapi.async_handler as _handler_mod

    fake_async_app = MagicMock()
    fake_handler = MagicMock()
    fake_handler.handle = AsyncMock(return_value=MagicMock(status_code=401))

    # Module-level attributes in slack_runner — patch directly.
    monkeypatch.setattr(_runner_mod, "build_runner", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(_runner_mod, "build_async_app", MagicMock(return_value=fake_async_app))
    monkeypatch.setattr(_runner_mod, "SlackLedgerStore", MagicMock(return_value=MagicMock()))

    # Function-local imports inside _get_handler — patch the SOURCE modules.
    monkeypatch.setattr(_sessions_mod, "FirestoreSessionService", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(_ctx_mod, "FirestoreClientStore", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(_handler_mod, "AsyncSlackRequestHandler", MagicMock(return_value=fake_handler))

    from accounting_agents.slack_runner import build_fastapi_app
    return build_fastapi_app()


def test_slack_events_route_exists(fastapi_app):
    """POST /slack/events must be registered (not 404/422)."""
    client = TestClient(fastapi_app, raise_server_exceptions=False)
    # Without a valid Slack signature the handler returns 401/400, not 404/422.
    resp = client.post("/slack/events")
    assert resp.status_code != 404
    assert resp.status_code != 422


def test_healthz_route_exists(fastapi_app):
    """GET /healthz must be present and return JSON."""
    client = TestClient(fastapi_app, raise_server_exceptions=False)
    resp = client.get("/healthz")
    assert resp.status_code in (200, 503)
    assert resp.headers["content-type"].startswith("application/json")


def test_openapi_schema_generates_cleanly(fastapi_app):
    """The OpenAPI schema must generate without errors (no 500)."""
    client = TestClient(fastapi_app, raise_server_exceptions=False)
    assert client.get("/openapi.json").status_code == 200
