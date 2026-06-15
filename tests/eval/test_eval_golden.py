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
_AGENT_MODULE = str(_REPO_ROOT / "accounting_agents")
_EVALSET_PATH = str(
    pathlib.Path(__file__).parent / "datasets" / "ledgr.evalset.json"
)

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
    # Cluster C — SG GST rule regression
    "C6_gst_non_registered_invoice_all_nt",
    "C7_gst_registered_invoice_standard_rated",
    "C8_gst_registered_zero_rated_line",
    # Cluster D — HITL Edit round-trip
    "D9_hitl_edit_round_trip_field_names",
    # Cluster E — adversarial / optional
    "E10_adversarial_unreadable_pdf_flagged",
]


# ──────────────────────────────────────────────────────────────────────────────
# Parametrised test — one test node per eval case
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.eval
@pytest.mark.skipif(not _HAS_CREDS, reason=_SKIP_REASON)
@pytest.mark.asyncio
@pytest.mark.parametrize("eval_case_id", _CASE_IDS)
async def test_golden_eval_case(eval_case_id: str) -> None:
    """Run AgentEvaluator.evaluate for a single case from the golden evalset.

    Each parametrised node exercises one cluster from the master-plan §10.B
    gate. Failures here block Step 2 (extract reviewer) from merging.

    AgentEvaluator.evaluate() loads the .test.json / evalset file, constructs
    an EvalSet, runs the agent, and asserts on tool_trajectory_avg_score +
    response_match_score per the config in eval_config.yaml.
    """
    # Import here so missing ADK deps don't break collection in the default suite.
    from google.adk.evaluation.agent_evaluator import AgentEvaluator

    # evaluate() accepts a full evalset file path; it runs ALL cases in the
    # file. For per-case isolation we build a minimal single-case evalset in
    # memory and delegate to evaluate_eval_set, which accepts an EvalSet obj.
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

    # Load the full evalset JSON and pull out just this case.
    raw = json.loads(pathlib.Path(_EVALSET_PATH).read_text())
    raw_case = next(
        (c for c in raw["eval_cases"] if c["eval_id"] == eval_case_id),
        None,
    )
    assert raw_case is not None, (
        f"Case {eval_case_id!r} not found in {_EVALSET_PATH}"
    )

    # Build typed Invocation objects.
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

    # Build SessionInput if present.
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

    # Criteria: tool_trajectory_avg_score=1.0 (anti-random-walk gate per
    # master plan §10.B) + response_match_score=0.5 (loose; final_response
    # values are short anchor strings, not full prose).
    eval_config = EvalConfig(
        criteria={
            "tool_trajectory_avg_score": BaseCriterion(threshold=1.0),
            "response_match_score": BaseCriterion(threshold=0.5),
        }
    )

    await AgentEvaluator.evaluate_eval_set(
        agent_module=_AGENT_MODULE,
        eval_set=eval_set,
        eval_config=eval_config,
        num_runs=1,
        print_detailed_results=True,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Full-set convenience test (runs all cases in one evaluate() call)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.eval
@pytest.mark.skipif(not _HAS_CREDS, reason=_SKIP_REASON)
@pytest.mark.asyncio
async def test_golden_eval_full_set() -> None:
    """Run AgentEvaluator.evaluate() across the entire ledgr evalset file.

    Use this for a full regression sweep. The per-case parametrised test above
    is better for pinpointing failures during development.
    """
    from google.adk.evaluation.agent_evaluator import AgentEvaluator

    await AgentEvaluator.evaluate(
        agent_module=_AGENT_MODULE,
        eval_dataset_file_path_or_dir=_EVALSET_PATH,
        num_runs=1,
        print_detailed_results=True,
    )
