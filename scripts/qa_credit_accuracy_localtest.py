#!/usr/bin/env python3
"""Live credit-accuracy harness for Desktop/LocalTest PDFs (control-CLI).

Runs ``process_document_batch`` against real files with tool-side billing enabled
(``LEDGR_CHARGE_CREDITS_IN_TOOL=1``) so balance before/after is observable without
Slack. Compare:

- **gate_units** — pre-flight estimate (page count)
- **credits_used** — actual deduct (reconciled doc count when tool billing on)
- **posted_documents** — what the engine delivered

Usage::

    set -a && source .env && set +a
    export LEDGR_CHARGE_CREDITS_IN_TOOL=1
    uv run python scripts/qa_credit_accuracy_localtest.py \\
        "/Users/davidkitdave/Desktop/LocalTest/.../invoice.pdf"
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

# Repo root on path when invoked as script
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from accounting_agents.credit_delivery import estimate_upload_pages
from app.credit_service import CreditService, InMemoryCreditStore, configure_shared_credit_service
from invoice_processing.shared_libraries.playground_context import playground_default_context
from ledgr_agent.tools import document_tools
from ledgr_agent.tools.credit_tools import read_credit_balance
from ledgr_agent.tools.document_tools import process_document_batch


def _playground_state() -> dict:
    state = playground_default_context().to_state()
    state.setdefault("firm_id", "T_PLAYGROUND")
    state.setdefault("slack_team_id", "T_PLAYGROUND")
    return state


def _run_one(path: Path, *, service: CreditService, repeat: bool = False) -> dict:
    data = path.read_bytes()
    gate_units = estimate_upload_pages(data, path.name)
    before = int(service.read_balance("T_PLAYGROUND"))

    batch = process_document_batch(
        SimpleNamespace(state=_playground_state()),
        paths=[str(path.resolve())],
    )

    after = int(service.read_balance("T_PLAYGROUND"))
    credits = batch.get("credits") or {}
    posted = batch.get("posted_documents") or []
    per_file = batch.get("per_file") or []

    return {
        "file": path.name,
        "repeat": repeat,
        "gate_units": gate_units,
        "balance_before": before,
        "balance_after": after,
        "balance_delta": before - after,
        "batch_status": batch.get("status"),
        "credits_used_reported": credits.get("credits_used"),
        "credits_remaining_reported": credits.get("credits_remaining"),
        "credit_status": credits.get("credit_status"),
        "posted_document_count": len(posted),
        "per_file_doc_types": [p.get("doc_type") for p in per_file if isinstance(p, dict)],
        "block_reason": (batch.get("validation_summary") or {}).get("block_reason"),
    }


def main(argv: list[str]) -> int:
    if not os.environ.get("GOOGLE_API_KEY"):
        print("ERROR: GOOGLE_API_KEY required for live extraction", file=sys.stderr)
        return 1

    os.environ.setdefault("LEDGR_CHARGE_CREDITS_IN_TOOL", "1")
    paths = [Path(p).expanduser().resolve() for p in argv[1:]]
    if not paths:
        default_invoice = Path(
            "/Users/davidkitdave/Desktop/LocalTest/TestDoc/Company-A/"
            "Company-B Pte. Ltd./Sales/FY2025/INV-001.pdf"
        )
        default_bank = Path(
            "/Users/davidkitdave/Desktop/LocalTest/TestDoc/Company-A/"
            "Company-B Pte. Ltd./BankStatement/FY2025/"
            "BUSINESS GROWTH ACCOUNT-5001-Jan-25.pdf"
        )
        paths = [p for p in (default_invoice, default_bank) if p.exists()]
        if not paths:
            print("Usage: qa_credit_accuracy_localtest.py <pdf> [pdf2 ...]", file=sys.stderr)
            return 2

    store = InMemoryCreditStore()
    service = CreditService(store)
    service.ensure_firm("T_PLAYGROUND")
    service.grant("T_PLAYGROUND", 500, note="qa accuracy run")
    configure_shared_credit_service(service)
    document_tools._credit_service_factory = lambda: service

    print("=== Ledgr credit accuracy (LocalTest, tool billing ON) ===")
    print(f"firm_id=T_PLAYGROUND start_balance={service.read_balance('T_PLAYGROUND')}")
    print()

    results: list[dict] = []
    for path in paths:
        if not path.exists():
            print(f"SKIP missing: {path}")
            continue
        print(f"Processing: {path.name} ...")
        first = _run_one(path, service=service, repeat=False)
        results.append(first)
        print(f"  1st run: gate={first['gate_units']} charged={first['balance_delta']} "
              f"status={first['batch_status']} credit_status={first['credit_status']}")
        second = _run_one(path, service=service, repeat=True)
        results.append(second)
        print(f"  2nd run (dedup/idempotent): charged={second['balance_delta']} "
              f"status={second['batch_status']} credit_status={second['credit_status']}")
        print()

    print("=== Summary JSON ===")
    print(json.dumps(results, indent=2))

    balance_check = read_credit_balance(
        SimpleNamespace(state=_playground_state())
    )
    print()
    print("read_credit_balance:", balance_check)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
