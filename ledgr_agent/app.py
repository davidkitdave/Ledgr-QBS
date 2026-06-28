"""ADK App wrapper for the lean ledgr_agent."""

from __future__ import annotations

from google.adk.apps import App

from ledgr_agent.agent import root_agent

ledgr_app = App(root_agent=root_agent, name="ledgr_agent")

__all__ = ["ledgr_app", "root_agent"]
