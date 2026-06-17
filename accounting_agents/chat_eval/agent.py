"""Chat-lane eval entry point for ADK AgentEvaluator and agents-cli eval.

Production Slack traffic uses ``accounting_agents.agent.assistant_app``
(``name=accounting_agents_assistant``). Eval uses a dedicated App whose
``name`` matches this directory (``chat_eval``) so agents-cli session
handling works. The underlying ``LlmAgent`` is the same ``assistant_agent``.
"""

from __future__ import annotations

from google.adk.apps import App

from accounting_agents.agent import assistant_agent
from accounting_agents.plugins.ledgr_reflect_retry import LedgrReflectRetryPlugin

# ADK AgentEvaluator convention: module exposes ``root_agent``.
root_agent = assistant_agent

# agents-cli AgentLoader convention: ``app`` with name == directory name.
app = App(
    name="chat_eval",
    root_agent=assistant_agent,
    plugins=[LedgrReflectRetryPlugin(max_retries=2)],
)

__all__ = ["app", "root_agent"]
