"""Hermetic CreditService for the Ledgr Slack credit system (plan task #5.1).

This is Slice 1 of plan 5: a pure-Python credit ledger with a pluggable
``CreditStore`` seam. Tests use ``InMemoryCreditStore``; a future slice will
add a Firestore-backed store that satisfies the same protocol.

See ``docs/superpowers/plans/2026-06-20-slack-credit-system.md`` and
ADR-0016 for the authoritative design. The store interface kept narrow on
purpose — only the operations the rest of the system needs are exposed.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Protocol, Set, Tuple

logger = logging.getLogger(__name__)


class CreditStore(Protocol):
    """Backend seam for the credit ledger."""

    def ensure_firm(self, firm_id: str) -> None: ...
    def read_balance(self, firm_id: str) -> int: ...
    def apply_grant(self, firm_id: str, amount: int, note: str) -> int: ...
    def apply_deduct(
        self, firm_id: str, amount: int, reason: str, idempotency_key: str
    ) -> int: ...


class InMemoryCreditStore:
    """Pure-Python store used by tests and the admin CLI shim.

    Dedup of ``apply_deduct`` is keyed by ``(firm_id, idempotency_key)`` so a
    retry of the same logical charge returns the same balance without
    double-spending credits.
    """

    def __init__(self) -> None:
        self._balances: Dict[str, int] = {}
        self._seen_deducts: Set[Tuple[str, str]] = set()
        self._firms: Set[str] = set()

    def ensure_firm(self, firm_id: str) -> None:
        self._firms.add(firm_id)
        self._balances.setdefault(firm_id, 0)

    def read_balance(self, firm_id: str) -> int:
        return self._balances.get(firm_id, 0)

    def apply_grant(self, firm_id: str, amount: int, note: str) -> int:
        self._balances[firm_id] = self._balances.get(firm_id, 0) + amount
        return self._balances[firm_id]

    def apply_deduct(
        self, firm_id: str, amount: int, reason: str, idempotency_key: str
    ) -> int:
        key = (firm_id, idempotency_key)
        if key in self._seen_deducts:
            return self._balances[firm_id]
        self._seen_deducts.add(key)
        self._balances[firm_id] = self._balances.get(firm_id, 0) - amount
        return self._balances[firm_id]

    def known_firms(self) -> list[str]:
        return sorted(self._firms)


#: Top-level collection holding per-firm credit balance docs.  Namespaced via
#: ``accounting_agents.config._ns`` so dev/prod can share a GCP project (ADR-0022).
_CREDIT_FIRMS_COLLECTION = "credit_firms"
#: Subcollection of idempotency markers; one doc per ``idempotency_key`` charged.
_DEDUCTS_SUBCOLLECTION = "deducts"


def _ns(name: str) -> str:
    """Namespace a top-level collection name (mirrors ``accounting_agents.config._ns``).

    Imported lazily so ``app.credit_service`` stays importable without the
    ``accounting_agents`` package on the path (keeps this module hermetic for
    the pure in-memory tests).  Falls back to the local env read if the import
    is unavailable.
    """
    try:
        from accounting_agents.config import _ns as _config_ns

        return _config_ns(name)
    except Exception:  # noqa: BLE001 — fall back to the raw env contract
        prefix = os.environ.get("LEDGR_FIRESTORE_NAMESPACE", "").strip()
        return f"{prefix}_{name}" if prefix else name


class FirestoreCreditStore:
    """Durable, atomic, idempotent :class:`CreditStore` backed by Firestore.

    Storage layout (top-level collection namespaced via ``_ns``)::

        {_ns}/credit_firms/{firm_id}                  -> {"balance": int}
        {_ns}/credit_firms/{firm_id}/deducts/{idem}   -> {"amount": int, "reason": str}

    The deduct path is the billing-correctness critical section: it runs inside a
    single ``@firestore.transactional`` that (a) returns the unchanged balance if
    the idempotency marker already exists (no double-spend) and otherwise (b)
    writes the marker AND decrements the balance in the *same* transaction, so two
    concurrent Cloud Run instances charging the same Slack file can never both
    win.  ``apply_grant`` is likewise a transactional read-modify-write.

    ``Transaction.read_time`` is unavailable in google-cloud-firestore 2.27.x, so
    we use the standard ``@firestore.transactional`` decorator (no ``read_time``
    math; the lease lock's staleness trick is not needed here).

    The firestore client + namespace are lazy-imported inside the class so merely
    importing this module never pulls in the firestore dep (tests stay hermetic).
    A ``client=`` injection seam mirrors ``FirestoreClientStore`` /
    ``InstallationStore``: pass a fake db (and ``firestore_ns``) to test without
    touching GCP.
    """

    def __init__(
        self,
        *,
        client: Any = None,
        firestore_ns: Optional[Any] = None,
        collection: Optional[str] = None,
    ) -> None:
        # Test seam: when injected, ``_db()`` returns this directly (no network).
        self._injected_client = client
        self._client: Any = None
        self._firestore_ns = firestore_ns
        self._collection = collection or _ns(_CREDIT_FIRMS_COLLECTION)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _db(self) -> Any:
        if self._injected_client is not None:
            return self._injected_client
        if self._client is None:
            from google.cloud import firestore  # lazy — never loaded in tests

            self._client = firestore.Client()
        return self._client

    def _ns_module(self) -> Any:
        """Firestore namespace exposing ``transactional`` (lazy + injectable)."""
        if self._firestore_ns is None:
            from google.cloud import firestore

            self._firestore_ns = firestore
        return self._firestore_ns

    def _firm_ref(self, firm_id: str) -> Any:
        return self._db().collection(self._collection).document(str(firm_id))

    def _deduct_ref(self, firm_id: str, idempotency_key: str) -> Any:
        return (
            self._firm_ref(firm_id)
            .collection(_DEDUCTS_SUBCOLLECTION)
            .document(str(idempotency_key))
        )

    @staticmethod
    def _balance_of(snap: Any) -> int:
        if not getattr(snap, "exists", False):
            return 0
        data = snap.to_dict() or {}
        try:
            return int(data.get("balance", 0) or 0)
        except (TypeError, ValueError):
            return 0

    # ------------------------------------------------------------------ #
    # CreditStore protocol
    # ------------------------------------------------------------------ #

    def ensure_firm(self, firm_id: str) -> None:
        """Create the firm doc with ``balance=0`` if absent (idempotent)."""
        ref = self._firm_ref(firm_id)
        snap = ref.get()
        if not getattr(snap, "exists", False):
            ref.set({"balance": 0}, merge=True)

    def read_balance(self, firm_id: str) -> int:
        """Current balance, or 0 for unknown firms.

        Defensive: never raises out to the gate.  A Firestore read failure here
        must not crash document intake, so we log and return 0 (the gate then
        treats the firm as out-of-credit, which is the safe default).
        """
        try:
            return self._balance_of(self._firm_ref(firm_id).get())
        except Exception:  # noqa: BLE001 — read failure must not crash the gate
            logger.warning(
                "FirestoreCreditStore.read_balance failed firm=%s (treating as 0)",
                firm_id,
                exc_info=True,
            )
            return 0

    def apply_grant(self, firm_id: str, amount: int, note: str) -> int:
        """Transactional read-modify-write that increments the firm balance.

        Errors propagate (billing correctness): a failed grant must be visible,
        not silently swallowed.
        """
        ns = self._ns_module()
        ref = self._firm_ref(firm_id)
        delta = int(amount)

        @ns.transactional
        def _txn(txn: Any) -> int:
            snap = ref.get(transaction=txn)
            new_balance = self._balance_of(snap) + delta
            txn.set(ref, {"balance": new_balance}, merge=True)
            return new_balance

        return int(_txn(self._db().transaction()))

    def apply_deduct(
        self, firm_id: str, amount: int, reason: str, idempotency_key: str
    ) -> int:
        """Atomic, idempotent deduct under concurrent Cloud Run instances.

        Single transaction:
          1. If the ``idempotency_key`` marker already exists → return the current
             balance unchanged (the charge already happened; no double-spend).
          2. Otherwise write the marker AND decrement the balance in the SAME
             transaction, so a concurrent retry on another instance sees the
             marker and (1) applies.

        Errors propagate (billing correctness).
        """
        ns = self._ns_module()
        firm_ref = self._firm_ref(firm_id)
        marker_ref = self._deduct_ref(firm_id, idempotency_key)
        delta = int(amount)
        reason_str = str(reason)

        @ns.transactional
        def _txn(txn: Any) -> int:
            firm_snap = firm_ref.get(transaction=txn)
            current = self._balance_of(firm_snap)
            marker_snap = marker_ref.get(transaction=txn)
            if getattr(marker_snap, "exists", False):
                # Already charged this idempotency_key → no double-spend.
                return current
            new_balance = current - delta
            txn.set(firm_ref, {"balance": new_balance}, merge=True)
            txn.set(marker_ref, {"amount": delta, "reason": reason_str})
            return new_balance

        return int(_txn(self._db().transaction()))


class CreditService:
    """Thin façade over a :class:`CreditStore` for the rest of the system."""

    def __init__(self, store: CreditStore) -> None:
        self._store = store

    def ensure_firm(self, firm_id: str) -> None:
        self._store.ensure_firm(firm_id)

    def grant(self, firm_id: str, amount: int, note: str = "") -> int:
        return self._store.apply_grant(firm_id, amount, note)

    def deduct(self, firm_id: str, amount: int, reason: str, idempotency_key: str) -> int:
        return self._store.apply_deduct(firm_id, amount, reason, idempotency_key)

    def read_balance(self, firm_id: str) -> int:
        return self._store.read_balance(firm_id)


_shared_credit_service: CreditService | None = None


def get_shared_credit_service() -> CreditService:
    """Process-wide credit ledger used by Slack delivery and the admin CLI."""

    global _shared_credit_service
    if _shared_credit_service is None:
        _shared_credit_service = CreditService(InMemoryCreditStore())
    return _shared_credit_service


def configure_shared_credit_service(service: CreditService) -> None:
    """Replace the shared service (tests or a future Firestore-backed store)."""

    global _shared_credit_service
    _shared_credit_service = service
