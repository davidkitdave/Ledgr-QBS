"""Tests for the hermetic CreditService slice 1 (plan task #5.1).

The in-memory store keeps these tests fast and offline; the production
Firestore-backed store will be added in a later slice behind the same
``CreditStore`` protocol.
"""

import pytest

from app.credit_service import CreditService, InMemoryCreditStore


@pytest.fixture
def service() -> CreditService:
    return CreditService(store=InMemoryCreditStore())


def test_grant_and_read_balance(service: CreditService) -> None:
    service.ensure_firm("T123")
    service.grant("T123", amount=10, note="trial")
    assert service.read_balance("T123") == 10


def test_deduct_is_transactional(service: CreditService) -> None:
    service.ensure_firm("T123")
    service.grant("T123", amount=5, note="trial")
    service.deduct("T123", amount=2, reason="delivery", idempotency_key="file-1")
    assert service.read_balance("T123") == 3
    service.deduct("T123", amount=2, reason="delivery", idempotency_key="file-1")
    assert service.read_balance("T123") == 3
