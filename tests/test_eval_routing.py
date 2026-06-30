"""Eval routing — single-lane ``ledgr_agent`` only."""

from __future__ import annotations

from tests.eval.eval_routing import (
    DOC_AGENT_MODULE,
    agent_directory,
    agent_module_for_case,
)


def test_doc_agent_module() -> None:
    assert agent_module_for_case("sg_gst_invoice_single") == DOC_AGENT_MODULE


def test_agent_directory_points_at_ledgr_agent() -> None:
    assert agent_directory() == "ledgr_agent"
