"""Operator CLI for credit grants and workspace discovery (ADR-0016)."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from ledgr_agent.billing import (
    CreditService,
    configure_durable_credit_service_if_prod,
    get_shared_credit_service,
)


def _wire_store() -> CreditService:
    configure_durable_credit_service_if_prod()
    return get_shared_credit_service()


def _workspaces_collection() -> str:
    prefix = os.environ.get("LEDGR_FIRESTORE_NAMESPACE", "").strip()
    return f"{prefix}_workspaces" if prefix else "workspaces"


def _list_installations() -> list[dict[str, Any]]:
    from google.cloud import firestore

    db = firestore.Client()
    rows: list[dict[str, Any]] = []
    for snap in db.collection(_workspaces_collection()).stream():
        data = snap.to_dict() or {}
        team_id = str(data.get("team_id") or "").strip()
        if not team_id:
            continue
        rows.append(
            {
                "team_id": team_id,
                "team_name": str(data.get("team_name") or data.get("team") or {}).get("name", "")
                if isinstance(data.get("team"), dict)
                else str(data.get("team_name") or ""),
                "installed_at": data.get("installed_at") or data.get("is_enterprise_install"),
            }
        )
    rows.sort(key=lambda r: r.get("team_name") or r["team_id"])
    return rows


def cmd_list_firms(_: argparse.Namespace) -> int:
    service = _wire_store()
    installations = _list_installations()
    if not installations:
        print("No workspaces found in Firestore.")
        return 0
    print(f"{'team_id':<14} {'balance':>8}  team_name")
    print("-" * 60)
    for row in installations:
        team_id = row["team_id"]
        balance = service.read_balance(team_id)
        name = row.get("team_name") or ""
        print(f"{team_id:<14} {balance:>8}  {name}")
    return 0


def cmd_grant(args: argparse.Namespace) -> int:
    firm_id = str(args.firm).strip()
    amount = int(args.amount)
    note = str(args.note or "").strip()
    if not firm_id:
        print("error: --firm is required", file=sys.stderr)
        return 1
    if amount <= 0:
        print("error: --amount must be positive", file=sys.stderr)
        return 1
    service = _wire_store()
    service.ensure_firm(firm_id)
    new_balance = service.grant(firm_id, amount, note=note or "operator grant")
    print(f"Granted {amount} credits to {firm_id} (balance now {new_balance})")
    if note:
        print(f"  note: {note}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ledgr_agent.admin",
        description="Ledgr operator tools — credit grants and workspace lookup.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    list_cmd = sub.add_parser("list-firms", help="List installed workspaces and credit balances")
    list_cmd.set_defaults(func=cmd_list_firms)

    grant_cmd = sub.add_parser("grant", help="Grant credits to a workspace (Slack team_id)")
    grant_cmd.add_argument("--firm", required=True, help="Slack workspace team_id (T…)")
    grant_cmd.add_argument("--amount", required=True, type=int, help="Credits to add")
    grant_cmd.add_argument("--note", default="", help="Audit note (e.g. invoice reference)")
    grant_cmd.set_defaults(func=cmd_grant)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
