"""Backward-compatible socket-mode entrypoint.

The real Slack ↔ ADK driver now lives in
``accounting_agents.slack_runner`` (Slack I/O owner; the ADK graph stays
Slack-agnostic). This module is a thin shim so existing ``python slack_bot.py``
invocations keep working.
"""

from __future__ import annotations

from accounting_agents.slack_runner import main

if __name__ == "__main__":
    main()
