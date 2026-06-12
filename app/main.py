"""Cloud Run / uvicorn entry point for the Ledgr Slack app.

Run with:
    uvicorn app.main:app --host 0.0.0.0 --port $PORT

Importing this module makes NO network calls.  All token reads and store
construction happen lazily inside fastapi_app() / at first Slack request.
"""

from app.slack_app import fastapi_app

app = fastapi_app()
