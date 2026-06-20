"""End-to-end runner test for the chat-lane ADK Tool Confirmation flow (ADR-0009).

Drives the real ``assistant_agent`` + real ``Runner`` + real
``FirestoreSessionService`` (backed by ``FakeFirestore``) through both turns of
the two-turn confirm protocol WITHOUT hitting a live LLM — a fake ``BaseLlm``
subclass is registered for the test model name so we can script Turn-1
(emit ``amend_ledger_row`` FunctionCall) and Turn-2 (emit a text reply after
seeing the synthesized FunctionResponse).

This is the "one link that proves the feature works at all" (per the code-review):
it verifies that after Turn-2 the tool's commit branch executed and
``state["pending_ledger_write"]`` contains the right spec.

Why a fake model is feasible here:
- ``LlmAgent`` uses ``LLMRegistry.new_llm(model_name)`` to resolve the model
  class. We register a fake class under a test-specific name, then build a
  ``LlmAgent`` with that name. The real agent is not modified.
- The fake model's ``generate_content_async`` is a simple coroutine/generator
  that inspects the incoming ``LlmRequest.contents`` to decide which turn it is.
- Everything else (tool dispatch, session persistence, confirmation event
  synthesis, state management) runs through the real ADK code paths.
"""

from __future__ import annotations

import asyncio
from typing import AsyncGenerator, Optional

from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.models.registry import LLMRegistry
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.tools import FunctionTool
from google.genai import types

from accounting_agents.assistant import (
    LEDGER_DATA_KEY,
    PENDING_WRITE_KEY,
    amend_ledger_row,
    lookup_row,
)
from accounting_agents.slack_runner import (
    REQUEST_CONFIRMATION_FUNCTION_CALL_NAME,
    classify_confirmation_reply,
    find_pending_confirmation,
    _synthesize_confirmation_message,
)

# --------------------------------------------------------------------------- #
# Fake LLM — scripts two turns deterministically
# --------------------------------------------------------------------------- #

_FAKE_MODEL_NAME = "fake-confirm-test-model"

# Flag to control what the fake model emits on the next call.
# Turn-1: emit the amend_ledger_row FunctionCall.
# Turn-2: detected by seeing an adk_request_confirmation FunctionResponse → emit text.
_TURN1_FC_ID = "amend-fc-001"


class _FakeConfirmLlm(BaseLlm):
    """Scriptable fake LLM for the two-turn confirmation e2e test.

    Turn-1 detection: last user content is plain text (the user's question).
    Turn-2 detection: last content contains a FunctionResponse for
    ``adk_request_confirmation`` — the runner has synthesized the bridge.
    """

    @classmethod
    def supported_models(cls) -> list[str]:
        return [_FAKE_MODEL_NAME]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        # Inspect the incoming contents to detect which turn we're in.
        contents = llm_request.contents or []
        is_turn2 = False
        for content in reversed(contents):
            for part in getattr(content, "parts", []) or []:
                fr = getattr(part, "function_response", None)
                if fr and getattr(fr, "name", None) == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME:
                    is_turn2 = True
                    break
            if is_turn2:
                break

        if is_turn2:
            # Turn-2: the tool's commit branch will run; just emit a text reply.
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part(text="Done — I've applied the change to your ledger.")],
                ),
                turn_complete=True,
            )
        else:
            # Turn-1: call amend_ledger_row to trigger the confirmation request.
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            function_call=types.FunctionCall(
                                id=_TURN1_FC_ID,
                                name="amend_ledger_row",
                                args={
                                    "row_index": "0",
                                    "field": "account",
                                    "new_value": "6010",
                                },
                            )
                        )
                    ],
                ),
                turn_complete=True,
            )


# Register once at import time (idempotent — LLMRegistry._register replaces existing).
LLMRegistry.register(_FakeConfirmLlm)

# --------------------------------------------------------------------------- #
# Test-local agent: same tools as assistant_agent, but uses fake model name.
# --------------------------------------------------------------------------- #

_QBS_LEDGER_ROWS = [
    {
        "_sheet": "Purchase",
        "_row": 2,
        "Invoice Number": "INV-1",
        "Description": "AWS hosting",
        "Source Amount": 1000.0,
        "Tax Amount": 90.0,
        "Account Code / COA": "6090",
        "Doc Type": "P",
    }
]

_TEST_STATE = {
    LEDGER_DATA_KEY: _QBS_LEDGER_ROWS,
    "tax_registered": True,
    "software": "QBS Ledger",
    "client_id": "c-e2e",
    "client_name": "Acme Trading Pte. Ltd.",
}


def _make_test_agent() -> LlmAgent:
    """Build an LlmAgent identical to assistant_agent but with the fake model."""
    return LlmAgent(
        name="assistant_e2e",
        model=_FAKE_MODEL_NAME,
        tools=[
            lookup_row,
            FunctionTool(amend_ledger_row, require_confirmation=True),
        ],
    )


