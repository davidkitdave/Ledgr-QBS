"""Live reference-free eval for ``ledgr_agent`` (requires Gemini creds)."""

from __future__ import annotations

import asyncio
import json
import os
import pathlib

import pytest
from dotenv import load_dotenv

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".env")
_EVALSET_PATH = pathlib.Path(__file__).parent / "datasets" / "ledgr_light.evalset.json"
_EVAL_CONFIG_PATH = pathlib.Path(__file__).parent / "eval_config_ledgr_light.json"

_HAS_CREDS = bool(
    os.environ.get("GOOGLE_API_KEY") or os.environ.get("GOOGLE_CLOUD_PROJECT")
)
_SKIP_REASON = (
    "Live LLM credentials not present. "
    "Set GOOGLE_API_KEY or GOOGLE_CLOUD_PROJECT to run the ledgr light eval."
)


def _case_ids() -> list[str]:
    raw = json.loads(_EVALSET_PATH.read_text(encoding="utf-8"))
    return [case["eval_id"] for case in raw["eval_cases"]]


async def _run_case(eval_case_id: str) -> None:
    from google.adk.evaluation.agent_evaluator import AgentEvaluator
    from google.adk.evaluation.eval_case import EvalCase, Invocation, SessionInput
    from google.adk.evaluation.eval_config import EvalConfig
    from google.adk.evaluation.eval_set import EvalSet
    from google.genai import types as genai_types

    raw = json.loads(_EVALSET_PATH.read_text(encoding="utf-8"))
    raw_case = next(c for c in raw["eval_cases"] if c["eval_id"] == eval_case_id)

    invocations: list[Invocation] = []
    for inv_raw in raw_case["conversation"]:
        parts: list[genai_types.Part] = []
        for part in inv_raw["user_content"]["parts"]:
            if part.get("text"):
                parts.append(genai_types.Part(text=part["text"]))
            elif part.get("inline_data"):
                inline = part["inline_data"]
                data = inline.get("data", "")
                if isinstance(data, str):
                    import base64

                    blob = base64.standard_b64decode(data)
                else:
                    blob = data
                parts.append(
                    genai_types.Part(
                        inline_data=genai_types.Blob(
                            mime_type=inline.get("mime_type", "application/pdf"),
                            data=blob,
                        )
                    )
                )
        user_content = genai_types.Content(role="user", parts=parts)
        invocations.append(
            Invocation(
                invocation_id=inv_raw.get("invocation_id", ""),
                user_content=user_content,
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
        eval_set_id=f"ledgr_light_v1__{eval_case_id}",
        eval_cases=[eval_case],
    )
    eval_config = EvalConfig.model_validate_json(
        _EVAL_CONFIG_PATH.read_text(encoding="utf-8")
    )
    from ledgr_agent.eval.register_custom_metrics import register_ledgr_light_custom_metrics

    register_ledgr_light_custom_metrics(eval_config)

    await AgentEvaluator.evaluate_eval_set(
        agent_module="ledgr_agent.agent",
        eval_set=eval_set,
        eval_config=eval_config,
        num_runs=1,
        print_detailed_results=True,
    )


@pytest.mark.eval
@pytest.mark.skipif(not _HAS_CREDS, reason=_SKIP_REASON)
@pytest.mark.parametrize("eval_case_id", _case_ids())
def test_ledgr_light_eval_case(eval_case_id: str) -> None:
    """Run one synthetic financial-doc case through the real ledgr_agent (reference-free)."""
    asyncio.run(_run_case(eval_case_id))
