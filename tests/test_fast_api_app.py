"""Tests for the production FastAPI server routes (accounting_agents.fast_api_app).

Hermetic: no live Slack/GCP. The Bolt request handler is stubbed and the app's
lifespan (which would build a real Firestore-backed handler) is intentionally
NOT entered — TestClient is used WITHOUT its context manager so startup events
do not run, and ``_bolt_handler`` is monkeypatched directly.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi import Response
from fastapi.testclient import TestClient

from accounting_agents import fast_api_app


def _registered_get_paths() -> set[str]:
    """Return the set of GET route paths registered on the FastAPI app."""
    paths: set[str] = set()
    for route in fast_api_app.app.routes:
        methods = getattr(route, "methods", None) or set()
        if "GET" in methods:
            paths.add(route.path)
    return paths


def test_oauth_routes_are_registered_as_get():
    """The two OAuth endpoints (and existing /healthz) must be GET routes."""
    paths = _registered_get_paths()
    assert "/slack/install" in paths
    assert "/slack/oauth_redirect" in paths
    assert "/healthz" in paths


def test_slack_install_delegates_to_bolt_handler(monkeypatch):
    """GET /slack/install forwards the request to the Bolt handler."""
    handler = AsyncMock()
    handler.handle.return_value = Response(content="install-ok", status_code=200)
    monkeypatch.setattr(fast_api_app, "_bolt_handler", handler)

    # No ``with client:`` — lifespan is not entered, so the real Firestore-backed
    # handler is never built; our stub stays in place.
    client = TestClient(fast_api_app.app)
    resp = client.get("/slack/install")

    assert resp.status_code == 200
    assert resp.text == "install-ok"
    handler.handle.assert_awaited()


def test_slack_oauth_redirect_delegates_to_bolt_handler(monkeypatch):
    """GET /slack/oauth_redirect forwards the request to the Bolt handler."""
    handler = AsyncMock()
    handler.handle.return_value = Response(content="redirect-ok", status_code=200)
    monkeypatch.setattr(fast_api_app, "_bolt_handler", handler)

    client = TestClient(fast_api_app.app)
    resp = client.get("/slack/oauth_redirect?code=abc&state=xyz")

    assert resp.status_code == 200
    assert resp.text == "redirect-ok"
    handler.handle.assert_awaited()


def test_oauth_routes_return_503_when_handler_not_ready(monkeypatch):
    """Both OAuth routes return 503 when the Bolt handler is not initialized."""
    monkeypatch.setattr(fast_api_app, "_bolt_handler", None)

    client = TestClient(fast_api_app.app)
    install = client.get("/slack/install")
    redirect = client.get("/slack/oauth_redirect")

    assert install.status_code == 503
    assert redirect.status_code == 503
