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
    def __init__(self, doc_id: str, data: Optional[dict], reference: "_DocRef"):
        self.id = doc_id
        self._data = data
        self.reference = reference

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
    def __init__(self, store: dict, path: tuple[str, ...]):
        self._store = store
        self._path = path  # (col, doc, col, ... , col)

    def document(self, doc_id: str) -> "_DocRef":
        return _DocRef(self._store, self._path + (doc_id,))

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
    def __init__(self, store: dict, path: tuple[str, ...]):
        self._store = store
        self._path = path  # (col, doc, col, doc, ...) ending in a doc-id

    @property
    def id(self) -> str:
        return self._path[-1]

    def collection(self, name: str) -> _CollectionRef:
        return _CollectionRef(self._store, self._path + (name,))

    def get(self) -> _Snapshot:
        return _Snapshot(self._path[-1], self._store.get(self._path), self)

    def set(self, data: dict, merge: bool = False) -> None:
        if merge and self._path in self._store:
            self._store[self._path] = {**self._store[self._path], **data}
        else:
            self._store[self._path] = dict(data)

    def delete(self) -> None:
        self._store.pop(self._path, None)


class FakeFirestore:
    """Dict-backed Firestore client compatible with the session/hitl call shapes."""

    def __init__(self) -> None:
        # Maps full doc path tuple -> data dict. Shared across all refs.
        self._store: dict[tuple[str, ...], dict] = {}

    def collection(self, name: str) -> _CollectionRef:
        return _CollectionRef(self._store, (name,))
