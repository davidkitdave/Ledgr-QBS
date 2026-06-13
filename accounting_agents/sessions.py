"""Firestore-backed :class:`BaseSessionService` for ADK 2.0 resume.

Why this exists
---------------
ADK 2.2.0 workflow resume (HITL pause via ``RequestInput`` → restart → resume)
relies entirely on the *session event stream*: on resume the dynamic-node
scheduler reconstructs every node's checkpoint state by **scanning
``session.events``** (see ``google/adk/workflow/utils/_rehydration_utils.py``
``_reconstruct_node_states``). There is no separate workflow-checkpoint blob to
persist. Verified empirically: a session service that faithfully stores

  * ``session.events`` (each serialized via ``Event.model_dump_json()``), and
  * the merged session ``state``

is sufficient to resume an interrupted workflow *across a full ``Runner``
re-creation* (a bot restart). ``DatabaseSessionService`` is unavailable (no
``sqlalchemy``); Firestore is already a project dependency, so this is the
infra-free persistence backend that unblocks resume.

Storage layout
--------------
``sessions/{app_name}/users/{user_id}/sessions/{session_id}`` — a document
holding ``app_name/user_id/id/state/last_update_time`` plus an ``events``
ordered subcollection (one doc per event, ``seq`` = monotonically increasing
index, ``data`` = the event JSON string).

Testability
-----------
The Firestore client is **injectable**. Pass any object that quacks like a
``google.cloud.firestore.Client`` (``.collection(...)`` → document/collection
refs). Tests inject :class:`FakeFirestore` from ``tests/`` (a dict-backed
stand-in); production lets the lazy default build a real client.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from google.adk.events.event import Event
from google.adk.sessions.base_session_service import (
    BaseSessionService,
    GetSessionConfig,
    ListSessionsResponse,
)
from google.adk.sessions.session import Session

logger = logging.getLogger(__name__)

#: Top-level Firestore collection holding all ADK sessions.
_ROOT_COLLECTION = "sessions"


class FirestoreSessionService(BaseSessionService):
    """An ADK session service persisting sessions + events to Firestore.

    Args:
        client: An optional Firestore client (or compatible fake). When omitted,
            a real ``google.cloud.firestore.Client`` is created lazily on first
            use so that importing this module never touches the network.
    """

    def __init__(self, client: Any = None) -> None:
        self._client = client

    # ------------------------------------------------------------------ #
    # Lazy Firestore client + path helpers
    # ------------------------------------------------------------------ #

    @property
    def client(self) -> Any:
        """Return the Firestore client, creating a real one on first use."""
        if self._client is None:
            from google.cloud import firestore

            self._client = firestore.Client()
        return self._client

    def _session_ref(self, app_name: str, user_id: str, session_id: str) -> Any:
        """Document ref for a single session."""
        return (
            self.client.collection(_ROOT_COLLECTION)
            .document(app_name)
            .collection("users")
            .document(user_id)
            .collection("sessions")
            .document(session_id)
        )

    def _events_ref(self, app_name: str, user_id: str, session_id: str) -> Any:
        """Collection ref for a session's ordered events."""
        return self._session_ref(app_name, user_id, session_id).collection("events")

    # ------------------------------------------------------------------ #
    # BaseSessionService abstract methods
    # ------------------------------------------------------------------ #

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: Optional[dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> Session:
        if not session_id:
            import uuid

            session_id = uuid.uuid4().hex

        ref = self._session_ref(app_name, user_id, session_id)
        if ref.get().exists:
            from google.adk.errors.already_exists_error import AlreadyExistsError

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
            self.client.collection(_ROOT_COLLECTION).document(app_name).collection("users")
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
                        events=[],  # list omits events, per ADK contract
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
        # Let the base class apply state delta to the in-memory session + trim
        # temp-scoped keys (this also returns early for partial events).
        event = await super().append_event(session=session, event=event)
        if event.partial:
            return event

        app_name, user_id, session_id = session.app_name, session.user_id, session.id

        # Persist the event in its own subcollection doc, ordered by seq.
        seq = len(session.events) - 1  # base class already appended in-memory
        self._events_ref(app_name, user_id, session_id).document(self._event_doc_id(seq)).set(
            {"seq": seq, "data": event.model_dump_json()}
        )

        # Persist the merged session state + last_update_time.
        session.last_update_time = event.timestamp
        self._session_ref(app_name, user_id, session_id).set(
            self._session_to_doc(session), merge=True
        )
        return event

    # ------------------------------------------------------------------ #
    # Serialization helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _event_doc_id(seq: int) -> str:
        """Zero-padded doc id so lexical ordering == numeric ordering."""
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
