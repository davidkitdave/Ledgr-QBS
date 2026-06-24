"""Hermetic CreditService for the Ledgr Slack credit system (plan task #5.1).

This is Slice 1 of plan 5: a pure-Python credit ledger with a pluggable
``CreditStore`` seam. Tests use ``InMemoryCreditStore``; a future slice will
add a Firestore-backed store that satisfies the same protocol.

See ``docs/superpowers/plans/2026-06-20-slack-credit-system.md`` and
ADR-0016 for the authoritative design. The store interface kept narrow on
purpose — only the operations the rest of the system needs are exposed.
"""

from __future__ import annotations

from typing import Dict, Protocol, Set, Tuple


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
