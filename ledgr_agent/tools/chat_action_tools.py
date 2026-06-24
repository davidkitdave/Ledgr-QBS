from google.adk.tools import FunctionTool

from accounting_agents.assistant.tools.explain_tools import explain_tax_treatment
from accounting_agents.assistant.tools.mutate_tools import amend_ledger_row

# Raw callable — kept directly callable for unit tests.
explain_tax_treatment_action = explain_tax_treatment
# Agent-facing tool. FunctionTool lets ADK strip the injected tool_context param
# so the vertexai eval-inference path (AgentConfig.from_agent) can parse it.
explain_tax_treatment_tool = FunctionTool(explain_tax_treatment)
amend_ledger_row_action = FunctionTool(amend_ledger_row, require_confirmation=True)
