"""Hermetic tests for the durable ``FirestoreCreditStore``.

Uses the dict-backed ``FakeFirestore`` (which carries a ``transaction()`` +
``FakeFirestoreNs`` ``transactional`` shim) so no real firestore is imported and
no network is touched — mirroring the lease-lock / sessions test pattern.
"""

from __future__ import annotations

from app.credit_service import CreditService, FirestoreCreditStore
from tests._fake_firestore import FakeFirestore


def _store(db: FakeFirestore) -> FirestoreCreditStore:
    return FirestoreCreditStore(client=db, firestore_ns=db.firestore_ns)


def test_unknown_firm_reads_zero() -> None:
    store = _store(FakeFirestore())
    assert store.read_balance("nope") == 0


def test_ensure_firm_creates_zero_balance_doc() -> None:
    db = FakeFirestore()
    store = _store(db)
    store.ensure_firm("T1")
    assert store.read_balance("T1") == 0


def test_grant_increments_balance() -> None:
    db = FakeFirestore()
    store = _store(db)
    assert store.apply_grant("T1", 10, note="trial") == 10
    assert store.apply_grant("T1", 5, note="topup") == 15
    assert store.read_balance("T1") == 15


def test_deduct_decrements_balance() -> None:
    db = FakeFirestore()
    store = _store(db)
    store.apply_grant("T1", 10, note="trial")
    assert store.apply_deduct("T1", 3, reason="delivery", idempotency_key="k1") == 7
    assert store.read_balance("T1") == 7


def test_double_deduct_same_key_is_idempotent_no_double_spend() -> None:
    db = FakeFirestore()
    store = _store(db)
    store.apply_grant("T1", 10, note="trial")
    first = store.apply_deduct("T1", 4, reason="delivery", idempotency_key="dup")
    second = store.apply_deduct("T1", 4, reason="delivery", idempotency_key="dup")
    assert first == 6
    assert second == 6  # marker existed → unchanged, NOT 2
    assert store.read_balance("T1") == 6


def test_distinct_keys_each_charge() -> None:
    db = FakeFirestore()
    store = _store(db)
    store.apply_grant("T1", 10, note="trial")
    store.apply_deduct("T1", 1, reason="delivery", idempotency_key="a")
    store.apply_deduct("T1", 2, reason="delivery", idempotency_key="b")
    assert store.read_balance("T1") == 7


def test_store_satisfies_credit_service_facade() -> None:
    db = FakeFirestore()
    service = CreditService(_store(db))
    service.ensure_firm("T1")
    service.grant("T1", 8, note="trial")
    assert service.deduct("T1", 3, reason="delivery", idempotency_key="x") == 5
    assert service.read_balance("T1") == 5


def test_namespace_applied_to_collection(monkeypatch) -> None:
    monkeypatch.setenv("LEDGR_FIRESTORE_NAMESPACE", "dev")
    db = FakeFirestore()
    store = FirestoreCreditStore(client=db, firestore_ns=db.firestore_ns)
    store.apply_grant("T1", 1, note="x")
    # The firm doc must live under the namespaced collection.
    assert ("dev_credit_firms", "T1") in db._store


def test_read_balance_defensive_on_error() -> None:
    class _BoomDb:
        def collection(self, *_a, **_k):
            raise RuntimeError("firestore down")

    store = FirestoreCreditStore(client=_BoomDb(), firestore_ns=FakeFirestore().firestore_ns)
    # Must not raise — defensive read returns 0 so the gate fails safe.
    assert store.read_balance("T1") == 0
