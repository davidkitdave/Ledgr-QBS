"""A hermetic, dict-backed Firestore stand-in for the HITL / sessions tests.

Supports exactly the call shapes the session service + hitl helpers make:

  db.collection(a).document(b).collection(c).document(d).set(data, merge=)
  ...                                  .get() -> snapshot(.exists, .to_dict())
  ...                                  .collection(e).document(f).set(...)
  ...                                  .collection(e).order_by("seq").stream()
  ...                                  .collection(e).stream()  (-> .reference.delete())
  db.collection(a).document(b).collection("users").list_documents()

Every reference is keyed by its full path inside a single shared ``store`` dict,
so independent references to the same path see each other's writes (mirroring
real Firestore and unlike per-object in-memory copies).
"""

from __future__ import annotations

from typing import Iterator, Optional


class _Snapshot:
    def __init__(
        self,
        doc_id: str,
        data: Optional[dict],
        reference: "_DocRef",
        *,
        update_time: Optional[float] = None,
        read_time: Optional[float] = None,
    ):
        self.id = doc_id
        self._data = data
        self.reference = reference
        # Server-sourced timestamps (epoch seconds). The lease lock's staleness
        # math reads ``update_time`` (when the doc was last written) and
        # ``read_time`` ("now"); both come from the same fake server clock so
        # cross-instance skew is irrelevant — mirroring real Firestore.
        self.update_time = update_time
        self.read_time = read_time

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> Optional[dict]:
        return dict(self._data) if self._data is not None else None


class _Query:
    def __init__(self, snapshots: list[_Snapshot]):
        self._snapshots = snapshots

    def order_by(self, field: str) -> "_Query":
        return _Query(
            sorted(self._snapshots, key=lambda s: (s.to_dict() or {}).get(field))
        )

    def stream(self) -> Iterator[_Snapshot]:
        return iter(list(self._snapshots))


class _CollectionRef:
    def __init__(
        self, store: dict, path: tuple[str, ...], clock: Optional["_ServerClock"] = None
    ):
        self._store = store
        self._path = path  # (col, doc, col, ... , col)
        self._clock = clock

    def document(self, doc_id: str) -> "_DocRef":
        return _DocRef(self._store, self._path + (doc_id,), self._clock)

    def _child_doc_ids(self) -> list[str]:
        # Direct child doc-ids only (paths exactly one doc deeper than this col).
        ids: list[str] = []
        plen = len(self._path)
        for key in self._store:
            if len(key) == plen + 1 and key[:plen] == self._path:
                ids.append(key[plen])
        return ids

    def _all_descendant_doc_ids(self) -> list[str]:
        # Like real Firestore list_documents(): surfaces direct child doc-ids
        # even when the doc itself holds no data but has descendants.
        ids: list[str] = []
        plen = len(self._path)
        for key in self._store:
            if len(key) > plen and key[:plen] == self._path:
                doc_id = key[plen]
                if doc_id not in ids:
                    ids.append(doc_id)
        return ids

    def list_documents(self) -> list["_DocRef"]:
        return [self.document(doc_id) for doc_id in self._all_descendant_doc_ids()]

    def order_by(self, field: str) -> _Query:
        return _Query(self._snapshots()).order_by(field)

    def stream(self) -> Iterator[_Snapshot]:
        return _Query(self._snapshots()).stream()

    def _snapshots(self) -> list[_Snapshot]:
        snaps: list[_Snapshot] = []
        for doc_id in self._child_doc_ids():
            ref = self.document(doc_id)
            snaps.append(_Snapshot(doc_id, self._store.get(ref._path), ref))
        return snaps


