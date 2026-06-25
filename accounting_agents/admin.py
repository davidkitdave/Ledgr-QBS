"""Minimal operator CLI for the credit ledger (plan task #5.1).

Run as a module so it can be invoked from the repo root without PYTHONPATH
games::

    python -m accounting_agents.admin grant T123 50 --note "topup"
    python -m accounting_agents.admin list
"""

from __future__ import annotations

import argparse
import sys
from typing import List

from app.credit_service import InMemoryCreditStore, get_shared_credit_service

_service = get_shared_credit_service()


def grant(firm_id: str, amount: int, note: str = "") -> None:
    _service.ensure_firm(firm_id)
    _service.grant(firm_id, amount=amount, note=note)


def list_firms() -> List[str]:
    inner = _service._store  # type: ignore[attr-defined]
    if isinstance(inner, InMemoryCreditStore):
        return inner.known_firms()
    return []


def _main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="accounting_agents.admin")
    sub = parser.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("grant")
    g.add_argument("firm_id")
    g.add_argument("amount", type=int)
    g.add_argument("--note", default="")
    sub.add_parser("list")
    args = parser.parse_args(argv)
    if args.cmd == "grant":
        grant(args.firm_id, args.amount, args.note)
        print(f"granted {args.amount} to {args.firm_id}")
    elif args.cmd == "list":
        for firm_id in list_firms():
            print(firm_id, _service.read_balance(firm_id))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
