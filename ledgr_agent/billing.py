"""Credit ledger, gate, and charge for ledgr_agent (ADR-0016).

Owns the CreditService, in-memory and Firestore stores, pre-flight gate,
post-delivery charge, and dev playground seeding. No dependency on
accounting_agents or invoice_processing.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Protocol, Set, Tuple

from ledgr_agent.internal.schemas import CreditSummary

logger = logging.getLogger(__name__)

_TRUTHY = frozenset({"1", "true", "yes", "on"})


class CreditStore(Protocol):
    """Backend seam for the credit ledger."""

    def ensure_firm(self, firm_id: str) -> None: ...
    def read_balance(self, firm_id: str) -> int: ...
    def apply_grant(self, firm_id: str, amount: int, note: str) -> int: ...
    def apply_deduct(
        self, firm_id: str, amount: int, reason: str, idempotency_key: str,
        *, channel_id: str | None = None,
    ) -> int: ...
    def apply_dev_seed_if_unseeded(
        self, firm_id: str, amount: int, note: str,
    ) -> tuple[int, bool]: ...


class InMemoryCreditStore:
    """Pure-Python store used by tests and dev playground."""

    def __init__(self) -> None:
        self._balances: Dict[str, int] = {}
        self._seen_deducts: Set[Tuple[str, str]] = set()
        self._firms: Set[str] = set()
        self._dev_seeded: Set[str] = set()

    def ensure_firm(self, firm_id: str) -> None:
        self._firms.add(firm_id)
        self._balances.setdefault(firm_id, 0)

    def read_balance(self, firm_id: str) -> int:
        return self._balances.get(firm_id, 0)

    def apply_grant(self, firm_id: str, amount: int, note: str) -> int:
        self._balances[firm_id] = self._balances.get(firm_id, 0) + amount
        return self._balances[firm_id]

    def apply_dev_seed_if_unseeded(
        self, firm_id: str, amount: int, note: str,
    ) -> tuple[int, bool]:
        self.ensure_firm(firm_id)
        if firm_id in self._dev_seeded:
            return self.read_balance(firm_id), False
        balance = self.read_balance(firm_id)
        if balance > 0:
            self._dev_seeded.add(firm_id)
            return balance, False
        self._dev_seeded.add(firm_id)
        if int(amount) <= 0:
            return balance, False
        return self.apply_grant(firm_id, amount, note), True

    def apply_deduct(
        self, firm_id: str, amount: int, reason: str, idempotency_key: str,
        *, channel_id: str | None = None,
    ) -> int:
        key = (firm_id, idempotency_key)
        if key in self._seen_deducts:
            return self._balances[firm_id]
        self._seen_deducts.add(key)
        self._balances[firm_id] = self._balances.get(firm_id, 0) - amount
        return self._balances[firm_id]

    def known_firms(self) -> list[str]:
        return sorted(self._firms)


_CREDIT_FIRMS_COLLECTION = "credit_firms"
_DEDUCTS_SUBCOLLECTION = "deducts"


def _ns(name: str) -> str:
    prefix = os.environ.get("LEDGR_FIRESTORE_NAMESPACE", "").strip()
    return f"{prefix}_{name}" if prefix else name


class FirestoreCreditStore:
    """Durable, atomic, idempotent CreditStore backed by Firestore."""

    def __init__(
        self,
        *,
        client: Any = None,
        firestore_ns: Optional[Any] = None,
        collection: Optional[str] = None,
    ) -> None:
        self._injected_client = client
        self._client: Any = None
        self._firestore_ns = firestore_ns
        self._collection = collection or _ns(_CREDIT_FIRMS_COLLECTION)

    def _db(self) -> Any:
        if self._injected_client is not None:
            return self._injected_client
        if self._client is None:
            from google.cloud import firestore

            self._client = firestore.Client()
        return self._client

    def _ns_module(self) -> Any:
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

    def ensure_firm(self, firm_id: str) -> None:
        ref = self._firm_ref(firm_id)
        snap = ref.get()
        if not getattr(snap, "exists", False):
            ref.set({"balance": 0}, merge=True)

    def read_balance(self, firm_id: str) -> int:
        try:
            return self._balance_of(self._firm_ref(firm_id).get())
        except Exception:  # noqa: BLE001
            logger.warning(
                "FirestoreCreditStore.read_balance failed firm=%s (treating as 0)",
                firm_id,
                exc_info=True,
            )
            return 0

    def apply_grant(self, firm_id: str, amount: int, note: str) -> int:
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

    def apply_dev_seed_if_unseeded(
        self, firm_id: str, amount: int, note: str,
    ) -> tuple[int, bool]:
        ns = self._ns_module()
        ref = self._firm_ref(firm_id)
        delta = int(amount)

        @ns.transactional
        def _txn(txn: Any) -> tuple[int, bool]:
            snap = ref.get(transaction=txn)
            data = snap.to_dict() or {}
            current = self._balance_of(snap)
            if data.get("dev_credit_seeded"):
                return current, False
            if current > 0:
                txn.set(ref, {"dev_credit_seeded": True}, merge=True)
                return current, False
            if delta <= 0:
                txn.set(ref, {"dev_credit_seeded": True}, merge=True)
                return current, False
            new_balance = current + delta
            txn.set(
                ref,
                {"balance": new_balance, "dev_credit_seeded": True},
                merge=True,
            )
            return new_balance, True

        return _txn(self._db().transaction())

    def apply_deduct(
        self, firm_id: str, amount: int, reason: str, idempotency_key: str,
        *, channel_id: str | None = None,
    ) -> int:
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
                return current
            new_balance = current - delta
            txn.set(firm_ref, {"balance": new_balance}, merge=True)
            marker: dict[str, Any] = {
                "amount": delta,
                "reason": reason_str,
                "at": datetime.now(timezone.utc).isoformat(),
            }
            if channel_id:
                marker["channel_id"] = str(channel_id)
            txn.set(marker_ref, marker)
            return new_balance

        return int(_txn(self._db().transaction()))


class CreditService:
    """Thin façade over a CreditStore."""

    def __init__(self, store: CreditStore) -> None:
        self._store = store

    def ensure_firm(self, firm_id: str) -> None:
        self._store.ensure_firm(firm_id)

    def grant(self, firm_id: str, amount: int, note: str = "") -> int:
        return self._store.apply_grant(firm_id, amount, note)

    def dev_seed_if_unseeded(self, firm_id: str, amount: int, note: str = "") -> bool:
        """Grant dev credits once per firm; manual top-ups use ``grant()`` instead."""

        self.ensure_firm(firm_id)
        _balance, granted = self._store.apply_dev_seed_if_unseeded(firm_id, amount, note)
        return granted

    def deduct(
        self, firm_id: str, amount: int, reason: str, idempotency_key: str,
        *, channel_id: str | None = None,
    ) -> int:
        return self._store.apply_deduct(
            firm_id, amount, reason, idempotency_key, channel_id=channel_id
        )

    def read_balance(self, firm_id: str) -> int:
        return self._store.read_balance(firm_id)


_shared_credit_service: CreditService | None = None


def get_shared_credit_service() -> CreditService:
    global _shared_credit_service
    if _shared_credit_service is None:
        _shared_credit_service = CreditService(InMemoryCreditStore())
    return _shared_credit_service


def configure_shared_credit_service(service: CreditService) -> None:
    global _shared_credit_service
    _shared_credit_service = service


def _env_is_prod() -> bool:
    return (os.environ.get("LEDGR_ENV") or "dev").strip().lower() == "prod"


def _firestore_creds_present() -> bool:
    for var in ("GOOGLE_APPLICATION_CREDENTIALS", "K_SERVICE", "FIRESTORE_EMULATOR_HOST"):
        if (os.environ.get(var) or "").strip():
            return True
    return False


def configure_durable_credit_service_if_prod() -> bool:
    if not (_env_is_prod() or _firestore_creds_present()):
        return False
    try:
        configure_shared_credit_service(CreditService(FirestoreCreditStore()))
        logger.info("credit store: durable FirestoreCreditStore installed")
        return True
    except Exception:  # noqa: BLE001
        logger.error(
            "credit store: failed to install FirestoreCreditStore; "
            "falling back to in-memory",
            exc_info=True,
        )
        return False


def apply_dev_credit_grants_from_env() -> None:
    """``LEDGR_DEV_CREDIT_GRANTS=T123:50`` — seed once per firm, not on every boot."""

    raw = os.environ.get("LEDGR_DEV_CREDIT_GRANTS", "").strip()
    if not raw:
        return
    service = get_shared_credit_service()
    for part in raw.split(","):
        chunk = part.strip()
        if not chunk or ":" not in chunk:
            continue
        firm_id, amount_str = chunk.split(":", 1)
        firm_id = firm_id.strip()
        if not firm_id:
            continue
        try:
            amount = int(amount_str.strip())
        except ValueError:
            logger.warning("skip invalid LEDGR_DEV_CREDIT_GRANTS entry: %r", chunk)
            continue
        if amount <= 0:
            service.ensure_firm(firm_id)
            continue
        if service.dev_seed_if_unseeded(firm_id, amount, note="dev auto-grant"):
            logger.info("dev credit seed: firm=%s amount=%s", firm_id, amount)


def wire_playground_credits() -> None:
    """Install durable store (prod) and apply dev grants before agent runs."""

    configure_durable_credit_service_if_prod()
    apply_dev_credit_grants_from_env()


def resolve_firm_id_from_state(state: Any) -> str | None:
    if state is None:
        return None
    getter = getattr(state, "get", None)
    if getter is None:
        return None
    for key in ("firm_id", "slack_team_id"):
        val = getter(key)
        if val and str(val).strip():
            return str(val).strip()
    return None


def resolve_firm_id(tool_context: Any) -> str | None:
    if tool_context is None:
        return None
    return resolve_firm_id_from_state(getattr(tool_context, "state", None))


def billable_units(
    *,
    file_kind: str,
    page_count: int,
    document_count: int = 0,
) -> int:
    """Billable credits: bank = pages; commercial = max(pages, unique docs, 1)."""

    pages = max(int(page_count), 1)
    if file_kind == "bank_statement":
        return pages
    return max(pages, int(document_count), 1)


def estimate_units_from_bytes(data: bytes, mime: str) -> int:
    """Pre-read gate estimate from source file page count."""

    from ledgr_agent.internal.gemini import count_input_pages

    return max(count_input_pages(data, mime), 1)


def _charge_disabled() -> bool:
    raw = os.environ.get("LEDGR_DISABLE_IN_TOOL_CHARGE", "")
    return raw.strip().lower() in _TRUTHY


def credit_gate_decision(
    *,
    firm_id: str | None,
    required_units: int,
) -> dict[str, Any]:
    if not firm_id:
        return {"allowed": True, "reason": "ok", "balance": 0}

    service = get_shared_credit_service()
    required = max(int(required_units), 0)
    balance = service.read_balance(firm_id)
    allowed = balance >= required
    if allowed:
        reason = "ok"
    elif balance <= 0:
        reason = "zero_credit"
    else:
        reason = "insufficient_credit"
    return {
        "allowed": allowed,
        "reason": reason,
        "balance": balance,
        "required_units": required,
    }


def gate(
    tool_context: Any,
    *,
    units: int,
    kind: str = "document",
) -> CreditSummary | None:
    """Pre-flight credit check. Returns blocked CreditSummary or None to proceed."""

    firm_id = resolve_firm_id(tool_context)
    decision = credit_gate_decision(firm_id=firm_id, required_units=units)
    if decision.get("allowed", True):
        return None
    balance = decision.get("balance")
    return CreditSummary(
        credits_estimated=units,
        credits_used=0,
        credits_remaining=int(balance) if isinstance(balance, int) else None,
        credit_status="blocked",
    )


def charge(
    tool_context: Any,
    *,
    units: int,
    file_id: str,
    kind: str = "document",
) -> CreditSummary:
    """Deduct credits on delivery; idempotent per file_id."""

    firm_id = resolve_firm_id(tool_context)
    if not firm_id:
        return CreditSummary(
            credits_estimated=units,
            credits_used=0,
            credits_remaining=None,
            credit_status="not_billable",
        )
    if units <= 0:
        balance = get_shared_credit_service().read_balance(firm_id)
        return CreditSummary(
            credits_estimated=units,
            credits_used=0,
            credits_remaining=balance,
            credit_status="not_billable",
        )
    if _charge_disabled():
        balance = get_shared_credit_service().read_balance(firm_id)
        return CreditSummary(
            credits_estimated=units,
            credits_used=0,
            credits_remaining=balance,
            credit_status="estimated",
        )
    try:
        state = getattr(tool_context, "state", None)
        channel_id = state.get("channel_id") if isinstance(state, dict) else None
        remaining = get_shared_credit_service().deduct(
            firm_id,
            amount=units,
            reason=f"delivery:{kind}",
            idempotency_key=file_id,
            channel_id=str(channel_id) if channel_id else None,
        )
        return CreditSummary(
            credits_estimated=units,
            credits_used=units,
            credits_remaining=int(remaining),
            credit_status="charged",
            credit_ledger_refs=[file_id],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Credit deduct failed (billing skipped): %s", exc)
        balance = get_shared_credit_service().read_balance(firm_id)
        return CreditSummary(
            credits_estimated=units,
            credits_used=0,
            credits_remaining=balance,
            credit_status="not_checked",
        )


def delivery_idempotency_key(*, channel_id: str, file_id: str) -> str:
    return f"{channel_id}:{file_id}:deliver"


def read_credit_balance(tool_context: Any) -> dict[str, Any]:
    """Return the current credit balance for the active firm (workspace team id)."""

    state = getattr(tool_context, "state", None)
    if state is None:
        return {
            "status": "error",
            "message": "no session state — run inside ADK web or agents-cli",
        }

    firm_id = resolve_firm_id_from_state(state)
    if not firm_id:
        return {
            "status": "error",
            "message": (
                "no firm_id in session. Set LEDGR_PLAYGROUND_FIRM_ID or add "
                "firm_id to playground_profile.json"
            ),
        }

    service = get_shared_credit_service()
    balance = int(service.read_balance(firm_id))
    return {
        "status": "success",
        "firm_id": firm_id,
        "balance": balance,
        "message": f"Balance: {balance} credits",
    }
