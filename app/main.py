"""Cloud Run / uvicorn entry point for the Ledgr Slack app.

Run with:
    uvicorn app.main:app --host 0.0.0.0 --port $PORT

Importing this module makes NO network calls.  All token reads and store
construction happen lazily inside build_fastapi_app() / at first Slack request.
"""

from accounting_agents.slack_runner import build_fastapi_app

app = build_fastapi_app()
