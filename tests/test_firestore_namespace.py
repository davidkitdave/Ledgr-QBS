"""Hermetic tests for the Firestore namespace prefix (``LEDGR_FIRESTORE_NAMESPACE``).

Asserts:
- ``_ns("clients") == "dev_clients"`` when the env var is "dev".
- ``_ns("clients") == "clients"`` when the var is unset (backward compat).
- ``FirestoreLeaseLock`` and ``SlackLedgerStore`` both target the same
  prefixed top-level collection as ``_ns("clients")``, so the profile doc,
  ledger pointer, and lock doc all live under the SAME client document.
"""

from __future__ import annotations

import random

import pytest

from tests._fake_firestore import FakeFirestore, FakeFirestoreNs


# ---------------------------------------------------------------------------
# _ns() unit assertions
# ---------------------------------------------------------------------------


def test_ns_applies_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGR_FIRESTORE_NAMESPACE", "dev")
    # Re-import to pick up the patched env (config reads os.environ at call time).
    from accounting_agents.config import _ns

    assert _ns("clients") == "dev_clients"
    assert _ns("sessions") == "dev_sessions"
    assert _ns("dedup_stash") == "dev_dedup_stash"


def test_ns_no_prefix_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEDGR_FIRESTORE_NAMESPACE", raising=False)
    from accounting_agents.config import _ns

    assert _ns("clients") == "clients"
    assert _ns("sessions") == "sessions"


def test_ns_no_prefix_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGR_FIRESTORE_NAMESPACE", "")
    from accounting_agents.config import _ns

    assert _ns("clients") == "clients"


# ---------------------------------------------------------------------------
# Recording fake: captures the first top-level .collection(name) arg
# ---------------------------------------------------------------------------


class _RecordingDb:
    """Wraps FakeFirestore and records the top-level collection names called."""

    def __init__(self, db: FakeFirestore) -> None:
        self._db = db
        self.top_level_collections: list[str] = []

    def collection(self, name: str):
        self.top_level_collections.append(name)
        return self._db.collection(name)

    def transaction(self):
        return self._db.transaction()

    # Propagate the fake firestore_ns so SlackLedgerStore/FirestoreLeaseLock
    # pick it up via getattr(db, "firestore_ns", None).
    @property
    def firestore_ns(self):
        return self._db.firestore_ns


# ---------------------------------------------------------------------------
# Consistency: all three stores agree on the prefixed collection name
# ---------------------------------------------------------------------------


def test_lease_lock_uses_namespaced_clients_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FirestoreLeaseLock must target _ns("clients") as its top-level collection."""
    monkeypatch.setenv("LEDGR_FIRESTORE_NAMESPACE", "dev")

    from accounting_agents.config import _ns
    from accounting_agents.lease_lock import FirestoreLeaseLock

    rec = _RecordingDb(FakeFirestore())
    lock = FirestoreLeaseLock(
        rec,
        instance_id="test",
        firestore_ns=FakeFirestoreNs(),
        sleep=lambda _: None,
        now=lambda: 0.0,
        rng=random.Random(0),
    )
    # Trigger a collection reference via acquire.
    token = lock.acquire("client-1", "2025")
    lock.release("client-1", "2025", token)

    expected = _ns("clients")  # "dev_clients"
    assert expected in rec.top_level_collections, (
        f"FirestoreLeaseLock used {rec.top_level_collections!r}, "
        f"expected top-level collection {expected!r}"
    )


def test_ledger_store_uses_namespaced_clients_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SlackLedgerStore must target _ns("clients") as its top-level collection."""
    monkeypatch.setenv("LEDGR_FIRESTORE_NAMESPACE", "dev")

    from accounting_agents.config import _ns
    from accounting_agents.ledger_store import SlackLedgerStore

    rec = _RecordingDb(FakeFirestore())
    store = SlackLedgerStore(rec)
    # get_pointer triggers a .collection(_CLIENTS_COLLECTION) call.
    store.get_pointer("client-1", "2025")

    expected = _ns("clients")  # "dev_clients"
    assert expected in rec.top_level_collections, (
        f"SlackLedgerStore used {rec.top_level_collections!r}, "
        f"expected top-level collection {expected!r}"
    )


def test_all_stores_agree_on_clients_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lease_lock and ledger_store must resolve to the SAME top-level collection."""
    monkeypatch.setenv("LEDGR_FIRESTORE_NAMESPACE", "dev")

    from accounting_agents.config import _ns
    from accounting_agents.lease_lock import FirestoreLeaseLock
    from accounting_agents.ledger_store import SlackLedgerStore

    expected = _ns("clients")

    # --- lease_lock ---
    rec_lock = _RecordingDb(FakeFirestore())
    lock = FirestoreLeaseLock(
        rec_lock,
        instance_id="test",
        firestore_ns=FakeFirestoreNs(),
        sleep=lambda _: None,
        now=lambda: 0.0,
        rng=random.Random(0),
    )
    token = lock.acquire("c1", "2025")
    lock.release("c1", "2025", token)
    assert expected in rec_lock.top_level_collections

    # --- ledger_store ---
    rec_store = _RecordingDb(FakeFirestore())
    store = SlackLedgerStore(rec_store)
    store.get_pointer("c1", "2025")
    assert expected in rec_store.top_level_collections


def test_stores_use_bare_name_when_namespace_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When LEDGR_FIRESTORE_NAMESPACE is unset, stores use bare 'clients'."""
    monkeypatch.delenv("LEDGR_FIRESTORE_NAMESPACE", raising=False)

    from accounting_agents.config import _ns
    from accounting_agents.lease_lock import FirestoreLeaseLock
    from accounting_agents.ledger_store import SlackLedgerStore

    expected = _ns("clients")  # == "clients" (no prefix)
    assert expected == "clients"

    rec_lock = _RecordingDb(FakeFirestore())
    lock = FirestoreLeaseLock(
        rec_lock,
        instance_id="test",
        firestore_ns=FakeFirestoreNs(),
        sleep=lambda _: None,
        now=lambda: 0.0,
        rng=random.Random(0),
    )
    token = lock.acquire("c1", "2025")
    lock.release("c1", "2025", token)
    assert "clients" in rec_lock.top_level_collections

    rec_store = _RecordingDb(FakeFirestore())
    store = SlackLedgerStore(rec_store)
    store.get_pointer("c1", "2025")
    assert "clients" in rec_store.top_level_collections
