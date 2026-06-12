"""Lazy environment settings for Ledgr Slack app.

All reads happen at call time — never at import time — so Cloud Run and Agent
Runtime (which set env vars after module import) always see the real values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class Settings:
    slack_bot_token: Optional[str]
    slack_signing_secret: Optional[str]
    slack_app_token: Optional[str]   # xapp-… required for Socket Mode
    gcp_project: Optional[str]
    location: Optional[str]
    gcs_bucket: Optional[str]        # GCS bucket for source + workbook archiving


def get_settings() -> Settings:
    """Read Slack + GCP settings from the environment at call time."""
    return Settings(
        slack_bot_token=os.environ.get("SLACK_BOT_TOKEN"),
        slack_signing_secret=os.environ.get("SLACK_SIGNING_SECRET"),
        slack_app_token=os.environ.get("SLACK_APP_TOKEN"),
        gcp_project=(
            os.environ.get("PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")
        ),
        location=os.environ.get("LOCATION"),
        gcs_bucket=os.environ.get("GCS_BUCKET", "ledgr-qbs-source-bucket"),
    )


def missing_slack_http() -> list[str]:
    """Return names of env vars missing for HTTP (Cloud Run) mode."""
    s = get_settings()
    missing: list[str] = []
    if not s.slack_bot_token:
        missing.append("SLACK_BOT_TOKEN")
    if not s.slack_signing_secret:
        missing.append("SLACK_SIGNING_SECRET")
    return missing


def missing_slack_socket() -> list[str]:
    """Return names of env vars missing for Socket Mode."""
    s = get_settings()
    missing: list[str] = []
    if not s.slack_bot_token:
        missing.append("SLACK_BOT_TOKEN")
    if not s.slack_app_token:
        missing.append("SLACK_APP_TOKEN")
    return missing
