from __future__ import annotations

from ledgr_agent.models.client_context import ClientContext, InMemoryClientStore

_store = InMemoryClientStore()


def get_client_store() -> InMemoryClientStore:
    """Return the in-process client profile store used by document tools."""

    return _store


def register_client(ctx: ClientContext) -> None:
    """Register a client profile for ``process_document_batch`` lookups."""

    _store.add(ctx)


def resolve_client(client_id: str) -> ClientContext | None:
    """Load a client profile by id, including the built-in playground demo."""

    ctx = _store.get(client_id)
    if ctx is not None:
        return ctx
    if client_id in {"playground", "playground_demo"}:
        return _playground_client()
    return None


def _playground_client() -> ClientContext:
    return ClientContext(
        client_id="playground",
        client_name="Playground Client",
        fye_month=12,
        accounting_software="QBS Ledger",
        base_currency="SGD",
        tax_registered=True,
        region="SINGAPORE",
    )
