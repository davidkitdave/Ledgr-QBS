"""Sequential bill pipeline: read_document → project_to_erp."""

from __future__ import annotations

from google.adk.agents import Agent, SequentialAgent

from ledgr_agent.shared.model_config import lite_model
from ledgr_agent.tools.project_to_erp import project_to_erp
from ledgr_agent.tools.read_document_tool import read_document


def build_bill_pipeline_agent() -> SequentialAgent:
    """Return a fixed-order pipeline for one commercial bill → ERP rows."""
    read_node = Agent(
        name="bill_read_node",
        model=lite_model(),
        description="Read one commercial bill into structured JSON.",
        instruction=(
            "You are step 1 of the bill pipeline. Call read_document exactly once. "
            "Use paths=[] when a file is attached; otherwise pass the exact paths "
            "from the user message. Do not call any other tool."
        ),
        tools=[read_document],
        output_key="bill_read_step",
    )
    project_node = Agent(
        name="bill_project_node",
        model=lite_model(),
        description="Project the read bill into ERP import rows.",
        instruction=(
            "You are step 2 of the bill pipeline. Call project_to_erp exactly once "
            "with document={} (empty dict is fine — the tool reads session state) and "
            "systems ['qbs', 'xero', 'autocount', 'sql_account']. "
            "Summarize vendor, invoice number, and QBS row counts."
        ),
        tools=[project_to_erp],
        output_key="bill_project_step",
    )
    return SequentialAgent(
        name="bill_pipeline",
        description="Light path: read one bill then project to ERP rows in fixed order.",
        sub_agents=[read_node, project_node],
    )
