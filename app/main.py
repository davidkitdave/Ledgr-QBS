"""Cloud Run / uvicorn entry point for the Ledgr Slack app.

Run with:
    uvicorn app.main:app --host 0.0.0.0 --port $PORT
"""

from ledgr_agent.billing import wire_playground_credits

wire_playground_credits()
from accounting_agents.slack_runner import build_fastapi_app

app = build_fastapi_app()
