"""Hermetic tests for the multi-workspace OAuth stores (plan task #5.1).

No live GCP calls, no network. Uses a hand-rolled fake Firestore client (the
same shape as ``tests/test_firestore_store.py``) injected via the ``client=``
seam so no real ``firestore.Client`` is ever constructed.

Covers:
  * FirestoreInstallationStore: save / find_bot / find_installation / delete.
  * FirestoreOAuthStateStore: issue / consume (one-time) / expiry.
  * config.missing_slack_oauth() for set/unset env.
"""

from __future__ import annotations

from slack_sdk.oauth.installation_store import Bot, Installation

from app.config import missing_slack_oauth
from app.installation_store import (
    FirestoreInstallationStore,
    FirestoreOAuthStateStore,
)


# --------------------------------------------------------------------------- #
# Hand-rolled fake Firestore (mirrors tests/test_firestore_store.py)
# --------------------------------------------------------------------------- #

class FakeSnapshot:
    def __init__(self, data: dict | None):
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict:
        return dict(self._data) if self._data is not None else {}


class FakeDocRef:
    """Writable document reference inside a FakeCollectionRef."""

    def __init__(self, store: dict[str, dict], doc_id: str):
        self._store = store
        self._doc_id = doc_id

    def get(self) -> FakeSnapshot:
        return FakeSnapshot(self._store.get(self._doc_id))

    def set(self, data: dict, merge: bool = False) -> None:
        if merge and self._doc_id in self._store:
            self._store[self._doc_id] = {**self._store[self._doc_id], **data}
        else:
            self._store[self._doc_id] = dict(data)

    def delete(self) -> None:
        self._store.pop(self._doc_id, None)


class FakeCollectionRef:
    def __init__(self, docs: dict[str, dict] | None = None):
        self._docs: dict[str, dict] = docs or {}

    def document(self, doc_id: str) -> FakeDocRef:
        return FakeDocRef(self._docs, doc_id)


class FakeFirestore:
    """Minimal fake Firestore client: .collection(name).document(id)..."""

    def __init__(self):
        self._collections: dict[str, FakeCollectionRef] = {}

    def collection(self, name: str) -> FakeCollectionRef:
        if name not in self._collections:
            self._collections[name] = FakeCollectionRef()
        return self._collections[name]


# --------------------------------------------------------------------------- #
# FirestoreInstallationStore
# --------------------------------------------------------------------------- #

def _installation() -> Installation:
    return Installation(
        app_id="A1",
        enterprise_id=None,
        team_id="T1",
        bot_token="xoxb-1",
        bot_id="B1",
        bot_user_id="U1",
        bot_scopes=["chat:write"],
        user_id="U1",
    )


class TestFirestoreInstallationStore:

    def _store(self) -> FirestoreInstallationStore:
        return FirestoreInstallationStore(client=FakeFirestore())

    def test_constructs_without_network(self):
        # Constructing with no injected client must NOT build a real client.
        store = FirestoreInstallationStore()
        assert store._client is None
        assert store._injected_client is None

    def test_save_then_find_bot_returns_bot_with_token(self):
        store = self._store()
        store.save(_installation())
        bot = store.find_bot(enterprise_id=None, team_id="T1")
        assert isinstance(bot, Bot)
        assert bot.bot_token == "xoxb-1"
        assert bot.bot_id == "B1"
        assert bot.bot_user_id == "U1"

    def test_save_then_find_installation_returns_installation(self):
        store = self._store()
        store.save(_installation())
        found = store.find_installation(enterprise_id=None, team_id="T1")
        assert isinstance(found, Installation)
        assert found.team_id == "T1"
        assert found.bot_token == "xoxb-1"

    def test_find_installation_missing_team_returns_none(self):
        store = self._store()
        store.save(_installation())
        assert store.find_installation(enterprise_id=None, team_id="NOPE") is None

    def test_find_bot_missing_team_returns_none(self):
        store = self._store()
        store.save(_installation())
        assert store.find_bot(enterprise_id=None, team_id="NOPE") is None

    def test_delete_installation_then_find_returns_none(self):
        store = self._store()
        store.save(_installation())
        assert store.find_installation(enterprise_id=None, team_id="T1") is not None
        store.delete_installation(enterprise_id=None, team_id="T1")
        assert store.find_installation(enterprise_id=None, team_id="T1") is None

    def test_reconstruction_filters_unknown_keys(self):
        # A stored dict with extra/legacy keys must still reconstruct cleanly.
        store = self._store()
        store.save(_installation())
        # Inject a junk key directly into the stored doc.
        doc = store._db().collection("workspaces").document("none-T1")
        data = doc.get().to_dict()
        data["__unknown_legacy_field__"] = "junk"
        doc.set(data, merge=False)
        found = store.find_installation(enterprise_id=None, team_id="T1")
        assert found is not None
        assert found.team_id == "T1"

    def test_no_real_client_constructed_after_use(self):
        store = self._store()
        store.save(_installation())
        store.find_bot(enterprise_id=None, team_id="T1")
        # _db() returned the injected fake; the lazy real client stays None.
        assert store._client is None


