from google.adk.tools import FunctionTool

from accounting_agents.assistant.tools.explain_tools import explain_tax_treatment
from accounting_agents.assistant.tools.mutate_tools import amend_ledger_row

explain_tax_treatment_action = explain_tax_treatment
amend_ledger_row_action = FunctionTool(amend_ledger_row, require_confirmation=True)