"""Concurrency safety tests for the Slack runner (QUICK-WIN fixes).

Covers the low-risk concurrency hardening so a few simultaneous document drops are
safe:

(a) two concurrent ``process_file_event`` calls for DIFFERENT file ids use DISTINCT
    per-document session ids (``f"{channel_id}:{file_id}"``) and never collide;
(b) the module-level semaphore caps the number of in-flight runs at N (instrumented
    with a live counter);
(c) the synchronous ``append_rows`` ledger write is invoked via
    ``asyncio.to_thread`` (so it never blocks the event loop);
(d) ``_ensure_session`` is idempotent under a simulated concurrent create: a second
    ``create_session`` raising ``AlreadyExistsError`` is handled, no crash.

All hermetic — no live Slack / Gemini / Firestore.
"""

from __future__ import annotations

import asyncio

from types import SimpleNamespace

import accounting_agents.slack_runner as slack_runner
from accounting_agents import nodes
from accounting_agents.slack_runner import (
    _ensure_session,
    _per_doc_session_id,
    answer_question,
    process_file_event,
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


def _ledger_payload():
    return {
        nodes.LEDGER_ROWS_KEY: {
            "client_id": "c1",
            "fy": "2026",
            "kind": "invoice",
            "software": "qbs",
            "batches": [
                {
                    "sheet": "Purchase",
                    "doc_key": "F:Purchase:INV",
                    "rows": [{"Invoice Number": "INV", "Source Amount": 1.0}],
                }
            ],
        },
        nodes.DELIVER_SUMMARY_KEY: "done",
    }


class _FakeSession:
    def __init__(self, state):
        self.state = dict(state)


class _RecordingSessionService:
    """Records (user_id, session_id) for every create/get and run scope."""

    def __init__(self, final_state):
        self._final_state = final_state
        self._created: set = set()
        self.create_calls: list = []

    async def get_session(self, *, app_name, user_id, session_id):
        if (user_id, session_id) not in self._created:
            return None
        return _FakeSession(self._final_state)

    async def create_session(self, *, app_name, user_id, session_id, state=None):
        self.create_calls.append((user_id, session_id))
        self._created.add((user_id, session_id))
        return _FakeSession(state or {})


class _FakeArtifactService:
    def __init__(self):
        self.saved = {}

    async def save_artifact(self, *, app_name, user_id, filename, artifact, session_id=None, custom_metadata=None):
        self.saved[(user_id, session_id, filename)] = artifact
        return 0


class _InstrumentedRunner:
    """Runner stand-in that records the session scope of each run_async call and
    tracks concurrent in-flight runs (for the semaphore test)."""

    def __init__(self, final_state, *, app_name="acc", run_delay=0.0, counter=None):
        self.app_name = app_name
        self.artifact_service = _FakeArtifactService()
        self.session_service = _RecordingSessionService(final_state)
        self.run_scopes: list = []
        self._run_delay = run_delay
        self._counter = counter  # optional {"now": int, "max": int}

    async def run_async(self, *, user_id, session_id, new_message=None, state_delta=None):
        self.run_scopes.append((user_id, session_id))
        self.session_service._created.add((user_id, session_id))
        if self._counter is not None:
            self._counter["now"] += 1
            self._counter["max"] = max(self._counter["max"], self._counter["now"])
        try:
            if self._run_delay:
                await asyncio.sleep(self._run_delay)
            yield SimpleNamespace(
                content=SimpleNamespace(parts=[SimpleNamespace(text="done")]),
                get_function_calls=lambda: [],
            )
        finally:
            if self._counter is not None:
                self._counter["now"] -= 1


class _NoopLedgerStore:
    def __init__(self):
        self.append_calls = 0
        self.read_calls = 0

    def append_rows(self, **kwargs):
        self.append_calls += 1
        return {"appended": 1, "deduped": 0, "filename": "x.xlsx", "slack_file_id": "wb"}

    def read_rows(self, **kwargs):
        self.read_calls += 1
        return []


class _FakeFirestoreDoc:
    def set(self, *a, **k):
        pass


class _FakeFirestoreCollection:
    def document(self, _id):
        return _FakeFirestoreDoc()


class _FakeDb:
    def collection(self, _name):
        return _FakeFirestoreCollection()


def _slack():
    posts: list = []
    return SimpleNamespace(chat_postMessage=lambda **k: posts.append(k) or {"ts": "1"}, _posts=posts)


# --------------------------------------------------------------------------- #
# (a) distinct per-doc session ids for concurrent drops
# --------------------------------------------------------------------------- #


def test_concurrent_file_events_use_distinct_session_ids():
    runner = _InstrumentedRunner(_ledger_payload())
    store = _NoopLedgerStore()
    db = _FakeDb()
    slack = _slack()

    async def drive():
        await asyncio.gather(
            process_file_event(
                runner=runner, ledger_store=store, db=db, slack_client=slack,
                channel_id="C1", file_id="F1", app_name="acc",
                download_fn=lambda c, f: b"%PDF a",
            ),
            process_file_event(
                runner=runner, ledger_store=store, db=db, slack_client=slack,
                channel_id="C1", file_id="F2", app_name="acc",
                download_fn=lambda c, f: b"%PDF b",
            ),
        )

    asyncio.run(drive())

    # Both runs targeted the SAME user_id (channel) but DISTINCT per-doc sessions.
    scopes = set(runner.run_scopes)
    assert ("C1", _per_doc_session_id("C1", "F1")) in scopes
    assert ("C1", _per_doc_session_id("C1", "F2")) in scopes
    assert _per_doc_session_id("C1", "F1") != _per_doc_session_id("C1", "F2")
    # user_id stayed the channel for both (client-profile resolution unchanged).
    assert {uid for uid, _ in runner.run_scopes} == {"C1"}


# --------------------------------------------------------------------------- #
# (b) semaphore caps concurrent in-flight runs at N
# --------------------------------------------------------------------------- #


def test_semaphore_caps_concurrent_runs(monkeypatch):
    # Force a small cap independent of the environment.
    monkeypatch.setattr(slack_runner, "_SEM", asyncio.Semaphore(2))
    counter = {"now": 0, "max": 0}
    runner = _InstrumentedRunner(_ledger_payload(), run_delay=0.02, counter=counter)
    store = _NoopLedgerStore()
    db = _FakeDb()
    slack = _slack()

    async def drive():
        await asyncio.gather(*[
            process_file_event(
                runner=runner, ledger_store=store, db=db, slack_client=slack,
                channel_id="C1", file_id=f"F{i}", app_name="acc",
                download_fn=lambda c, f: b"%PDF",
            )
            for i in range(6)
        ])

    asyncio.run(drive())

    # Never more than 2 runs in flight at once despite 6 simultaneous drops.
    assert counter["max"] <= 2
    # All six still completed (queued, not dropped).
    assert len(runner.run_scopes) == 6


# --------------------------------------------------------------------------- #
# (c) append_rows is invoked via asyncio.to_thread (non-blocking)
# --------------------------------------------------------------------------- #


def test_append_rows_is_offloaded_to_thread(monkeypatch):
    runner = _InstrumentedRunner(_ledger_payload())
    store = _NoopLedgerStore()
    db = _FakeDb()
    slack = _slack()

    offloaded: list = []
    real_to_thread = asyncio.to_thread

    async def spy_to_thread(fn, *args, **kwargs):
        offloaded.append(fn)
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(slack_runner.asyncio, "to_thread", spy_to_thread)

    result = asyncio.run(
        process_file_event(
            runner=runner, ledger_store=store, db=db, slack_client=slack,
            channel_id="C1", file_id="F1", app_name="acc",
            download_fn=lambda c, f: b"%PDF",
        )
    )

    assert result["status"] == "delivered"
    # The synchronous ledger write was dispatched through to_thread.
    assert store.append_rows in offloaded
    assert store.append_calls == 1


def test_answer_question_offloads_read_rows_to_thread(monkeypatch):
    runner = _InstrumentedRunner({}, app_name="acc")
    store = _NoopLedgerStore()
    slack = _slack()

    offloaded: list = []
    real_to_thread = asyncio.to_thread

    async def spy_to_thread(fn, *args, **kwargs):
        offloaded.append(fn)
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(slack_runner.asyncio, "to_thread", spy_to_thread)

    asyncio.run(
        answer_question(
            runner=runner, ledger_store=store, slack_client=slack,
            channel_id="C1", question="how much?", app_name="acc",
            message_ts="100.1",
        )
    )

    # The synchronous Slack-IO read was dispatched through to_thread.
    assert store.read_rows in offloaded
    # Per-question session id was used (not the bare channel).
    assert ("C1", "C1:q:100.1") in runner.run_scopes


# --------------------------------------------------------------------------- #
# (d) _ensure_session is idempotent under a concurrent create race
# --------------------------------------------------------------------------- #


def test_ensure_session_idempotent_on_already_exists():
    from google.adk.errors.already_exists_error import AlreadyExistsError

    calls = {"n": 0}

    class _RaceSessionService:
        async def create_session(self, *, app_name, user_id, session_id, state=None):
            calls["n"] += 1
            # Simulate a concurrent create having already won the race.
            raise AlreadyExistsError(f"Session {session_id} already exists.")

        async def get_session(self, *, app_name, user_id, session_id):
            return _FakeSession({})

    runner = SimpleNamespace(session_service=_RaceSessionService())

    # Must NOT raise — the already-exists error is treated as success.
    asyncio.run(_ensure_session(runner, "acc", "C1", "C1:F1"))
    assert calls["n"] == 1
