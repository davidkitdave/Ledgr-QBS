"""Cloud Run / uvicorn entry point for the Ledgr Slack app.

Run with:
    uvicorn app.main:app --host 0.0.0.0 --port $PORT
"""

from ledgr_agent.billing import wire_playground_credits
from ledgr_slack import build_fastapi_app

wire_playground_credits()

app = build_fastapi_app()
