"""Ledgr Coordinator (front-desk) agent package.

Exposes ``root_agent`` so ``adk web`` and the agents-cli playground can discover
and run the coordinator directly.
"""

from .agent import root_agent as root_agent
