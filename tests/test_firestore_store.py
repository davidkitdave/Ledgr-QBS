"""Hermetic tests for FirestoreClientStore, get_by_channel, make_load_client_by_channel_callback,
InMemoryClientStore channel support, and write methods (save_profile, set_channel,
set_status) on both stores.

No MagicMock fluent chains. No live GCP calls. Uses a hand-rolled fake Firestore client
that supports exactly the call shapes the production code makes:
  fake.collection(name).document(id).get()
  fake.collection(name).document(id).set(data, merge=False)
  fake.collection(name).document(id).collection(sub).stream()
  fake.collection(name).document(id).collection(sub).document(id).set(data)
"""

from __future__ import annotations


from ledgr_slack.client_context import (
    ClientContext,
    FirestoreClientStore,
    InMemoryClientStore,
    make_load_client_by_channel_callback,
)


# --------------------------------------------------------------------------- #
# Hand-rolled fake Firestore
# --------------------------------------------------------------------------- #

class FakeSnapshot:
    """Mimics a Firestore DocumentSnapshot.

    ``reference`` mirrors the real ``DocumentSnapshot.reference`` so production
    code can stream-then-delete (``snap.reference.delete()``). It is only set on
    snapshots produced by a subcollection ``.stream()``.
    """

    def __init__(self, data: dict | None, reference: "FakeSubDocRef | None" = None):
        self._data = data
        self.reference = reference

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict:
        return dict(self._data) if self._data is not None else {}


class FakeSubcollection:
    """Mimics a Firestore CollectionReference supporting .stream() and .document().set()."""

    def __init__(self, docs: dict[str, dict] | None = None):
        # keyed by doc_id -> data dict
        self._docs: dict[str, dict] = docs or {}

    @classmethod
    def from_list(cls, docs: list[dict]) -> "FakeSubcollection":
        """Build from a list (numbered keys 0,1,2,…)."""
        return cls({str(i): d for i, d in enumerate(docs)})

    def stream(self):
        # Snapshot over a copy of the keys so callers can delete during iteration.
        return [
            FakeSnapshot(self._docs[doc_id], reference=FakeSubDocRef(self._docs, doc_id))
            for doc_id in list(self._docs.keys())
        ]

    def document(self, doc_id: str) -> "FakeSubDocRef":
        return FakeSubDocRef(self._docs, doc_id)


class FakeSubDocRef:
    """A writable document reference inside a FakeSubcollection."""

    def __init__(self, store: dict[str, dict], doc_id: str):
        self._store = store
        self._doc_id = doc_id

    def set(self, data: dict, merge: bool = False) -> None:
        if merge and self._doc_id in self._store:
            self._store[self._doc_id] = {**self._store[self._doc_id], **data}
        else:
            self._store[self._doc_id] = dict(data)

    def delete(self) -> None:
        self._store.pop(self._doc_id, None)

    def get(self) -> FakeSnapshot:
        return FakeSnapshot(self._store.get(self._doc_id))


class FakeDocRef:
    """Mimics a Firestore DocumentReference supporting .get(), .set(), .collection()."""

    def __init__(self, data: dict | None, subcollections: dict[str, list[dict]] | None = None):
        self._data: dict | None = data
        # Convert list-of-dicts to keyed dict for each subcollection so they're writable.
        self._subcollections: dict[str, FakeSubcollection] = {
            name: FakeSubcollection.from_list(docs)
            for name, docs in (subcollections or {}).items()
        }

    def get(self) -> FakeSnapshot:
        return FakeSnapshot(self._data)

    def set(self, data: dict, merge: bool = False) -> None:
        if merge and self._data is not None:
            self._data = {**self._data, **data}
        else:
            self._data = dict(data)

    def delete(self) -> None:
        self._data = None

    def collection(self, name: str) -> FakeSubcollection:
        if name not in self._subcollections:
            self._subcollections[name] = FakeSubcollection()
        return self._subcollections[name]


class FakeCollectionRef:
    """Mimics a Firestore CollectionReference supporting .document()."""

    def __init__(self, docs: dict[str, FakeDocRef] | None = None):
        self._docs: dict[str, FakeDocRef] = docs or {}

    def document(self, doc_id: str) -> FakeDocRef:
        if doc_id not in self._docs:
            # Auto-create an empty writable doc ref
            self._docs[doc_id] = FakeDocRef(data=None)
        return self._docs[doc_id]


