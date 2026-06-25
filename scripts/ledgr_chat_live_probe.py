#!/usr/bin/env python3
"""Live probe: run the Company-A B6 question and print tool trajectory + reply.

Mirrors what you asked in Slack (#skyline-international-pte-ltd) without
needing the bot. Uses the same chat eval App + session state as B6.

Usage::

    uv run python scripts/ledgr_chat_live_probe.py
    uv run python scripts/ledgr_chat_live_probe.py --question "why account code for 25-D15?"
    uv run python scripts/ledgr_chat_live_probe.py --case B8_thread_followup_coa_description --multi-turn
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys

_REPO = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

_DEFAULT_QUESTION = (
    "Tell me why you used this account code for invoice 25-D15 and 25-D12"
)
_B7_CASE = "B7_chat_thread_delivery_context_trajectory"
_B8_CASE = "B8_thread_followup_coa_description"
_B6_CASE = "B6_chat_invoice_account_code_trajectory"
_EVALSET = _REPO / "tests/eval/datasets/ledgr.evalset.json"

_BAD_PHRASES = (
    "provide the vendor",
    "provide the account code",
    "don't see any document",
    "cannot find",
    "please provide",
)


async def _probe_turn(
    runner,
    *,
    user_id: str,
    session_id: str,
    question: str,
) -> dict:
    from google.genai import types as genai_types

    user_content = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=question)],
    )
    tool_names: list[str] = []
    text_parts: list[str] = []

    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=user_content,
    ):
        if not event.content or not event.content.parts:
            continue
        for part in event.content.parts:
            if part.function_call and part.function_call.name:
                tool_names.append(part.function_call.name)
            if part.text and getattr(event, "author", None) != "user":
                text_parts.append(part.text)

    return {
        "question": question,
        "tool_trajectory": tool_names,
        "response_preview": "".join(text_parts)[:500],
        "response_len": len("".join(text_parts)),
    }


async def _probe(
    question: str,
    *,
    case_id: str = _B6_CASE,
    multi_turn: bool = False,
) -> dict | list[dict]:
    from google.adk.runners import Runner
    from google.adk.sessions.in_memory_session_service import InMemorySessionService

    from accounting_agents.chat_eval.agent import app as chat_eval_app

    raw = json.loads(_EVALSET.read_text())
    case = next(c for c in raw["eval_cases"] if c["eval_id"] == case_id)
    session_input = case["session_input"]

    session_service = InMemorySessionService()
    runner = Runner(app=chat_eval_app, session_service=session_service)
    session = await session_service.create_session(
        app_name=chat_eval_app.name,
        user_id=session_input["user_id"],
        state=session_input.get("state") or {},
    )

    if multi_turn:
        turns: list[dict] = []
        for inv in case.get("conversation") or []:
            user_parts = inv.get("user_content", {}).get("parts") or []
            q = user_parts[0].get("text") if user_parts else question
            turns.append(
                await _probe_turn(
                    runner,
                    user_id=session.user_id,
                    session_id=session.id,
                    question=q,
                )
            )
        return turns

    return await _probe_turn(
        runner,
        user_id=session.user_id,
        session_id=session.id,
        question=question,
    )


def _trajectory_gate(case_id: str, tool_names: list[str]) -> tuple[bool, str]:
    if case_id == _B7_CASE:
        expected = ["lookup_row", "explain_categorization"]
    elif case_id == _B8_CASE:
        expected = ["lookup_coa_account"]
    else:
        expected = [
            "lookup_row",
            "explain_categorization",
            "lookup_row",
            "explain_categorization",
        ]
    ei = 0
    for name in tool_names:
        if ei < len(expected) and name == expected[ei]:
            ei += 1
    ok = ei == len(expected)
    return ok, f"{ei}/{len(expected)}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", default=_DEFAULT_QUESTION)
    parser.add_argument(
        "--case",
        default=_B6_CASE,
        choices=[_B6_CASE, _B7_CASE, _B8_CASE],
        help="Eval case id to seed session state",
    )
    parser.add_argument(
        "--multi-turn",
        action="store_true",
        help="Run every turn in the eval case conversation (same session id)",
    )
    parser.add_argument(
        "--thread-ts",
        default="1700000099.000200",
        help="Simulated delivery thread parent ts (informational for B7/B8)",
    )
    args = parser.parse_args()

    result = asyncio.run(
        _probe(args.question, case_id=args.case, multi_turn=args.multi_turn)
    )

    print("CASE:", args.case)
    if args.case in (_B7_CASE, _B8_CASE):
        print("THREAD_TS (simulated):", args.thread_ts)

    ok = True
    if args.multi_turn:
        assert isinstance(result, list)
        for i, turn in enumerate(result, start=1):
            print(f"\n--- TURN {i} ---")
            print("QUESTION:", turn["question"])
            print("TOOL_TRAJECTORY:", turn["tool_trajectory"])
            print("RESPONSE (first 500 chars):")
            print(turn["response_preview"] or "(empty)")
            if i == len(result) and args.case == _B8_CASE:
                gate_ok, gate_label = _trajectory_gate(args.case, turn["tool_trajectory"])
                print(f"\nTRAJECTORY_GATE: {'PASS' if gate_ok else 'FAIL'} ({gate_label})")
                ok = ok and gate_ok
            lower = (turn["response_preview"] or "").lower()
            for phrase in _BAD_PHRASES:
                if phrase in lower:
                    print(f"REGRESSION_FLAG: response contains {phrase!r}")
                    ok = False
        return 0 if ok else 2

    assert isinstance(result, dict)
    print("QUESTION:", result["question"])
    print("TOOL_TRAJECTORY:", result["tool_trajectory"])
    print("RESPONSE (first 500 chars):")
    print(result["response_preview"])
    if not result["response_preview"]:
        print("(empty — check creds / model errors above)")
        return 1

    gate_ok, gate_label = _trajectory_gate(args.case, result["tool_trajectory"])
    ok = gate_ok
    print(f"\nTRAJECTORY_GATE: {'PASS' if gate_ok else 'FAIL'} ({gate_label})")
    lower = result["response_preview"].lower()
    for phrase in _BAD_PHRASES:
        if phrase in lower:
            print(f"REGRESSION_FLAG: response contains {phrase!r}")
            ok = False
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
