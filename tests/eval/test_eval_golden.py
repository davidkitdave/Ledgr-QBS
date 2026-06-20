"""Golden eval gate for Ledgr QBS — Step 1.5b (master plan §10.B).

Runs the ADK AgentEvaluator against ``tests/eval/datasets/ledgr.evalset.json``
using the Pydantic-backed EvalSet / EvalCase schema confirmed at:

  .venv/lib/python3.12/site-packages/google/adk/evaluation/
    eval_case.py  — EvalCase, Invocation, IntermediateData, SessionInput
    eval_set.py   — EvalSet
    agent_evaluator.py:196 — AgentEvaluator.evaluate() signature

ADK doc reference: https://adk.dev/evaluate/index.md (fetched 2026-06-15)
Criteria reference: https://adk.dev/evaluate/criteria/index.md

Gated by ``@pytest.mark.eval`` — NOT collected in the default fast suite
(``pyproject.toml`` already has ``addopts = "--ignore=tests/eval"``). Run
deliberately with:

    pytest tests/eval/ -m eval -v

or the minimal form:

    pytest tests/eval/test_eval_golden.py -m eval

This module never fires live LLM calls unless you export valid credentials.
Without ``GOOGLE_API_KEY`` / ``GOOGLE_CLOUD_PROJECT`` the test is skipped so
CI stays green.

Cluster map
-----------
A  Happy-path doc lane    A1 (invoice), A2 (bank statement)
B  Chat trajectory        B3 (show_client_profile), B4 (summarize_by_category),
                          B5 (multi-turn FYE→currency)
C  SG GST regression      C6 (non-registered → NT), C7 (registered → SR),
                          C8 (zero-rated line → ZR)
D  HITL Edit round-trip   D9 (tax_treatment/net_amount field names)
E  Adversarial            E10 (unreadable doc flagged, not silently written)
F  Direction / DocKind    F1-F12 (ADR-0015 eval gate — no hardcoded rules,
                          direction + doc_kind + tax_visible correctness
                          for expense claims, telco splits, non-GST clients,
                          overseas suppliers, no-tax sales, credit notes,
                          ambiguous → unknown)
"""

from __future__ import annotations

import asyncio
import os
import pathlib

import pytest

# ──────────────────────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────────────────────

_REPO_ROOT = pathlib.Path(__file__).parent.parent.parent
_EVALSET_PATH = str(
    pathlib.Path(__file__).parent / "datasets" / "ledgr.evalset.json"
)
_EVAL_CONFIG_PATH = str(pathlib.Path(__file__).parent / "eval_config.yaml")

# ──────────────────────────────────────────────────────────────────────────────
# Skip guard — no creds, no live LLM calls
# ──────────────────────────────────────────────────────────────────────────────

_HAS_CREDS = bool(
    os.environ.get("GOOGLE_API_KEY") or os.environ.get("GOOGLE_CLOUD_PROJECT")
)
_SKIP_REASON = (
    "Live LLM credentials not present. "
    "Set GOOGLE_API_KEY or GOOGLE_CLOUD_PROJECT to run the golden eval."
)


# ──────────────────────────────────────────────────────────────────────────────
# Eval-case IDs (must match eval_id values in ledgr.evalset.json)
# ──────────────────────────────────────────────────────────────────────────────

_CASE_IDS = [
    # Cluster A — happy-path doc lane
    "A1_happy_path_invoice_classify_extract",
    "A2_happy_path_bank_statement_classify_extract",
    # Cluster B — chat trajectory
    "B3_chat_show_client_profile_trajectory",
    "B4_chat_summarize_by_category_trajectory",
    "B5_chat_multi_turn_fye_then_currency",
    "B6_chat_invoice_account_code_trajectory",
    # Cluster C — SG GST rule regression
    "C6_gst_non_registered_invoice_all_nt",
    "C7_gst_registered_invoice_standard_rated",
    "C8_gst_registered_zero_rated_line",
    # Cluster D — HITL Edit round-trip
    "D9_hitl_edit_round_trip_field_names",
    # Cluster E — adversarial / optional
    "E10_adversarial_unreadable_pdf_flagged",
    # Cluster F — direction / doc_kind (ADR-0015)
    "F1_expense_claim_no_tax_purchase",
    "F2_expense_claim_overseas_purchase",
    "F3_invoice_sales_local_gst_registered",
    "F4_invoice_purchase_local_gst_registered",
    "F5_invoice_purchase_telco_split",
    "F6_invoice_purchase_overseas_no_gst",
    "F7_invoice_purchase_no_tax_gst_registered_client",
    "F8_invoice_sales_no_tax_gst_registered_client",
    "F9_non_gst_client_purchase_with_supplier_gst",
    "F10_non_gst_client_sales_with_gst_shown",
    "F11_credit_note_sales",
    "F12_ambiguous_unknown",
]


