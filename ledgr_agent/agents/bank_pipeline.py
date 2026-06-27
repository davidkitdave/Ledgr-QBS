"""Sequential bank pipeline: read_bank_statement → project_bank_workbook."""

from __future__ import annotations

from google.adk.agents import Agent, SequentialAgent

from ledgr_agent.shared.model_config import lite_model
from ledgr_agent.tools.project_bank_workbook import project_bank_workbook
from ledgr_agent.tools.read_bank_statement_tool import read_bank_statement


def build_bank_pipeline_agent() -> SequentialAgent:
    """Return a fixed-order pipeline for one bank statement → workbook sheets."""
    read_node = Agent(
        name="bank_read_node",
        model=lite_model(),
        description="Read one bank statement into structured JSON.",
        instruction=(
            "You are step 1 of the bank pipeline. Call read_bank_statement exactly once. "
            "Use paths=[] when a file is attached; otherwise pass the exact paths "
            "from the user message. Do not call read_document or process_document_batch."
        ),
        tools=[read_bank_statement],
        output_key="bank_read_step",
    )
    project_node = Agent(
        name="bank_project_node",
        model=lite_model(),
        description="Project the read bank statement into workbook sheets.",
        instruction=(
            "You are step 2 of the bank pipeline. Call project_bank_workbook exactly once "
            "with statement={} (empty dict is fine — the tool reads session state). "
            "Summarize sheet titles, transaction row counts, and reconciled status."
        ),
        tools=[project_bank_workbook],
        output_key="bank_project_step",
    )
    return SequentialAgent(
        name="bank_pipeline",
        description="Light path: read one bank statement then build workbook sheets.",
        sub_agents=[read_node, project_node],
    )
