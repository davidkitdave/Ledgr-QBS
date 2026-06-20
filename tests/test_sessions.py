"""Hermetic tests for the Firestore-backed ADK session service.

No network: a dict-backed FakeFirestore stands in for the real client. These
prove the service round-trips ``state`` + ``events`` (the two things ADK resume
needs) and that a freshly-built service instance reads back exactly what an
earlier instance wrote (simulating a process restart).
"""

from __future__ import annotations

import asyncio

from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.genai import types

from accounting_agents.sessions import FirestoreSessionService
from tests._fake_firestore import FakeFirestore


def _run(coro):
    return asyncio.run(coro)


def _make_event(text: str, state_delta: dict | None = None) -> Event:
    return Event(
        author="user",
        content=types.Content(parts=[types.Part(text=text)]),
        actions=EventActions(state_delta=state_delta or {}),
    )


def test_create_and_get_session_round_trips_state():
    db = FakeFirestore()
    svc = FirestoreSessionService(client=db)

    _run(
        svc.create_session(
            app_name="acc",
            user_id="C123",
            session_id="C123",
            state={"channel_id": "C123", "fye_month": 3},
        )
    )

    got = _run(svc.get_session(app_name="acc", user_id="C123", session_id="C123"))
    assert got is not None
    assert got.id == "C123"
    assert got.state["channel_id"] == "C123"
    assert got.state["fye_month"] == 3
    assert got.events == []


def test_append_event_persists_events_and_state():
    db = FakeFirestore()
    svc = FirestoreSessionService(client=db)
    session = _run(
        svc.create_session(app_name="acc", user_id="u", session_id="s", state={})
    )

    _run(svc.append_event(session, _make_event("first", {"step": 1})))
    _run(svc.append_event(session, _make_event("second", {"step": 2})))

    got = _run(svc.get_session(app_name="acc", user_id="u", session_id="s"))
    assert got is not None
    assert len(got.events) == 2
    assert got.events[0].content.parts[0].text == "first"
    assert got.events[1].content.parts[0].text == "second"
    # State delta merged and persisted.
    assert got.state["step"] == 2


def test_event_json_round_trip_is_faithful():
    """The exact Event (incl. function_call interrupt) survives JSON round-trip."""
    db = FakeFirestore()
    svc = FirestoreSessionService(client=db)
    session = _run(
        svc.create_session(app_name="acc", user_id="u", session_id="s", state={})
    )

    interrupt_event = Event(
        author="wf",
        content=types.Content(
            parts=[
                types.Part(
                    function_call=types.FunctionCall(
                        name="adk_request_input",
                        id="op-42",
                        args={"message": "approve?"},
                    )
                )
            ]
        ),
        long_running_tool_ids=["op-42"],
    )
    _run(svc.append_event(session, interrupt_event))

    got = _run(svc.get_session(app_name="acc", user_id="u", session_id="s"))
    assert got is not None
    ev = got.events[0]
    fc = ev.get_function_calls()[0]
    assert fc.name == "adk_request_input"
    assert fc.id == "op-42"
    assert list(ev.long_running_tool_ids) == ["op-42"]


def test_persistence_survives_new_service_instance():
    """A new service reading the same backing store sees prior writes (restart)."""
    db = FakeFirestore()
    svc1 = FirestoreSessionService(client=db)
    session = _run(
        svc1.create_session(app_name="acc", user_id="u", session_id="s", state={})
    )
    _run(svc1.append_event(session, _make_event("hello", {"k": "v"})))

    # Simulate a bot restart: brand new service, same Firestore backing store.
    svc2 = FirestoreSessionService(client=db)
    got = _run(svc2.get_session(app_name="acc", user_id="u", session_id="s"))
    assert got is not None
    assert len(got.events) == 1
    assert got.events[0].content.parts[0].text == "hello"
    assert got.state["k"] == "v"


def test_list_and_delete_session():
    db = FakeFirestore()
    svc = FirestoreSessionService(client=db)
    _run(svc.create_session(app_name="acc", user_id="u", session_id="s1", state={}))
    _run(svc.create_session(app_name="acc", user_id="u", session_id="s2", state={}))

    listed = _run(svc.list_sessions(app_name="acc", user_id="u"))
    ids = {s.id for s in listed.sessions}
    assert ids == {"s1", "s2"}

    _run(svc.delete_session(app_name="acc", user_id="u", session_id="s1"))
    assert _run(svc.get_session(app_name="acc", user_id="u", session_id="s1")) is None
    assert _run(svc.get_session(app_name="acc", user_id="u", session_id="s2")) is not None


def test_requested_tool_confirmations_round_trip():
    """ADR-0009 smoke test: a pending ``adk_request_confirmation`` survives JSON.

    Proves our JSON-Pydantic-faithful FirestoreSessionService does NOT hit the
    "unsupported on Database/VertexAi" Tool-Confirmation limitation: an event
    carrying ``actions.requested_tool_confirmations`` round-trips with the hint
    AND payload intact.
    """
    from google.adk.tools.tool_confirmation import ToolConfirmation

    db = FakeFirestore()
    svc = FirestoreSessionService(client=db)
    session = _run(
        svc.create_session(app_name="acc", user_id="u", session_id="s", state={})
    )

    fc_id = "adk-confirm-1"
    confirm_event = Event(
        author="assistant",
        content=types.Content(
            parts=[
                types.Part(
                    function_call=types.FunctionCall(
                        name="adk_request_confirmation",
                        id=fc_id,
                        args={},
                    )
                )
            ]
        ),
        long_running_tool_ids=[fc_id],
        actions=EventActions(
            requested_tool_confirmations={
                fc_id: ToolConfirmation(
                    hint="Amend Purchase row 2: account 6090 → 6010?",
                    payload={"op": "amend", "sheet": "Purchase", "row": 2,
                             "updates": {"Account Code / COA": "6010"}},
                )
            }
        ),
    )
    _run(svc.append_event(session, confirm_event))

    # Simulate a bot restart: a brand-new service reading the same backing store.
    svc2 = FirestoreSessionService(client=db)
    got = _run(svc2.get_session(app_name="acc", user_id="u", session_id="s"))
    assert got is not None
    ev = got.events[0]
    requested = ev.actions.requested_tool_confirmations
    assert fc_id in requested
    rehydrated = requested[fc_id]
    assert rehydrated.hint == "Amend Purchase row 2: account 6090 → 6010?"
    assert rehydrated.payload["op"] == "amend"
    assert rehydrated.payload["updates"]["Account Code / COA"] == "6010"