class _DocRef:
    def __init__(
        self, store: dict, path: tuple[str, ...], clock: Optional["_ServerClock"] = None
    ):
        self._store = store
        self._path = path  # (col, doc, col, doc, ...) ending in a doc-id
        self._clock = clock

    @property
    def id(self) -> str:
        return self._path[-1]

    def collection(self, name: str) -> _CollectionRef:
        return _CollectionRef(self._store, self._path + (name,), self._clock)

    def get(self, transaction: object = None) -> _Snapshot:
        # ``transaction`` is accepted (and ignored) so the lease lock's
        # ``ref.get(transaction=txn)`` shape works. Server timestamps come from
        # the injectable clock: ``update_time`` = when this doc was last written;
        # ``read_time`` = "now" (advances every read).
        update_time = None
        read_time = None
        if self._clock is not None:
            update_time = self._clock.write_times.get(self._path)
            read_time = self._clock.now()
        return _Snapshot(
            self._path[-1],
            self._store.get(self._path),
            self,
            update_time=update_time,
            read_time=read_time,
        )

    def set(self, data: dict, merge: bool = False) -> None:
        if merge and self._path in self._store:
            self._store[self._path] = {**self._store[self._path], **data}
        else:
            self._store[self._path] = dict(data)
        if self._clock is not None:
            self._clock.write_times[self._path] = self._clock.now()

    def delete(self) -> None:
        self._store.pop(self._path, None)
        if self._clock is not None:
            self._clock.write_times.pop(self._path, None)


class _ServerClock:
    """Injectable fake "server clock" backing snapshot read_time / update_time.

    ``now()`` returns the current fake server time (epoch seconds). ``advance(n)``
    moves it forward so tests can age a lease past its TTL. ``write_times`` records
    the server time each doc path was last written, mirroring Firestore's
    ``DocumentSnapshot.update_time``.
    """

    def __init__(self, start: float = 1_000_000.0):
        self._t = float(start)
        self.write_times: dict[tuple[str, ...], float] = {}

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += float(seconds)


class _FakeTransaction:
    """Stand-in for a Firestore transaction.

    The lease lock calls ``ref.set`` / ``ref.delete`` through the transaction
    object (``txn.set(ref, data)`` / ``txn.delete(ref)``); both apply immediately
    to the shared store (no real isolation needed for the hermetic tests). A
    test can set ``raises=True`` to simulate Firestore being unavailable so the
    fail-closed path can be exercised.
    """

    def __init__(self, raises: bool = False):
        self._raises = raises

    def set(self, ref: "_DocRef", data: dict, merge: bool = False) -> None:
        if self._raises:
            raise RuntimeError("fake firestore unavailable")
        ref.set(data, merge=merge)

    def delete(self, ref: "_DocRef") -> None:
        if self._raises:
            raise RuntimeError("fake firestore unavailable")
        ref.delete()


def fake_transactional(fn):
    """Passthrough shim for ``@firestore.transactional``.

    Real firestore wraps the function in retry/commit machinery; here we just
    call it with whatever transaction object is passed, which is all the lease
    lock needs.
    """

    def _wrapped(transaction, *args, **kwargs):
        return fn(transaction, *args, **kwargs)

    return _wrapped


class FakeFirestoreNs:
    """Stand-in for the ``google.cloud.firestore`` namespace the lease lock uses."""

    SERVER_TIMESTAMP = object()
    transactional = staticmethod(fake_transactional)


class FakeFirestore:
    """Dict-backed Firestore client compatible with the session/hitl call shapes.

    Extended for WS5b: carries a ``_ServerClock`` so ``get(transaction=...)``
    snapshots expose ``read_time``/``update_time`` (driven by an injectable fake
    server clock), and exposes ``transaction(raises=...)`` for the lease lock.
    """

    def __init__(self, clock: Optional[_ServerClock] = None) -> None:
        # Maps full doc path tuple -> data dict. Shared across all refs.
        self._store: dict[tuple[str, ...], dict] = {}
        # Server clock backing snapshot timestamps (created lazily if absent).
        self.clock = clock if clock is not None else _ServerClock()
        # When True, transactions raise (fail-closed test).
        self.transaction_raises = False
        # Fake firestore namespace so a default FirestoreLeaseLock built over this
        # fake db uses the fake ``transactional`` shim — keeping every existing
        # SlackLedgerStore(FakeFirestore()) test hermetic (no real firestore txn).
        self.firestore_ns = FakeFirestoreNs()

    def collection(self, name: str) -> _CollectionRef:
        return _CollectionRef(self._store, (name,), self.clock)

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(raises=self.transaction_raises)
