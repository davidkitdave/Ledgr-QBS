"""Firestore-backed ADK session service + session helpers."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from google.adk.errors.already_exists_error import AlreadyExistsError
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.sessions.base_session_service import (
    BaseSessionService,
    GetSessionConfig,
    ListSessionsResponse,
)
from google.adk.sessions.session import Session

from ledgr_slack.config import _ns
from ledgr_slack.firestore_safe import firestore_safe_state

logger = logging.getLogger(__name__)

_ROOT_COLLECTION = "sessions"


class FirestoreSessionService(BaseSessionService):
    """ADK session service persisting sessions + events to Firestore."""

    def __init__(self, client: Any = None) -> None:
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            from google.cloud import firestore

            self._client = firestore.Client()
        return self._client

    def _session_ref(self, app_name: str, user_id: str, session_id: str) -> Any:
        return (
            self.client.collection(_ns(_ROOT_COLLECTION))
            .document(app_name)
            .collection("users")
            .document(user_id)
            .collection("sessions")
            .document(session_id)
        )

    def _events_ref(self, app_name: str, user_id: str, session_id: str) -> Any:
        return self._session_ref(app_name, user_id, session_id).collection("events")

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: Optional[dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> Session:
        if not session_id:
            session_id = uuid.uuid4().hex
        ref = self._session_ref(app_name, user_id, session_id)
        if ref.get().exists:
            raise AlreadyExistsError(f"Session with id {session_id} already exists.")
        session = Session(
            app_name=app_name,
            user_id=user_id,
            id=session_id,
            state=dict(state or {}),
            last_update_time=0.0,
        )
        ref.set(self._session_to_doc(session))
        return session

    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: Optional[GetSessionConfig] = None,
    ) -> Optional[Session]:
        ref = self._session_ref(app_name, user_id, session_id)
        snap = ref.get()
        if not snap.exists:
            return None
        doc = snap.to_dict() or {}
        events = self._load_events(app_name, user_id, session_id)
        if config is not None:
            if config.num_recent_events is not None:
                events = (
                    [] if config.num_recent_events == 0 else events[-config.num_recent_events :]
                )
            if config.after_timestamp is not None:
                events = [e for e in events if e.timestamp >= config.after_timestamp]
        return Session(
            app_name=app_name,
            user_id=user_id,
            id=session_id,
            state=dict(doc.get("state") or {}),
            events=events,
            last_update_time=float(doc.get("last_update_time") or 0.0),
        )

    async def list_sessions(
        self, *, app_name: str, user_id: Optional[str] = None
    ) -> ListSessionsResponse:
        sessions: list[Session] = []
        users_col = (
            self.client.collection(_ns(_ROOT_COLLECTION)).document(app_name).collection("users")
        )
        user_docs = (
            [users_col.document(user_id)] if user_id is not None else list(users_col.list_documents())
        )
        for user_doc in user_docs:
            uid = user_doc.id
            for sess_snap in user_doc.collection("sessions").stream():
                doc = sess_snap.to_dict() or {}
                sessions.append(
                    Session(
                        app_name=app_name,
                        user_id=uid,
                        id=sess_snap.id,
                        state=dict(doc.get("state") or {}),
                        events=[],
                        last_update_time=float(doc.get("last_update_time") or 0.0),
                    )
                )
        return ListSessionsResponse(sessions=sessions)

    async def delete_session(
        self, *, app_name: str, user_id: str, session_id: str
    ) -> None:
        events_ref = self._events_ref(app_name, user_id, session_id)
        for ev_snap in events_ref.stream():
            ev_snap.reference.delete()
        self._session_ref(app_name, user_id, session_id).delete()

    async def append_event(self, session: Session, event: Event) -> Event:
        event = await super().append_event(session=session, event=event)
        if event.partial:
            return event
        app_name, user_id, session_id = session.app_name, session.user_id, session.id
        seq = len(session.events) - 1
        self._events_ref(app_name, user_id, session_id).document(self._event_doc_id(seq)).set(
            {"seq": seq, "data": event.model_dump_json()}
        )
        session.last_update_time = event.timestamp
        safe_doc = self._session_to_doc(session)
        safe_doc["state"] = firestore_safe_state(dict(session.state))
        self._session_ref(app_name, user_id, session_id).set(safe_doc, merge=True)
        return event

    @staticmethod
    def _event_doc_id(seq: int) -> str:
        return f"{seq:012d}"

    @staticmethod
    def _session_to_doc(session: Session) -> dict[str, Any]:
        return {
            "app_name": session.app_name,
            "user_id": session.user_id,
            "id": session.id,
            "state": dict(session.state),
            "last_update_time": float(session.last_update_time or 0.0),
        }

    def _load_events(self, app_name: str, user_id: str, session_id: str) -> list[Event]:
        events_ref = self._events_ref(app_name, user_id, session_id)
        snaps = list(events_ref.order_by("seq").stream())
        events: list[Event] = []
        for snap in snaps:
            data = (snap.to_dict() or {}).get("data")
            if data:
                events.append(Event.model_validate_json(data))
        return events


async def _ensure_session(
    runner: Any, app_name: str, user_id: str, session_id: Optional[str] = None
) -> None:
    """Create the session if it does not exist yet (idempotent, race-safe).

    Avoids a check-then-create TOCTOU race: attempt ``create_session`` directly
    and treat an already-exists error as success (a concurrent drop won the race).
    ``session_id`` defaults to ``user_id`` for the single-id (Q&A) caller.
    """
    if session_id is None:
        session_id = user_id

    try:
        await runner.session_service.create_session(
            app_name=app_name, user_id=user_id, session_id=session_id, state={}
        )
    except AlreadyExistsError:
        # A concurrent create won the race; the existing session is fine to reuse.
        pass

async def _apply_state_delta(
    runner: Any, app_name: str, user_id: str, session_id: str, state_delta: dict
) -> None:
    """Merge ``state_delta`` into the live session by appending a state-only event.

    Used by the chat-lane confirm flow to persist the cleared ``pending_ledger_write``
    list, the idempotency marker, and the refreshed ``ledger_data`` AFTER the
    write tools have run — so the next turn sees the post-write state. Best-effort:
    a persistence failure must not crash the chat lane (the workbook write already
    succeeded).
    """
    if not state_delta:
        return
    try:
        session = await runner.session_service.get_session(
            app_name=app_name, user_id=user_id, session_id=session_id
        )
        if session is None:
            return
        await runner.session_service.append_event(
            session,
            Event(author="assistant", actions=EventActions(state_delta=state_delta)),
        )
    except Exception:  # noqa: BLE001 — state persistence is best-effort
        logger.exception("failed to persist post-write chat state delta")

