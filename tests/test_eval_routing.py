"""Unit tests for eval lane routing (no live LLM)."""

from __future__ import annotations

import importlib

import pytest

from tests.eval.eval_routing import (
    CHAT_AGENT_MODULE,
    CHAT_CASE_IDS,
    DOC_AGENT_MODULE,
    agent_module_for_case,
    chat_agent_directory,
    is_chat_case,
)


@pytest.mark.parametrize(
    ("case_id", "expected_module"),
    [
        ("B3_chat_show_client_profile_trajectory", CHAT_AGENT_MODULE),
        ("B6_chat_invoice_account_code_trajectory", CHAT_AGENT_MODULE),
        ("A1_happy_path_invoice_classify_extract", DOC_AGENT_MODULE),
        ("C6_gst_non_registered_invoice_all_nt", DOC_AGENT_MODULE),
    ],
)
def test_agent_module_routing(case_id: str, expected_module: str) -> None:
    assert agent_module_for_case(case_id) == expected_module


def test_chat_case_ids_match_b_prefix() -> None:
    for case_id in CHAT_CASE_IDS:
        assert is_chat_case(case_id)


def test_chat_eval_module_exposes_root_agent() -> None:
    mod = importlib.import_module(CHAT_AGENT_MODULE)
    assert hasattr(mod, "root_agent")
    assert mod.root_agent.name == "assistant"
    assert mod.app.name == "chat_eval"


def test_doc_eval_module_exposes_root_agent() -> None:
    mod = importlib.import_module(DOC_AGENT_MODULE)
    assert hasattr(mod, "root_agent")
    assert mod.root_agent.name == "coordinator_graph"


def test_chat_agent_directory() -> None:
    assert chat_agent_directory() == "accounting_agents/chat_eval"
