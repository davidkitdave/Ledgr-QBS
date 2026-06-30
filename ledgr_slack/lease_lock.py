"""``FirestoreLeaseLock`` — cross-instance advisory lock for the ledger write path.

WS5b. The ledger read-modify-write critical section (download Slack workbook →
mutate → re-upload → update the Firestore pointer) is serialized **within a
single process** by an in-process ``threading.Lock`` keyed on ``(client_id, fy)``
(WS5a). That is not enough on multi-instance Cloud Run: two instances each hold
their own in-process lock and can still clobber the same FY workbook.

This module adds an **advisory, TTL-based lease lock** backed by a dedicated
Firestore lock doc, acquired via a ``@firestore.transactional`` compare-and-set
with retry/backoff and a LOUD timeout. The lease is taken *inside* the in-process
lock at each ledger_store write call site, so the two locks compose:

    with self._lock_for(client_id, fy):        # fast same-process serialize
        token = self._lease.acquire(client_id, fy)   # cross-instance serialize
        try:
            ... critical section ...
        finally:
            self._lease.release(client_id, fy, token)

Lock doc path
-------------
``clients/{client_id}/ledger_locks/{fy}`` — a new ``ledger_locks`` subcollection
parallel to ``ledgers``. Fields: ``holder`` (``"{instance_id}:{uuid4}"``, minted
per acquire) and ``lease_seconds`` (stored for audit).

CLOCK-SAFETY
------------
``Transaction.read_time`` is NOT available in google-cloud-firestore 2.27.0.
Instead we use the **DocumentSnapshot server timestamps**, which ARE available:
``snap.update_time`` (server time the lease doc was last written, i.e. when the
current holder acquired it) and ``snap.read_time`` (server time of this read,
i.e. "now"). The staleness predicate is::

    (snap.read_time - snap.update_time).total_seconds() > lease_seconds

Both timestamps are server-sourced, so clock skew across instances is irrelevant
— no ``SERVER_TIMESTAMP`` write and no probe write are needed for the staleness
math. If either timestamp is missing (malformed doc) the lease is treated as
stale (reclaimable).

Failure modes
-------------
- **Acquire timeout** → raise :class:`LeaseAcquireTimeout` (loud). The caller must
  see it; the document is retried later, never silently dropped.
- **Firestore error during acquire** → fail-closed: the error propagates and the
  critical section is NOT entered. Refusing the write is the safe choice.
- **Holder crash mid-write** → the lease goes stale after ``lease_seconds`` and the
  next waiter reclaims it via the staleness predicate; the already-uploaded work is
  idempotent via the existing ``seen_doc_keys`` dedupe.

Everything that touches real infrastructure is injectable (``firestore_ns``,
``sleep``, ``now``, RNG) so the lock is hermetically testable with no real
Firestore import and no real sleeping.
"""

from __future__ import annotations

import logging
import os
import random
import time
import uuid
from typing import Any, Callable, Optional

from ledgr_slack.config import _ns as _config_ns

logger = logging.getLogger(__name__)

#: Firestore collection holding client profiles (lock lives in a subcollection).
_CLIENTS_COLLECTION = "clients"
#: Subcollection name for the per-FY ledger lock docs (parallel to ``ledgers``).
_LEDGER_LOCKS_SUBCOLLECTION = "ledger_locks"

#: Lease duration: a holder that goes silent for longer than this is reclaimable.
DEFAULT_LEASE_SECONDS = 90
#: Hard ceiling on how long ``acquire`` blocks before raising loud.
DEFAULT_MAX_WAIT_SECONDS = 120
#: Exponential-backoff base and ceiling (seconds).
_BACKOFF_BASE = 0.25
_BACKOFF_MAX = 4.0


class LeaseAcquireTimeout(RuntimeError):
    """Raised (loud) when ``acquire`` cannot win the lease within ``max_wait_seconds``."""


def _default_firestore_ns() -> Any:
    """Lazy real-firestore namespace exposing ``transactional`` + ``SERVER_TIMESTAMP``.

    Imported inside the function so importing this module never pulls in real
    firestore — keeps tests hermetic and import cheap.
    """
    from google.cloud import firestore

    return firestore


