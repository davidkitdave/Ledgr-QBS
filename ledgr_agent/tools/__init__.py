"""Ledgr agent tools."""

from __future__ import annotations

from ledgr_agent.billing import read_credit_balance

__all__ = [
    "build_sheets",
    "read_credit_balance",
    "read_doc",
]


def __getattr__(name: str):
    if name == "read_doc":
        from ledgr_agent.tools.read_doc import read_doc

        return read_doc
    if name == "build_sheets":
        from ledgr_agent.tools.build_sheets import build_sheets

        return build_sheets
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