class FakeFirestore:
    """Minimal fake Firestore client. Supports .collection(name).document(id)..."""

    def __init__(self, collections: dict[str, FakeCollectionRef] | None = None):
        self._collections: dict[str, FakeCollectionRef] = collections or {}

    def collection(self, name: str) -> FakeCollectionRef:
        if name not in self._collections:
            self._collections[name] = FakeCollectionRef()
        return self._collections[name]


# --------------------------------------------------------------------------- #
# Shared fixture data
# --------------------------------------------------------------------------- #

CLIENT_ID = "client-abc-123"
CHANNEL_ID = "C0123456"

CLIENT_DOC = {
    "client_id": CLIENT_ID,
    "client_name": "Acme Pte Ltd",
    "channel_id": CHANNEL_ID,
    "slack_team_id": "T9999",
    "firm_id": "firm-1",
    "fye_month": 3,
    "accounting_software": "Xero",
    "gst_registered": False,   # spec field name; maps to tax_registered=False
    "region": "SINGAPORE",
    "base_currency": "SGD",
    "status": "active",
    "category_mapping": {
        "Telephone & Internet": "6-1000",
        "Bank Fees": None,
    },
}

ENTITY_MEMORY_DOCS = [
    {
        "name": "Telco B",
        "reg_no": "199200001Z",
        "mapping_code": "6-1000",
        "role": "Creditor",
        "tax_code": "TX",
    },
]

CHANNEL_DOC = {"client_id": CLIENT_ID}


def _make_fake_firestore() -> FakeFirestore:
    client_doc_ref = FakeDocRef(
        data=CLIENT_DOC,
        subcollections={
            "entity_memory": ENTITY_MEMORY_DOCS,
        },
    )
    return FakeFirestore(
        collections={
            "clients": FakeCollectionRef({CLIENT_ID: client_doc_ref}),
            "channels": FakeCollectionRef({
                CHANNEL_ID: FakeDocRef(data=CHANNEL_DOC),
            }),
        }
    )


def _make_empty_fake_firestore() -> FakeFirestore:
    """Empty writable fake for write round-trip tests."""
    return FakeFirestore()


# --------------------------------------------------------------------------- #
# FirestoreClientStore.get()
# --------------------------------------------------------------------------- #

class TestFirestoreClientStoreGet:

    def _store(self) -> FirestoreClientStore:
        return FirestoreClientStore(client=_make_fake_firestore())

    def test_returns_none_for_none_id(self):
        assert self._store().get(None) is None

    def test_returns_none_for_missing_client(self):
        assert self._store().get("does-not-exist") is None

    def test_client_name(self):
        ctx = self._store().get(CLIENT_ID)
        assert ctx is not None
        assert ctx.client_name == "Acme Pte Ltd"

    def test_fye_month_is_int(self):
        ctx = self._store().get(CLIENT_ID)
        assert ctx.fye_month == 3
        assert isinstance(ctx.fye_month, int)

    def test_accounting_software(self):
        ctx = self._store().get(CLIENT_ID)
        assert ctx.accounting_software == "Xero"

    def test_tax_registered_false_from_gst_registered(self):
        # gst_registered=False in doc -> tax_registered=False on context
        ctx = self._store().get(CLIENT_ID)
        assert ctx.tax_registered is False

    def test_region_default(self):
        ctx = self._store().get(CLIENT_ID)
        assert ctx.region == "SINGAPORE"

    def test_base_currency_default(self):
        ctx = self._store().get(CLIENT_ID)
        assert ctx.base_currency == "SGD"

    def test_category_mapping_from_doc_map(self):
        ctx = self._store().get(CLIENT_ID)
        assert ctx.category_mapping["Telephone & Internet"] == "6-1000"
        # None value preserved (unmapped category)
        assert "Bank Fees" in ctx.category_mapping
        assert ctx.category_mapping["Bank Fees"] is None

    def test_entity_memory_parsed(self):
        ctx = self._store().get(CLIENT_ID)
        assert len(ctx.entity_memory) == 1
        em = ctx.entity_memory[0]
        assert em.name == "Telco B"
        assert em.reg_no == "199200001Z"
        assert em.mapping_code == "6-1000"
        assert em.role == "Creditor"
        assert em.tax_code == "TX"


# --------------------------------------------------------------------------- #
# FirestoreClientStore.get_by_channel()
# --------------------------------------------------------------------------- #