def _ts_to_seconds(value: Any) -> Optional[float]:
    """Normalize a snapshot server timestamp to epoch seconds.

    ``DocumentSnapshot.read_time`` / ``.update_time`` may be a proto ``Timestamp``
    (has ``.timestamp()``) or a ``datetime`` (also has ``.timestamp()``); handle
    both. Returns ``None`` when the value is missing or cannot be normalized.
    """
    if value is None:
        return None
    ts = getattr(value, "timestamp", None)
    if callable(ts):
        try:
            return float(ts())
        except Exception:  # pragma: no cover - defensive
            return None
    # Bare numeric (e.g. an already-normalized injected clock value).
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class FirestoreLeaseLock:
    """Advisory TTL lease over ``clients/{client_id}/ledger_locks/{fy}``."""

    def __init__(
        self,
        db: Any,
        *,
        instance_id: Optional[str] = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
        firestore_ns: Optional[Any] = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
        rng: Optional[random.Random] = None,
    ) -> None:
        self._db = db
        self._instance_id = instance_id or os.environ.get("K_REVISION", "local")
        self._lease_seconds = int(lease_seconds)
        self._max_wait_seconds = float(max_wait_seconds)
        # Lazy + injectable firestore namespace (exposes transactional / SERVER_TIMESTAMP).
        self._firestore_ns = firestore_ns
        self._sleep = sleep
        self._now = now
        self._rng = rng or random.Random()

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _ns(self) -> Any:
        if self._firestore_ns is None:
            self._firestore_ns = _default_firestore_ns()
        return self._firestore_ns

    def _lock_ref(self, client_id: str, fy: str) -> Any:
        return (
            self._db.collection(_config_ns(_CLIENTS_COLLECTION))
            .document(client_id)
            .collection(_LEDGER_LOCKS_SUBCOLLECTION)
            .document(str(fy))
        )

    def _is_stale(self, snap: Any) -> bool:
        """True when the lease doc is reclaimable (expired or malformed)."""
        update_s = _ts_to_seconds(getattr(snap, "update_time", None))
        read_s = _ts_to_seconds(getattr(snap, "read_time", None))
        if update_s is None or read_s is None:
            # Malformed server timestamps → treat as stale (reclaimable).
            return True
        return (read_s - update_s) > self._lease_seconds

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with jitter: ``min(max, base*2**a) * rand(0.5,1.0)``.

        The exponent is clamped before the shift so a long wait loop can never
        overflow ``2 ** attempt`` (the ceiling makes larger exponents irrelevant).
        """
        # base * 2**exp reaches _BACKOFF_MAX well before this; clamp to stay finite.
        exp = min(attempt, 32)
        capped = min(_BACKOFF_MAX, _BACKOFF_BASE * (2 ** exp))
        return capped * self._rng.uniform(0.5, 1.0)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def acquire(self, client_id: str, fy: str) -> str:
        """Win the lease for ``(client_id, fy)`` or raise loud on timeout.

        Loops a ``@firestore.transactional`` compare-and-set until it wins or the
        wait deadline elapses. The token (``"{instance_id}:{uuid4}"``) is minted
        per acquire and returned on success; pass it to :meth:`release`.

        Raises:
            LeaseAcquireTimeout: when the lease cannot be won within
                ``max_wait_seconds`` (loud — never silently dropped).
            Exception: any Firestore error propagates (fail-closed — the caller
                must NOT enter the critical section).
        """
        ns = self._ns()
        ref = self._lock_ref(client_id, fy)
        token = f"{self._instance_id}:{uuid.uuid4()}"
        deadline = self._now() + self._max_wait_seconds

        @ns.transactional
        def _attempt(txn: Any) -> bool:
            snap = ref.get(transaction=txn)
            if getattr(snap, "exists", False) and not self._is_stale(snap):
                return False  # someone else holds a live lease
            txn.set(
                ref,
                {"holder": token, "lease_seconds": self._lease_seconds},
            )
            return True

        attempt = 0
        while True:
            # Firestore errors propagate here → fail-closed (do not enter section).
            won = _attempt(self._db.transaction())
            if won:
                logger.debug(
                    "lease acquired client=%s fy=%s holder=%s", client_id, fy, token
                )
                return token

            if self._now() >= deadline:
                raise LeaseAcquireTimeout(
                    f"could not acquire ledger lease for client={client_id!r} "
                    f"fy={fy!r} within {self._max_wait_seconds}s "
                    f"(instance={self._instance_id!r}); refusing to write"
                )

            self._sleep(self._backoff(attempt))
            attempt += 1

    def release(self, client_id: str, fy: str, token: str) -> None:
        """Release the lease iff ``token`` still holds it (holder-scoped).

        A no-op when the lease was already reclaimed by another instance — the
        holder-token guard prevents a revived old holder from deleting the new
        holder's lease.
        """
        ns = self._ns()
        ref = self._lock_ref(client_id, fy)

        @ns.transactional
        def _attempt(txn: Any) -> None:
            snap = ref.get(transaction=txn)
            data = snap.to_dict() if getattr(snap, "exists", False) else None
            if data and data.get("holder") == token:
                txn.delete(ref)

        _attempt(self._db.transaction())
