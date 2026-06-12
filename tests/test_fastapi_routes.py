"""Regression: FastAPI route handlers must INJECT the Request object, not treat it
as a query param.

Guards the `from __future__ import annotations` pitfall: with PEP 563 string
annotations, FastAPI resolves `req: Request` against the MODULE globals — so
`Request` must be imported at module level in app/slack_app.py, not inside
fastapi_app(). When it was a local import, every Slack route returned HTTP 422
("missing query param 'req'") and /openapi.json returned 500. This test calls the
routes through TestClient (route-existence checks alone did NOT catch the bug).
"""
from fastapi.testclient import TestClient


def _oauth_env(monkeypatch):
    monkeypatch.setenv("SLACK_CLIENT_ID", "x")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "y")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "z")
    monkeypatch.setenv("SLACK_BASE_URL", "https://x.run.app")


def test_slack_routes_inject_request_not_422(monkeypatch):
    _oauth_env(monkeypatch)
    from app.slack_app import fastapi_app

    client = TestClient(fastapi_app(), raise_server_exceptions=False)

    # /slack/install renders the Bolt install page (200) — NOT a 422 query-validation error.
    assert client.get("/slack/install").status_code != 422
    # POST /slack/events reaches Bolt's signature check (401 for an unsigned request) — NOT 422.
    assert client.post("/slack/events").status_code != 422
    # oauth_redirect with a junk code reaches Bolt (400) — NOT 422.
    assert client.get("/slack/oauth_redirect?code=a&state=b").status_code != 422
    # The OpenAPI schema must generate cleanly (the bug made this 500).
    assert client.get("/openapi.json").status_code == 200
    # /healthz works (200 when OAuth configured).
    assert client.get("/healthz").status_code == 200
