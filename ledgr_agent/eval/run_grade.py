#!/usr/bin/env python3
"""Grade all ledgr light eval cases and dump per-case/per-metric JSON."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import pathlib
import statistics
import sys

from dotenv import load_dotenv
from google.adk.evaluation.agent_evaluator import AgentEvaluator
from google.adk.evaluation.eval_case import EvalCase, Invocation, SessionInput
from google.adk.evaluation.eval_config import EvalConfig
from google.adk.evaluation.eval_set import EvalSet
from google.genai import types as genai_types

_REPO = pathlib.Path(__file__).resolve().parents[2]
load_dotenv(_REPO / ".env")
_EVALSET_PATH = pathlib.Path(__file__).parent / "datasets" / "ledgr_light.evalset.json"
_EVAL_CONFIG_PATH = pathlib.Path(__file__).parent / "eval_config_ledgr_light.json"


def _build_eval_set() -> EvalSet:
    raw = json.loads(_EVALSET_PATH.read_text(encoding="utf-8"))
    eval_cases: list[EvalCase] = []
    for raw_case in raw["eval_cases"]:
        invocations: list[Invocation] = []
        for inv_raw in raw_case["conversation"]:
            parts: list[genai_types.Part] = []
            for part in inv_raw["user_content"]["parts"]:
                if part.get("text"):
                    parts.append(genai_types.Part(text=part["text"]))
                elif part.get("inline_data"):
                    inline = part["inline_data"]
                    data = inline.get("data", "")
                    blob = base64.standard_b64decode(data) if isinstance(data, str) else data
                    parts.append(
                        genai_types.Part(
                            inline_data=genai_types.Blob(
                                mime_type=inline.get("mime_type", "application/pdf"),
                                data=blob,
                            )
                        )
                    )
            invocations.append(
                Invocation(
                    invocation_id=inv_raw.get("invocation_id", ""),
                    user_content=genai_types.Content(role="user", parts=parts),
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
        eval_cases.append(
            EvalCase(
                eval_id=raw_case["eval_id"],
                conversation=invocations,
                session_input=session_input,
            )
        )
    return EvalSet(eval_set_id=raw.get("eval_set_id", "ledgr_light_v1"), eval_cases=eval_cases)


def _case_metrics(eval_case_result) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for per_inv in eval_case_result.eval_metric_result_per_invocation:
        for metric_result in per_inv.eval_metric_results:
            if metric_result.score is not None:
                metrics[metric_result.metric_name] = float(metric_result.score)
    return metrics


async def grade_all(*, print_details: bool = False) -> dict:
    from google.adk.evaluation.eval_config import get_eval_metrics_from_config

    from ledgr_agent.eval.register_custom_metrics import register_ledgr_light_custom_metrics

    eval_set = _build_eval_set()
    eval_config = EvalConfig.model_validate_json(_EVAL_CONFIG_PATH.read_text(encoding="utf-8"))
    register_ledgr_light_custom_metrics(eval_config)
    agent = await AgentEvaluator._get_agent_for_eval(module_name="ledgr_agent.agent")
    eval_metrics = get_eval_metrics_from_config(eval_config)
    from google.adk.evaluation.simulation.user_simulator_provider import UserSimulatorProvider

    provider = UserSimulatorProvider(user_simulator_config=eval_config.user_simulator_config)
    results_by_id = await AgentEvaluator._get_eval_results_by_eval_id(
        agent_for_eval=agent,
        eval_set=eval_set,
        eval_metrics=eval_metrics,
        num_runs=1,
        user_simulator_provider=provider,
    )

    cases: list[dict] = []
    for eval_id, case_results in results_by_id.items():
        case_result = case_results[0]
        metrics = _case_metrics(case_result)
        case_mean = statistics.mean(metrics.values()) if metrics else 0.0
        cases.append(
            {
                "eval_case_id": eval_id,
                "metrics": metrics,
                "overall_mean": case_mean,
                "passed": case_result.final_eval_status.name,
            }
        )
        if print_details:
            metric_groups = AgentEvaluator._get_eval_metric_results_with_invocation(case_results)
            AgentEvaluator._process_metrics_and_get_failures(
                eval_metric_results=metric_groups,
                print_detailed_results=True,
                agent_module=eval_id,
            )

    overall = statistics.mean(c["overall_mean"] for c in cases) if cases else 0.0
    return {"cases": cases, "overall_mean": overall}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=pathlib.Path,
        default=_REPO / "artifacts" / "grade_results" / "ledgr_light_run.json",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    payload = asyncio.run(grade_all(print_details=args.verbose))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.output}")
    print(f"Overall mean: {payload['overall_mean']:.3f}")
    for case in payload["cases"]:
        print(f"  {case['eval_case_id']}: {case['overall_mean']:.3f} ({case['passed']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
