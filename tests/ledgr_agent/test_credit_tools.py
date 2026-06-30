"""Tests for read_credit_balance playground tool."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ledgr_agent.billing import (
    CreditService,
    InMemoryCreditStore,
    configure_shared_credit_service,
    wire_playground_credits,
)
from ledgr_agent.billing import read_credit_balance


@pytest.fixture(autouse=True)
def _restore_shared_service():
    import ledgr_agent.billing as billing

    saved = billing._shared_credit_service
    try:
        yield
    finally:
        billing._shared_credit_service = saved


def test_read_credit_balance_returns_firm_balance() -> None:
    store = InMemoryCreditStore()
    service = CreditService(store)
    service.ensure_firm("T_PLAYGROUND")
    service.grant("T_PLAYGROUND", 12, note="qa")
    configure_shared_credit_service(service)

    result = read_credit_balance(
        SimpleNamespace(state={"firm_id": "T_PLAYGROUND", "client_id": "playground"})
    )
    assert result["status"] == "success"
    assert result["balance"] == 12
    assert result["firm_id"] == "T_PLAYGROUND"


def test_read_credit_balance_errors_without_firm_id() -> None:
    result = read_credit_balance(SimpleNamespace(state={"client_id": "playground"}))
    assert result["status"] == "error"
    assert "firm_id" in result["message"]


def test_wire_playground_credits_applies_dev_grants(monkeypatch) -> None:
    configure_shared_credit_service(CreditService(InMemoryCreditStore()))
    monkeypatch.setenv("LEDGR_DEV_CREDIT_GRANTS", "T_PLAYGROUND:7")
    wire_playground_credits()
    out = read_credit_balance(
        SimpleNamespace(state={"firm_id": "T_PLAYGROUND", "client_id": "x"})
    )
    assert out["balance"] == 7
