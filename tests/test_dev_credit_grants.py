"""Dev credit auto-grant from LEDGR_DEV_CREDIT_GRANTS."""

from __future__ import annotations

import logging

from accounting_agents.credit_delivery import apply_dev_credit_grants_from_env, wire_shared_credit_service
from app.credit_service import get_shared_credit_service

logger = logging.getLogger(__name__)


def test_apply_dev_credit_grants_from_env(monkeypatch) -> None:
    from app.credit_service import configure_shared_credit_service, CreditService, InMemoryCreditStore

    store = InMemoryCreditStore()
    configure_shared_credit_service(CreditService(store))
    monkeypatch.setenv("LEDGR_DEV_CREDIT_GRANTS", "TQA:50,TZERO:0,bad, T2:10")
    apply_dev_credit_grants_from_env()
    assert get_shared_credit_service().read_balance("TQA") == 50
    assert get_shared_credit_service().read_balance("T2") == 10
    assert get_shared_credit_service().read_balance("TZERO") == 0


def test_wire_shared_credit_service_applies_dev_grants(monkeypatch) -> None:
    from app.credit_service import configure_shared_credit_service, CreditService, InMemoryCreditStore

    store = InMemoryCreditStore()
    configure_shared_credit_service(CreditService(store))
    monkeypatch.setenv("LEDGR_DEV_CREDIT_GRANTS", "TBOOT:25")
    wire_shared_credit_service()
    assert get_shared_credit_service().read_balance("TBOOT") == 25
