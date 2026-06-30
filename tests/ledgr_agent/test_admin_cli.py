"""Tests for ledgr_agent.admin operator CLI."""

from __future__ import annotations

from ledgr_agent.admin import build_parser, cmd_grant
from ledgr_agent.billing import CreditService, InMemoryCreditStore, configure_shared_credit_service


def test_grant_cli_adds_credits(monkeypatch) -> None:
    service = CreditService(InMemoryCreditStore())
    configure_shared_credit_service(service)
    monkeypatch.setattr("ledgr_agent.admin._wire_store", lambda: service)
    monkeypatch.setattr("ledgr_agent.admin._list_installations", lambda: [])

    rc = cmd_grant(
        type("NS", (), {"firm": "T_TEST", "amount": 50, "note": "trial"})()
    )
    assert rc == 0
    assert service.read_balance("T_TEST") == 50


def test_parser_has_grant_and_list() -> None:
    parser = build_parser()
    args = parser.parse_args(["grant", "--firm", "T1", "--amount", "10"])
    assert args.command == "grant"
    assert args.firm == "T1"
    assert args.amount == 10
