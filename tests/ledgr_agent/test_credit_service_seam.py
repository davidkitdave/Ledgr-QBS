"""Tests for ledgr_agent.billing gate seam."""

from __future__ import annotations

import pytest

import ledgr_agent.billing as billing
from ledgr_agent.billing import credit_gate_decision


@pytest.fixture(autouse=True)
def _reset_shared_service() -> None:
    original = billing._shared_credit_service
    billing._shared_credit_service = None
    try:
        yield
    finally:
        billing._shared_credit_service = original


def test_default_gate_blocks_unknown_firm_with_zero_balance() -> None:
    decision = credit_gate_decision(firm_id="unknown-firm", required_units=1)
    assert decision["allowed"] is False
    assert decision["reason"] == "zero_credit"
    assert decision["balance"] == 0


def test_default_gate_skipped_when_firm_id_is_none() -> None:
    decision = credit_gate_decision(firm_id=None, required_units=1)
    assert decision == {"allowed": True, "reason": "ok", "balance": 0}


def test_monkeypatching_shared_service_changes_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    class _StubService:
        def __init__(self, balance: int) -> None:
            self._balance = balance
            self.read_calls: list[str] = []

        def read_balance(self, firm_id: str) -> int:
            self.read_calls.append(firm_id)
            return self._balance

    stub = _StubService(balance=0)
    billing.configure_shared_credit_service(stub)  # type: ignore[arg-type]

    decision = credit_gate_decision(firm_id="firm-1", required_units=2)

    assert stub.read_calls == ["firm-1"]
    assert decision["allowed"] is False
    assert decision["reason"] == "zero_credit"


def test_positive_balance_allows() -> None:
    class _StubService:
        def read_balance(self, firm_id: str) -> int:
            return 5

    billing.configure_shared_credit_service(_StubService())  # type: ignore[arg-type]
    decision = credit_gate_decision(firm_id="firm-with-credits", required_units=1)
    assert decision["allowed"] is True
    assert decision["reason"] == "ok"


def test_gate_blocks_when_required_units_exceed_balance() -> None:
    class _StubService:
        def read_balance(self, firm_id: str) -> int:
            return 2

    billing.configure_shared_credit_service(_StubService())  # type: ignore[arg-type]
    decision = credit_gate_decision(firm_id="firm-with-some-credits", required_units=5)
    assert decision["allowed"] is False
    assert decision["reason"] == "insufficient_credit"
    assert decision["required_units"] == 5
