"""Generate agents-cli-compatible traces from ADK golden evalsets.

Chat cases (cluster B) require seeded session state (ledger rows, processing
log, FY). The stock ``agents-cli eval generate`` path runs Vertex inference
without ``session_input``, so this script uses ADK ``LocalEvalService`` with
the correct agent module per case, then writes traces that ``agents-cli eval
grade`` can score.

Usage::

    uv run python eval/ledgr_eval_generate.py --lane chat
    agents-cli eval grade --traces artifacts/traces/chat_traces.json \\
        --config tests/eval/eval_config_chat.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from typing import Any

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_DEFAULT_EVALSET = _REPO_ROOT / "tests/eval/datasets/ledgr.evalset.json"
_DEFAULT_OUTPUT_DIR = _REPO_ROOT / "artifacts/traces"


def _text_parts(content: dict[str, Any] | None) -> list[dict[str, str]]:
    if not content:
        return []
    return [{"text": p["text"]} for p in content.get("parts", []) if p.get("text")]


def _invocation_to_agent_data(
    invocation: dict[str, Any],
    *,
    agent_name: str,
) -> dict[str, Any]:
    """Convert one ADK-style invocation dict to agents-cli ``agent_data`` turns."""
    events: list[dict[str, Any]] = []

    user_content = invocation.get("user_content") or {}
    events.append(
        {
            "author": "user",
            "content": {
                "role": user_content.get("role", "user"),
                "parts": user_content.get("parts") or [],
            },
        }
    )

    intermediate = invocation.get("intermediate_data") or {}
    for tool_use in intermediate.get("tool_uses") or []:
        name = tool_use.get("name", "")
        args = tool_use.get("args") or {}
        events.append(
            {
                "author": agent_name,
                "content": {
                    "parts": [{"function_call": {"name": name, "args": args}}],
                },
            }
        )
        events.append(
            {
                "author": agent_name,
                "content": {
                    "parts": [
                        {
                            "function_response": {
                                "name": name,
                                "response": {"status": "ok"},
                            }
                        }
                    ],
                },
            }
        )

    final_response = invocation.get("final_response") or {}
    text_parts = _text_parts(final_response)
    if text_parts:
        events.append(
            {
                "author": agent_name,
                "content": {
                    "role": final_response.get("role", "model"),
                    "parts": text_parts,
                },
            }
        )

    return {
        "agents": {
            agent_name: {
                "agent_id": agent_name,
                "instruction": "Ledgr chat assistant (eval trace export).",
            }
        },
        "turns": [{"turn_index": 0, "events": events}],
    }


def _expected_tool_names(conversation: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for inv in conversation:
        intermediate = inv.get("intermediate_data") or {}
        for tool_use in intermediate.get("tool_uses") or []:
            name = tool_use.get("name")
            if name:
                names.append(name)
    return names


def _actual_tool_names_from_invocations(invocations: list[Any]) -> list[str]:
    names: list[str] = []
    for inv in invocations:
        intermediate = getattr(inv, "intermediate_data", None)
        if not intermediate:
            continue
        for tool_use in getattr(intermediate, "tool_uses", None) or []:
            name = getattr(tool_use, "name", None)
            if name:
                names.append(name)
    return names


def _build_agents_cli_case(
    *,
    eval_case_id: str,
    conversation: list[dict[str, Any]],
    actual_invocations: list[Any],
    agent_name: str,
) -> dict[str, Any]:
    """Merge expected + actual into one agents-cli EvalCase dict."""
    turns: list[dict[str, Any]] = []
    agents_map: dict[str, Any] = {
        agent_name: {
            "agent_id": agent_name,
            "instruction": "Ledgr chat assistant.",
        }
    }

    for idx, expected_inv in enumerate(conversation):
        is_last = idx == len(conversation) - 1
        if is_last and actual_invocations:
            actual = actual_invocations[min(idx, len(actual_invocations) - 1)]
            user_content = actual.user_content
            user_parts = [
                {"text": p.text}
                for p in (user_content.parts or [])
                if getattr(p, "text", None)
            ]
            events: list[dict[str, Any]] = [
                {
                    "author": "user",
                    "content": {"role": "user", "parts": user_parts},
                }
            ]
            intermediate = getattr(actual, "intermediate_data", None)
            for tool_use in getattr(intermediate, "tool_uses", None) or []:
                name = tool_use.name
                args = dict(tool_use.args or {})
                events.append(
                    {
                        "author": agent_name,
                        "content": {
                            "parts": [{"function_call": {"name": name, "args": args}}],
                        },
                    }
                )
                events.append(
                    {
                        "author": agent_name,
                        "content": {
                            "parts": [
                                {
                                    "function_response": {
                                        "name": name,
                                        "response": {"status": "ok"},
                                    }
                                }
                            ],
                        },
                    }
                )
            final = getattr(actual, "final_response", None)
            if final and final.parts:
                texts = [
                    {"text": p.text} for p in final.parts if getattr(p, "text", None)
                ]
                if texts:
                    events.append(
                        {
                            "author": agent_name,
                            "content": {"role": "model", "parts": texts},
                        }
                    )
            turns.append({"turn_index": idx, "events": events})
        else:
            agent_data = _invocation_to_agent_data(expected_inv, agent_name=agent_name)
            for turn in agent_data["turns"]:
                turn["turn_index"] = idx
                turns.append(turn)
            agents_map.update(agent_data.get("agents") or {})

    expected_tools = _expected_tool_names(conversation)
    actual_tools = _actual_tool_names_from_invocations(actual_invocations)

    last_prompt_parts = (conversation[-1].get("user_content") or {}).get("parts") or []
    case: dict[str, Any] = {
        "eval_case_id": eval_case_id,
        "prompt": {"role": "user", "parts": last_prompt_parts},
        "expected_tool_trajectory": expected_tools,
        "actual_tool_trajectory": actual_tools,
        "agent_data": {"agents": agents_map, "turns": turns},
    }

    response_text = ""
    if actual_invocations:
        actual = actual_invocations[-1]
        final = getattr(actual, "final_response", None)
        if final and final.parts:
            response_text = "".join(
                p.text for p in final.parts if getattr(p, "text", None)
            )
    if response_text:
        case["responses"] = [
            {"response": {"role": "model", "parts": [{"text": response_text}]}}
        ]

    final_expected = conversation[-1].get("final_response") or {}
    expected_text = "".join(
        p.get("text", "") for p in (final_expected.get("parts") or []) if p.get("text")
    )
    if expected_text:
        case["reference"] = {
            "response": {"role": "model", "parts": [{"text": expected_text}]}
        }

    return case


async def _run_adk_inference(
    eval_case_id: str,
    eval_set_path: pathlib.Path,
) -> tuple[str, list[Any]]:
    """Run ADK Runner inference for one golden case (chat lane)."""
    from google.adk.evaluation.eval_case import IntermediateData, Invocation
    from google.adk.runners import Runner
    from google.adk.sessions.in_memory_session_service import InMemorySessionService
    from google.genai import types as genai_types

    from accounting_agents.chat_eval.agent import app as chat_eval_app
    from tests.eval.eval_routing import is_chat_case

    if not is_chat_case(eval_case_id):
        raise ValueError(
            f"Case {eval_case_id!r} is not a chat case. "
            "Use pytest tests/eval/ -m eval for doc-lane cases."
        )

    raw = json.loads(eval_set_path.read_text())
    raw_case = next(c for c in raw["eval_cases"] if c["eval_id"] == eval_case_id)
    session_input = raw_case.get("session_input") or {}
    conversation = raw_case.get("conversation") or []

    session_service = InMemorySessionService()
    runner = Runner(app=chat_eval_app, session_service=session_service)
    session = await session_service.create_session(
        app_name=chat_eval_app.name,
        user_id=session_input.get("user_id", "eval_user"),
        state=session_input.get("state") or {},
    )

    invocations: list[Invocation] = []
    for inv_raw in conversation:
        parts = (inv_raw.get("user_content") or {}).get("parts") or []
        text = parts[0].get("text", "") if parts else ""
        user_content = genai_types.Content(
            role="user",
            parts=[genai_types.Part(text=text)],
        )

        tool_uses: list[genai_types.FunctionCall] = []
        final_text_parts: list[str] = []

        async for event in runner.run_async(
            user_id=session.user_id,
            session_id=session.id,
            new_message=user_content,
        ):
            if not event.content or not event.content.parts:
                continue
            for part in event.content.parts:
                if part.function_call and part.function_call.name:
                    tool_uses.append(part.function_call)
                if part.text and getattr(event, "author", None) != "user":
                    final_text_parts.append(part.text)

        final_response = None
        if final_text_parts:
            final_response = genai_types.Content(
                role="model",
                parts=[genai_types.Part(text="".join(final_text_parts))],
            )

        intermediate_data = (
            IntermediateData(tool_uses=tool_uses, intermediate_responses=[])
            if tool_uses
            else None
        )
        invocations.append(
            Invocation(
                invocation_id=inv_raw.get("invocation_id", ""),
                user_content=user_content,
                final_response=final_response,
                intermediate_data=intermediate_data,
            )
        )

    agent_name = getattr(chat_eval_app.root_agent, "name", None) or "assistant"
    return agent_name, invocations


async def generate_traces(
    *,
    evalset_path: pathlib.Path,
    case_ids: list[str],
    output_path: pathlib.Path,
) -> dict[str, Any]:
    """Run inference for *case_ids* and write agents-cli trace JSON."""
    raw = json.loads(evalset_path.read_text())
    cases_by_id = {c["eval_id"]: c for c in raw["eval_cases"]}

    exported: list[dict[str, Any]] = []
    for case_id in case_ids:
        if case_id not in cases_by_id:
            raise ValueError(f"Case {case_id!r} not in {evalset_path}")
        print(f"[ledgr_eval_generate] inference {case_id}", flush=True)
        agent_name, actual_invs = await _run_adk_inference(case_id, evalset_path)
        exported.append(
            _build_agents_cli_case(
                eval_case_id=case_id,
                conversation=cases_by_id[case_id]["conversation"],
                actual_invocations=actual_invs,
                agent_name=agent_name,
            )
        )

    payload = {"eval_cases": exported}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[ledgr_eval_generate] wrote {output_path} ({len(exported)} cases)", flush=True)
    return payload


def _select_case_ids(
    *,
    lane: str,
    evalset_path: pathlib.Path,
    case_id: str | None,
) -> list[str]:
    from tests.eval.eval_routing import is_chat_case

    raw = json.loads(evalset_path.read_text())
    all_ids = [c["eval_id"] for c in raw["eval_cases"]]

    if case_id:
        return [case_id]

    if lane == "chat":
        return [cid for cid in all_ids if is_chat_case(cid)]
    if lane == "doc":
        raise ValueError(
            "Doc-lane generate is not supported here (no session/PDF wiring). "
            "Use: pytest tests/eval/ -m eval"
        )
    if lane == "all":
        return [cid for cid in all_ids if is_chat_case(cid)]
    raise ValueError(f"Unknown lane {lane!r}; use chat, doc, or all")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evalset",
        type=pathlib.Path,
        default=_DEFAULT_EVALSET,
        help="ADK EvalSet JSON (default: tests/eval/datasets/ledgr.evalset.json)",
    )
    parser.add_argument(
        "--lane",
        choices=("chat", "doc", "all"),
        default="chat",
        help="Which eval cluster to run (default: chat)",
    )
    parser.add_argument(
        "--case-id",
        default=None,
        help="Run a single eval_id instead of the whole lane",
    )
    parser.add_argument(
        "--output",
        type=pathlib.Path,
        default=None,
        help="Output trace JSON path (default: artifacts/traces/<lane>_traces.json)",
    )
    args = parser.parse_args(argv)

    output = args.output or (_DEFAULT_OUTPUT_DIR / f"{args.lane}_traces.json")
    case_ids = _select_case_ids(
        lane=args.lane,
        evalset_path=args.evalset,
        case_id=args.case_id,
    )

    asyncio.run(
        generate_traces(
            evalset_path=args.evalset,
            case_ids=case_ids,
            output_path=output,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
