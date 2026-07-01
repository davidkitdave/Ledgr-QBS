"""Dev credit auto-grant from LEDGR_DEV_CREDIT_GRANTS."""

from __future__ import annotations

import logging

from ledgr_slack.credit_adapter import apply_dev_credit_grants_from_env, wire_shared_credit_service
from ledgr_agent.billing import get_shared_credit_service

logger = logging.getLogger(__name__)


def test_apply_dev_credit_grants_from_env(monkeypatch) -> None:
    from ledgr_agent.billing import configure_shared_credit_service, CreditService, InMemoryCreditStore

    store = InMemoryCreditStore()
    configure_shared_credit_service(CreditService(store))
    monkeypatch.setenv("LEDGR_DEV_CREDIT_GRANTS", "TQA:50,TZERO:0,bad, T2:10")
    apply_dev_credit_grants_from_env()
    assert get_shared_credit_service().read_balance("TQA") == 50
    assert get_shared_credit_service().read_balance("T2") == 10
    assert get_shared_credit_service().read_balance("TZERO") == 0


def test_apply_dev_credit_grants_idempotent(monkeypatch) -> None:
    from ledgr_agent.billing import configure_shared_credit_service, CreditService, InMemoryCreditStore

    store = InMemoryCreditStore()
    configure_shared_credit_service(CreditService(store))
    monkeypatch.setenv("LEDGR_DEV_CREDIT_GRANTS", "TQA:50")
    apply_dev_credit_grants_from_env()
    apply_dev_credit_grants_from_env()
    assert get_shared_credit_service().read_balance("TQA") == 50


def test_dev_seed_skips_when_balance_already_positive(monkeypatch) -> None:
    from ledgr_agent.billing import configure_shared_credit_service, CreditService, InMemoryCreditStore

    store = InMemoryCreditStore()
    service = CreditService(store)
    service.grant("TEXIST", 12, note="prior")
    configure_shared_credit_service(service)
    monkeypatch.setenv("LEDGR_DEV_CREDIT_GRANTS", "TEXIST:50")
    apply_dev_credit_grants_from_env()
    assert get_shared_credit_service().read_balance("TEXIST") == 12


def test_wire_shared_credit_service_applies_dev_grants(monkeypatch) -> None:
    from ledgr_agent.billing import configure_shared_credit_service, CreditService, InMemoryCreditStore

    store = InMemoryCreditStore()
    configure_shared_credit_service(CreditService(store))
    monkeypatch.setenv("LEDGR_DEV_CREDIT_GRANTS", "TBOOT:25")
    wire_shared_credit_service()
    assert get_shared_credit_service().read_balance("TBOOT") == 25
