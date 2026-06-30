"""Slack frontend package for Ledgr.

This package owns all Slack-facing code. It depends on ``ledgr_agent`` (the
pure agent library) but ``ledgr_agent`` must NEVER import from here.
"""

from accounting_agents.slack_runner import build_fastapi_app  # noqa: F401

__all__ = ["build_fastapi_app"]
