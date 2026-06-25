"""Tests for read_credit_balance playground tool."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.credit_service import CreditService, InMemoryCreditStore, configure_shared_credit_service
from accounting_agents.credit_delivery import wire_shared_credit_service
from ledgr_agent.tools.credit_tools import read_credit_balance
from ledgr_agent.tools import document_tools


@pytest.fixture(autouse=True)
def _restore_credit_factory():
    """Restore document_tools globals and app.credit_service singleton after each test."""
    import app.credit_service as _cs_mod

    saved_factory = document_tools._credit_service_factory
    saved_singleton = document_tools._credit_service_singleton
    saved_shared = _cs_mod._shared_credit_service
    try:
        yield
    finally:
        document_tools._credit_service_factory = saved_factory
        document_tools._credit_service_singleton = saved_singleton
        _cs_mod._shared_credit_service = saved_shared


def test_read_credit_balance_returns_firm_balance() -> None:
    store = InMemoryCreditStore()
    service = CreditService(store)
    service.ensure_firm("T_PLAYGROUND")
    service.grant("T_PLAYGROUND", 12, note="qa")
    configure_shared_credit_service(service)
    document_tools._credit_service_factory = lambda: service

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


def test_wire_shared_credit_service_applies_dev_grants(monkeypatch) -> None:
    store = InMemoryCreditStore()
    configure_shared_credit_service(CreditService(store))
    document_tools._credit_service_factory = None
    monkeypatch.setenv("LEDGR_DEV_CREDIT_GRANTS", "T_PLAYGROUND:7")
    wire_shared_credit_service()
    out = read_credit_balance(
        SimpleNamespace(state={"firm_id": "T_PLAYGROUND", "client_id": "x"})
    )
    assert out["balance"] == 7