# --------------------------------------------------------------------------- #
# FirestoreOAuthStateStore
# --------------------------------------------------------------------------- #

class TestFirestoreOAuthStateStore:

    def test_issue_returns_nonempty_str_and_persists(self):
        fake = FakeFirestore()
        store = FirestoreOAuthStateStore(client=fake)
        state = store.issue()
        assert isinstance(state, str)
        assert state
        # doc exists in the fake
        snap = fake.collection("oauth_states").document(state).get()
        assert snap.exists
        assert "created_at" in snap.to_dict()

    def test_consume_true_once_then_false(self):
        store = FirestoreOAuthStateStore(client=FakeFirestore())
        state = store.issue()
        assert store.consume(state) is True
        # second consume fails — the doc was deleted (one-time use)
        assert store.consume(state) is False

    def test_consume_unknown_state_returns_false(self):
        store = FirestoreOAuthStateStore(client=FakeFirestore())
        assert store.consume("never-issued") is False

    def test_expired_state_consume_false(self):
        # expiration_seconds=0 → any positive age is expired.
        store = FirestoreOAuthStateStore(client=FakeFirestore(), expiration_seconds=0)
        state = store.issue()
        # Force created_at far in the past to guarantee expiry regardless of clock.
        doc = store._db().collection("oauth_states").document(state)
        doc.set({"created_at": 0.0}, merge=False)
        assert store.consume(state) is False
        # expired doc is cleaned up
        assert doc.get().exists is False

    def test_constructs_without_network(self):
        store = FirestoreOAuthStateStore()
        assert store._client is None
        assert store._injected_client is None


# --------------------------------------------------------------------------- #
# config.missing_slack_oauth()
# --------------------------------------------------------------------------- #

class TestMissingSlackOAuth:

    _VARS = (
        "SLACK_CLIENT_ID",
        "SLACK_CLIENT_SECRET",
        "SLACK_SIGNING_SECRET",
        "SLACK_BASE_URL",
    )

    def test_all_missing_when_unset(self, monkeypatch):
        for var in self._VARS:
            monkeypatch.delenv(var, raising=False)
        assert set(missing_slack_oauth()) == set(self._VARS)

    def test_none_missing_when_all_set(self, monkeypatch):
        monkeypatch.setenv("SLACK_CLIENT_ID", "id")
        monkeypatch.setenv("SLACK_CLIENT_SECRET", "secret")
        monkeypatch.setenv("SLACK_SIGNING_SECRET", "sign")
        monkeypatch.setenv("SLACK_BASE_URL", "https://example.run.app")
        assert missing_slack_oauth() == []

    def test_partial_missing(self, monkeypatch):
        monkeypatch.setenv("SLACK_CLIENT_ID", "id")
        monkeypatch.setenv("SLACK_CLIENT_SECRET", "secret")
        monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
        monkeypatch.delenv("SLACK_BASE_URL", raising=False)
        missing = missing_slack_oauth()
        assert "SLACK_SIGNING_SECRET" in missing
        assert "SLACK_BASE_URL" in missing
        assert "SLACK_CLIENT_ID" not in missing
        assert "SLACK_CLIENT_SECRET" not in missing
