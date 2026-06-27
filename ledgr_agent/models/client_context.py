"""Minimal client profile types for ledgr_agent."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ClientContext:
    client_id: str
    client_name: str
    fye_month: int = 12
    accounting_software: str = "QBS Ledger"
    base_currency: str = "SGD"
    tax_registered: bool = True
    region: str = "SINGAPORE"
    client_uen: str | None = None
    firm_id: str | None = None
    slack_team_id: str | None = None

    def to_state(self) -> dict[str, object]:
        return {
            "client_id": self.client_id,
            "client_name": self.client_name,
            "fye_month": self.fye_month,
            "software": self.accounting_software,
            "base_currency": self.base_currency,
            "tax_registered": self.tax_registered,
            "region": self.region,
            "client_uen": self.client_uen,
            "firm_id": self.firm_id,
            "slack_team_id": self.slack_team_id,
        }


def client_context_from_state(state: dict) -> ClientContext:
    """Rebuild a minimal :class:`ClientContext` from session state."""
    region = str(state.get("region") or "SINGAPORE")
    base_currency = str(state.get("base_currency") or "SGD")
    return ClientContext(
        client_id=str(state.get("client_id") or "unknown"),
        client_name=str(state.get("client_name") or "Unknown Client"),
        client_uen=state.get("client_uen"),
        firm_id=state.get("firm_id") or state.get("slack_team_id"),
        slack_team_id=state.get("slack_team_id"),
        region=region,
        accounting_software=str(state.get("software") or "QBS Ledger"),
        base_currency=base_currency,
        tax_registered=bool(state.get("tax_registered", True)),
        fye_month=int(state.get("fye_month") or 12),
    )


def playground_default_context() -> ClientContext:
    """Default playground client profile for dev/eval."""
    return ClientContext(
        client_id="playground",
        client_name="Playground Client",
        firm_id="T_PLAYGROUND",
        slack_team_id="T_PLAYGROUND",
        region="SINGAPORE",
        accounting_software="qbs",
        base_currency="SGD",
        tax_registered=True,
        fye_month=12,
    )


class InMemoryClientStore:
    def __init__(self) -> None:
        self._by_id: dict[str, ClientContext] = {}

    def add(self, ctx: ClientContext) -> None:
        self._by_id[ctx.client_id] = ctx

    def get(self, client_id: str) -> ClientContext | None:
        return self._by_id.get(client_id)