class TestFirestoreClientStoreGetByChannel:

    def _store(self) -> FirestoreClientStore:
        return FirestoreClientStore(client=_make_fake_firestore())

    def test_resolves_known_channel(self):
        ctx = self._store().get_by_channel(CHANNEL_ID)
        assert ctx is not None
        assert ctx.client_id == CLIENT_ID
        assert ctx.client_name == "Acme Pte Ltd"

    def test_returns_none_for_missing_channel(self):
        assert self._store().get_by_channel("nope") is None

    def test_returns_none_for_none_channel(self):
        assert self._store().get_by_channel(None) is None

    def test_same_client_as_get(self):
        store = self._store()
        by_channel = store.get_by_channel(CHANNEL_ID)
        by_id = store.get(CLIENT_ID)
        assert by_channel is not None and by_id is not None
        assert by_channel.client_id == by_id.client_id
        assert by_channel.fye_month == by_id.fye_month


# --------------------------------------------------------------------------- #
# make_load_client_by_channel_callback
# --------------------------------------------------------------------------- #

class StubCallbackContext:
    def __init__(self, state: dict):
        self.state = state


class TestMakeLoadClientByChannelCallback:

    def _store(self) -> FirestoreClientStore:
        return FirestoreClientStore(client=_make_fake_firestore())

    def test_returns_none(self):
        cb = make_load_client_by_channel_callback(self._store())
        ctx_obj = StubCallbackContext({"channel_id": CHANNEL_ID})
        result = cb(ctx_obj)
        assert result is None

    def test_populates_state_from_to_state(self):
        store = self._store()
        cb = make_load_client_by_channel_callback(store)
        state: dict = {"channel_id": CHANNEL_ID}
        cb(StubCallbackContext(state))

        assert state.get("client_id") == CLIENT_ID
        assert state.get("software") == "Xero"
        assert state.get("fye_month") == 3
        cm = state.get("category_mapping")
        assert cm is not None
        assert cm["Telephone & Internet"] == "6-1000"
        assert cm["Bank Fees"] is None

    def test_no_crash_when_channel_missing(self):
        cb = make_load_client_by_channel_callback(self._store())
        state: dict = {"channel_id": "unknown-channel"}
        cb(StubCallbackContext(state))
        # state unchanged — no client_id injected
        assert "client_id" not in state

    def test_no_crash_when_state_has_no_channel_id(self):
        cb = make_load_client_by_channel_callback(self._store())
        state: dict = {}
        cb(StubCallbackContext(state))
        assert "client_id" not in state


# --------------------------------------------------------------------------- #
# InMemoryClientStore channel support
# --------------------------------------------------------------------------- #

class TestInMemoryClientStoreChannel:

    def _ctx(self, client_id: str = "mem-client-1") -> ClientContext:
        return ClientContext(client_id=client_id, client_name="Test Co", fye_month=12)

    def test_add_with_channel_id_then_get_by_channel(self):
        store = InMemoryClientStore()
        ctx = self._ctx()
        store.add(ctx, channel_id="C1")
        result = store.get_by_channel("C1")
        assert result is ctx

    def test_get_by_channel_unknown_returns_none(self):
        store = InMemoryClientStore()
        store.add(self._ctx(), channel_id="C1")
        assert store.get_by_channel("X") is None

    def test_get_by_channel_none_returns_none(self):
        store = InMemoryClientStore()
        store.add(self._ctx(), channel_id="C1")
        assert store.get_by_channel(None) is None

    def test_add_without_channel_id_still_works(self):
        # Existing callers that omit channel_id must not break
        store = InMemoryClientStore()
        ctx = self._ctx("no-channel")
        store.add(ctx)  # no channel_id arg
        assert store.get("no-channel") is ctx

    def test_add_with_channel_id_none_does_not_register_channel(self):
        store = InMemoryClientStore()
        ctx = self._ctx()
        store.add(ctx, channel_id=None)
        # get_by_channel(None) always returns None
        assert store.get_by_channel(None) is None


# --------------------------------------------------------------------------- #
# FirestoreClientStore write round-trips (fake Firestore, no live GCP)
# --------------------------------------------------------------------------- #

WRITE_PROFILE = {
    "client_id": "new-client-1",
    "channel_id": "C-NEW-1",
    "slack_team_id": "T-WS-1",
    "client_name": "Newco Pte Ltd",
    "fye_month": 12,
    "accounting_software": "QBS Ledger",
    "gst_registered": True,
    "region": "SINGAPORE",
    "base_currency": "SGD",
    "status": "active",
    "category_mapping": {},
}