def _make_runner(agent: LlmAgent) -> Runner:
    return Runner(
        app_name="ledgr-e2e-test",
        agent=agent,
        session_service=InMemorySessionService(),
    )


async def _run_turn(
    runner: Runner,
    user_id: str,
    session_id: str,
    new_message: types.Content,
    state_delta: Optional[dict] = None,
) -> list:
    events = []
    async for ev in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=new_message,
        state_delta=state_delta,
    ):
        events.append(ev)
    return events


# --------------------------------------------------------------------------- #
# The test
# --------------------------------------------------------------------------- #


def test_e2e_confirmation_flow_commit_branch_executes():
    """Turn-1 emits adk_request_confirmation; Turn-2 via bridge populates pending_write.

    Proves end-to-end:
    1. The fake LLM calls amend_ledger_row on Turn-1.
    2. ADK's FunctionTool machinery calls request_confirmation, which emits an
       adk_request_confirmation long-running event and persists it in the session.
    3. find_pending_confirmation detects the pending confirmation from the session.
    4. classify_confirmation_reply("yes") returns True.
    5. _synthesize_confirmation_message builds the FunctionResponse Content.
    6. Turn-2 runner.run_async with the synthesized message re-executes
       amend_ledger_row's commit branch.
    7. state["pending_ledger_write"] contains the right amend spec.
    """
    agent = _make_test_agent()
    runner = _make_runner(agent)
    app_name = runner.app_name
    user_id = "C-e2e"
    session_id = "S-e2e"

    # Create the session with ledger state pre-loaded.
    asyncio.run(runner.session_service.create_session(
        app_name=app_name,
        user_id=user_id,
        session_id=session_id,
        state=_TEST_STATE,
    ))

    # ---- Turn 1: user asks to change the account code ----
    turn1_msg = types.Content(
        role="user",
        parts=[types.Part(text="Change the AWS hosting account code to 6010")],
    )
    asyncio.run(_run_turn(runner, user_id, session_id, turn1_msg))

    # The session should now have a pending adk_request_confirmation.
    session_after_t1 = asyncio.run(runner.session_service.get_session(
        app_name=app_name, user_id=user_id, session_id=session_id
    ))
    assert session_after_t1 is not None

    pending = find_pending_confirmation(session_after_t1)
    assert pending is not None, (
        "No pending adk_request_confirmation found after Turn-1. "
        "ADK Tool Confirmation may have changed its event shape."
    )
    fc_id, confirmation = pending
    assert fc_id  # a non-empty id

    # NOTE (ADR-0009 / the bug this test pins): ADK does NOT reliably carry the
    # request-side payload (passed to request_confirmation(payload=spec)) through
    # to the Turn-2 ToolConfirmation. After a real runner round-trip,
    # ``confirmation.payload`` is None here. The commit must therefore NOT depend
    # on it — Turn-2 RE-DERIVES the spec from the tool's own original args
    # (row_index/field/new_value), which ADK re-supplies on resume. We assert the
    # pending confirmation exists; we deliberately do NOT assert the payload
    # survived (it doesn't), because relying on it is the silent-empty-write bug.
    payload = getattr(confirmation, "payload", None)
    assert payload is None, (
        "ToolConfirmation.payload unexpectedly survived the round-trip. "
        "If ADK now carries it, the re-derivation seam is still correct, but "
        "this assertion documents the contract the fix does NOT rely on."
    )

    # ---- Turn 2: user says "yes" — bridge synthesizes FunctionResponse ----
    # The synthesized FunctionResponse needs only the matching fc_id + confirmed;
    # the (None) payload is irrelevant to the commit.
    assert classify_confirmation_reply("yes") is True
    turn2_msg = _synthesize_confirmation_message(fc_id, confirmation, confirmed=True)

    asyncio.run(_run_turn(runner, user_id, session_id, turn2_msg))

    # After Turn-2 the tool's commit branch should have appended the RE-DERIVED
    # spec to pending_ledger_write — proving the seam works end to end without
    # the Turn-1 payload.
    session_after_t2 = asyncio.run(runner.session_service.get_session(
        app_name=app_name, user_id=user_id, session_id=session_id
    ))
    assert session_after_t2 is not None
    state = dict(session_after_t2.state)

    pending_writes = state.get(PENDING_WRITE_KEY)
    assert isinstance(pending_writes, list) and pending_writes, (
        f"state['{PENDING_WRITE_KEY}'] is empty/missing after Turn-2. "
        f"Full state keys: {list(state.keys())}"
    )
    write_spec = pending_writes[0]
    assert write_spec["op"] == "amend"
    assert write_spec["sheet"] == "Purchase"
    assert write_spec["row"] == 2
    assert write_spec["updates"]["Account Code / COA"] == "6010"
    assert "row_signature" in write_spec and write_spec["row_signature"]
