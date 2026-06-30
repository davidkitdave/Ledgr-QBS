"""Tests for durable-store selection at startup wiring.

The wiring must pick InMemory in dev/test (no GCP creds, LEDGR_ENV != prod) and
must NOT require firestore there. In prod it installs the durable store.
"""

from __future__ import annotations

import pytest

import ledgr_slack.credit_adapter as cd
from ledgr_agent.billing import (
    CreditService,
    FirestoreCreditStore,
    InMemoryCreditStore,
    configure_shared_credit_service,
    get_shared_credit_service,
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Clear every env var that could flip the durable-store selection."""
    for var in (
        "LEDGR_ENV",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "K_SERVICE",
    ):
        monkeypatch.delenv(var, raising=False)
    # Reset shared singleton to a clean in-memory store each test.
    configure_shared_credit_service(CreditService(InMemoryCreditStore()))
    yield


def test_dev_keeps_in_memory_and_does_not_require_firestore() -> None:
    installed = cd.configure_durable_credit_service_if_prod()
    assert installed is False
    store = get_shared_credit_service()._store
    assert isinstance(store, InMemoryCreditStore)


def test_prod_installs_firestore_store(monkeypatch) -> None:
    monkeypatch.setenv("LEDGR_ENV", "prod")
    # FirestoreCreditStore() construction must not touch the network (lazy client).
    installed = cd.configure_durable_credit_service_if_prod()
    assert installed is True
    store = get_shared_credit_service()._store
    assert isinstance(store, FirestoreCreditStore)


def test_firestore_creds_present_installs_durable_store(monkeypatch) -> None:
    monkeypatch.setenv("K_SERVICE", "ledgr-prod")
    installed = cd.configure_durable_credit_service_if_prod()
    assert installed is True
    assert isinstance(get_shared_credit_service()._store, FirestoreCreditStore)


def test_wire_shared_credit_service_keeps_in_memory_in_dev(monkeypatch) -> None:
    cd.wire_shared_credit_service()
    assert isinstance(get_shared_credit_service()._store, InMemoryCreditStore)