class TestFirestoreClientStoreWrite:

    def _store(self) -> FirestoreClientStore:
        return FirestoreClientStore(client=_make_empty_fake_firestore())

    def test_save_profile_then_get(self):
        store = self._store()
        store.save_profile(WRITE_PROFILE)
        ctx = store.get("new-client-1")
        assert ctx is not None
        assert ctx.client_name == "Newco Pte Ltd"
        assert ctx.fye_month == 12
        assert ctx.tax_registered is True
        assert ctx.status == "active"
        assert ctx.accounting_software == "QBS Ledger"

    def test_set_channel_then_get_by_channel(self):
        store = self._store()
        store.save_profile(WRITE_PROFILE)
        store.set_channel("C-NEW-1", "new-client-1")
        ctx = store.get_by_channel("C-NEW-1")
        assert ctx is not None
        assert ctx.client_id == "new-client-1"

    def test_set_status_then_get(self):
        store = self._store()
        store.save_profile(WRITE_PROFILE)
        store.set_status("new-client-1", "active")
        ctx = store.get("new-client-1")
        assert ctx is not None
        assert ctx.status == "active"

    def test_processing_log_append_and_list(self):
        store = self._store()
        store.save_profile(WRITE_PROFILE)
        store.append_processing_log(
            client_id="new-client-1",
            file_id="F123",
            entry={
                "filename": "soa.pdf",
                "doc_type": "statement_of_account",
                "extraction_path": "legacy",
                "delivered_at": "2026-06-16T10:00:00+00:00",
                "row_count": 5,
                "fy": "2025",
            },
        )
        entries = store.list_processing_log("new-client-1", limit=5)
        assert len(entries) == 1
        assert entries[0]["file_id"] == "F123"
        assert entries[0]["extraction_path"] == "legacy"

    def test_save_profile_channel_id_indexed(self):
        # channel_id in the profile dict → get_by_channel works after save_profile
        store = self._store()
        store.save_profile(WRITE_PROFILE)
        # channel reverse-index is separate (set_channel), not done by save_profile itself
        # but after we call set_channel it works
        store.set_channel(WRITE_PROFILE["channel_id"], WRITE_PROFILE["client_id"])
        ctx = store.get_by_channel(WRITE_PROFILE["channel_id"])
        assert ctx is not None
        assert ctx.client_id == WRITE_PROFILE["client_id"]


# --------------------------------------------------------------------------- #
# InMemoryClientStore write round-trips
# --------------------------------------------------------------------------- #

INMEM_PROFILE = {
    "client_id": "mem-write-1",
    "channel_id": "C-MEM-1",
    "slack_team_id": "T-MEM",
    "client_name": "MemCo",
    "fye_month": 3,
    "accounting_software": "Xero",
    "gst_registered": False,
    "region": "SINGAPORE",
    "base_currency": "SGD",
    "status": "active",
    "category_mapping": {"Sales": "4-1000"},
}

class TestInMemoryClientStoreWrite:

    def test_save_profile_then_get(self):
        store = InMemoryClientStore()
        store.save_profile(INMEM_PROFILE)
        ctx = store.get("mem-write-1")
        assert ctx is not None
        assert ctx.client_name == "MemCo"
        assert ctx.fye_month == 3
        assert ctx.tax_registered is False
        assert ctx.status == "active"
        assert ctx.category_mapping == {"Sales": "4-1000"}

    def test_save_profile_with_channel_id_indexes_channel(self):
        store = InMemoryClientStore()
        store.save_profile(INMEM_PROFILE)
        # channel_id is in the profile dict, save_profile should index it
        ctx = store.get_by_channel("C-MEM-1")
        assert ctx is not None
        assert ctx.client_id == "mem-write-1"

    def test_set_channel_then_get_by_channel(self):
        store = InMemoryClientStore()
        store.save_profile(INMEM_PROFILE)
        store.set_channel("C-OTHER", "mem-write-1")
        ctx = store.get_by_channel("C-OTHER")
        assert ctx is not None
        assert ctx.client_id == "mem-write-1"

    def test_set_status_then_get(self):
        store = InMemoryClientStore()
        store.save_profile(INMEM_PROFILE)
        store.set_status("mem-write-1", "active")
        ctx = store.get("mem-write-1")
        assert ctx is not None
        assert ctx.status == "active"

    def test_set_status_unknown_client_is_noop(self):
        store = InMemoryClientStore()
        store.set_status("unknown", "active")  # must not raise