# ──────────────────────────────────────────────────────────────────────────────
# Parametrised test — one test node per eval case
# ──────────────────────────────────────────────────────────────────────────────


async def _run_golden_eval_case(eval_case_id: str) -> None:
    """Run AgentEvaluator.evaluate_eval_set for one golden case."""
    from google.adk.evaluation.agent_evaluator import AgentEvaluator
    import json

    from google.adk.evaluation.eval_case import (
        EvalCase,
        IntermediateData,
        Invocation,
        SessionInput,
    )
    from google.adk.evaluation.eval_config import EvalConfig
    from google.adk.evaluation.eval_metrics import BaseCriterion
    from google.adk.evaluation.eval_set import EvalSet
    from google.genai import types as genai_types

    from tests.eval.eval_routing import agent_module_for_case

    raw = json.loads(pathlib.Path(_EVALSET_PATH).read_text())
    raw_case = next(
        (c for c in raw["eval_cases"] if c["eval_id"] == eval_case_id),
        None,
    )
    assert raw_case is not None, (
        f"Case {eval_case_id!r} not found in {_EVALSET_PATH}"
    )

    invocations: list[Invocation] = []
    for inv_raw in raw_case.get("conversation", []):
        uc_raw = inv_raw["user_content"]
        user_content = genai_types.Content(
            role=uc_raw.get("role", "user"),
            parts=[genai_types.Part(text=p["text"]) for p in uc_raw.get("parts", [])],
        )

        final_response = None
        if inv_raw.get("final_response"):
            fr_raw = inv_raw["final_response"]
            final_response = genai_types.Content(
                role=fr_raw.get("role", "model"),
                parts=[genai_types.Part(text=p["text"]) for p in fr_raw.get("parts", [])],
            )

        intermediate_data = None
        if inv_raw.get("intermediate_data"):
            id_raw = inv_raw["intermediate_data"]
            tool_uses = [
                genai_types.FunctionCall(name=tu["name"], args=tu.get("args", {}))
                for tu in id_raw.get("tool_uses", [])
            ]
            intermediate_data = IntermediateData(
                tool_uses=tool_uses,
                intermediate_responses=[],
            )

        invocations.append(
            Invocation(
                invocation_id=inv_raw.get("invocation_id", ""),
                user_content=user_content,
                final_response=final_response,
                intermediate_data=intermediate_data,
            )
        )

    session_input = None
    if raw_case.get("session_input"):
        si = raw_case["session_input"]
        session_input = SessionInput(
            app_name=si["app_name"],
            user_id=si["user_id"],
            state=si.get("state", {}),
        )

    eval_case = EvalCase(
        eval_id=eval_case_id,
        conversation=invocations,
        session_input=session_input,
    )

    eval_set = EvalSet(
        eval_set_id=f"ledgr_golden_gate_v1__{eval_case_id}",
        eval_cases=[eval_case],
    )

    eval_config = EvalConfig(
        criteria={
            "tool_trajectory_avg_score": BaseCriterion(threshold=1.0),
            "response_match_score": BaseCriterion(threshold=0.5),
        }
    )

    await AgentEvaluator.evaluate_eval_set(
        agent_module=agent_module_for_case(eval_case_id),
        eval_set=eval_set,
        eval_config=eval_config,
        num_runs=1,
        print_detailed_results=True,
    )


@pytest.mark.eval
@pytest.mark.skipif(not _HAS_CREDS, reason=_SKIP_REASON)
@pytest.mark.parametrize("eval_case_id", _CASE_IDS)
def test_golden_eval_case(eval_case_id: str) -> None:
    """Run AgentEvaluator.evaluate for a single case from the golden evalset.

    Each parametrised node exercises one cluster from the master-plan §10.B
    gate. Failures here block Step 2 (extract reviewer) from merging.

    AgentEvaluator.evaluate() loads the .test.json / evalset file, constructs
    an EvalSet, runs the agent, and asserts on tool_trajectory_avg_score +
    response_match_score per the config in eval_config.yaml.
    """
    asyncio.run(_run_golden_eval_case(eval_case_id))


# ──────────────────────────────────────────────────────────────────────────────
# Full-set convenience test (runs all cases in one evaluate() call)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.eval
@pytest.mark.skipif(not _HAS_CREDS, reason=_SKIP_REASON)
def test_golden_eval_full_set() -> None:
    """Run AgentEvaluator across all golden cases, routed per lane.

    Chat cases (B*) use ``accounting_agents.chat_eval.agent``; doc cases use
    ``accounting_agents.agent``. There is no single root agent for both lanes.
    """
    import json

    raw = json.loads(pathlib.Path(_EVALSET_PATH).read_text())
    for raw_case in raw["eval_cases"]:
        asyncio.run(_run_golden_eval_case(raw_case["eval_id"]))
