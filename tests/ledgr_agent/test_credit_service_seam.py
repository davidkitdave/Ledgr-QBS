"""Tests for the ``_get_credit_service`` seam in ``ledgr_agent.tools.document_tools``.

The seam exists so production can wire a Firestore-backed ``CreditService``
without changing ``_credit_gate``. Tests should be able to swap the factory
in/out without touching production code paths.
"""

from __future__ import annotations

from typing import Any

import pytest

from ledgr_agent.tools import document_tools
from ledgr_agent.tools.document_tools import _credit_gate, _get_credit_service


@pytest.fixture(autouse=True)
def _reset_credit_factory() -> None:
    """Make sure each test starts with a clean factory slot and singleton."""
    original_factory = document_tools._credit_service_factory
    original_singleton = document_tools._credit_service_singleton
    document_tools._credit_service_factory = None
    document_tools._credit_service_singleton = None
    try:
        yield
    finally:
        document_tools._credit_service_factory = original_factory
        document_tools._credit_service_singleton = original_singleton


def test_default_factory_returns_credit_service() -> None:
    """Default behaviour: ``_get_credit_service`` returns an in-memory CreditService."""
    service = _get_credit_service()
    assert service is not None
    # Sanity check: the default store is the in-memory backend.
    assert hasattr(service, "read_balance")
    assert service.read_balance("any-firm") == 0


def test_default_gate_blocks_unknown_firm_with_zero_balance() -> None:
    """An unknown firm has balance 0 → not >= 1 path → blocked.

    Documents the production contract of ``_credit_gate`` when wired with the
    default in-memory store: an unprovisioned firm cannot run batches because
    ``read_balance`` returns 0.
    """
    decision = _credit_gate(firm_id="unknown-firm", paths=["/tmp/nope.pdf"])
    assert decision["allowed"] is False
    assert decision["reason"] == "zero_credit"
    assert decision["balance"] == 0


def test_default_gate_skipped_when_firm_id_is_none() -> None:
    """Missing firm_id is treated as a no-op allow (defensive path)."""
    decision = _credit_gate(firm_id=None, paths=["/tmp/nope.pdf"])
    assert decision == {"allowed": True, "reason": "ok", "balance": 0}


def test_monkeypatching_factory_changes_what_gate_uses(monkeypatch: pytest.MonkeyPatch) -> None:
    """A swapped factory must change what ``_credit_gate`` sees."""

    class _StubService:
        def __init__(self, balance: int) -> None:
            self._balance = balance
            self.read_calls: list[str] = []

        def read_balance(self, firm_id: str) -> int:
            self.read_calls.append(firm_id)
            return self._balance

    stub = _StubService(balance=0)
    monkeypatch.setattr(
        document_tools,
        "_credit_service_factory",
        lambda: stub,
    )

    decision = _credit_gate(firm_id="firm-1", paths=["/tmp/a.pdf", "/tmp/b.pdf"])

    assert stub.read_calls == ["firm-1"], "gate must read through the stub"
    assert decision["allowed"] is False
    assert decision["reason"] == "zero_credit"
    assert decision["balance"] == 0


def test_monkeypatched_factory_with_positive_balance_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    class _StubService:
        def __init__(self, balance: int) -> None:
            self._balance = balance

        def read_balance(self, firm_id: str) -> int:
            return self._balance

    monkeypatch.setattr(
        document_tools,
        "_credit_service_factory",
        lambda: _StubService(balance=5),
    )

    decision = _credit_gate(firm_id="firm-with-credits", paths=["/tmp/a.pdf"])

    assert decision["allowed"] is True
    assert decision["reason"] == "ok"
    assert decision["balance"] == 5


def test_gate_blocks_when_required_units_exceed_balance(monkeypatch: pytest.MonkeyPatch) -> None:
    class _StubService:
        def read_balance(self, firm_id: str) -> int:
            return 2

    monkeypatch.setattr(
        document_tools,
        "_credit_service_factory",
        lambda: _StubService(),
    )

    decision = _credit_gate(
        firm_id="firm-with-some-credits",
        paths=["/tmp/five-page.pdf"],
        required_units=5,
    )

    assert decision["allowed"] is False
    assert decision["reason"] == "insufficient_credit"
    assert decision["balance"] == 2
    assert decision["required_units"] == 5


def test_missing_app_package_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """When neither factory nor ``app.credit_service`` is available, return ``None``.

    We simulate the import failure by clearing the factory and replacing the
    module's own lazy import with one that raises ImportError.
    """
    document_tools._credit_service_factory = None

    import builtins

    real_import = builtins.__import__

    def _blocking_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "app.credit_service" or name.startswith("app.credit_service"):
            raise ImportError("simulated absence of app.credit_service")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)

    assert _get_credit_service() is None
    decision = _credit_gate(firm_id="any-firm", paths=["/tmp/a.pdf"])
    assert decision == {"allowed": True, "reason": "ok", "balance": 0}
