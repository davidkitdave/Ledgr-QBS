#!/usr/bin/env python3
"""Local two-phase extraction demo — prints Phase 1 table + Phase 2 summary."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from invoice_processing.extract.document_extractor import extract_document_bundle, mime_for
from invoice_processing.extract.document_normalizer import normalize_document_bundle
from invoice_processing.extract.record_merge import merge_document_records

CLIENT = "Acme Client Pte. Ltd."
BASE = Path.home() / "Desktop/LocalTest/TestDoc/Sample Test Group" / CLIENT

DOCS = [
    BASE / "Purchase/FY2026/INV-2026-003-sample.pdf",
    BASE / "Purchase/FY2025/INV-2025-012-sample.pdf",
    BASE / "Purchase/FY2025/MGT-2025-011-sample.pdf",
    BASE / "Purchase/FY2026/EXP-2026-040-sample.pdf",
]


def _field_table(record) -> list[dict]:
    rows = [{"Label": f.label, "Value": f.value, "Source": f.source} for f in record.labeled_fields]
    for t in record.totals:
        rows.append({"Label": f"[total] {t.label}", "Value": t.value, "Source": t.source})
    return rows


def _line_table(record) -> list[dict]:
    return [
        {
            "Description": li.description[:80],
            "Qty": li.quantity,
            "Unit": li.unit_amount,
            "Net": li.net_amount,
            "Currency": li.currency,
        }
        for li in record.line_items
    ]


def run_one(path: Path) -> dict:
    data = path.read_bytes()
    mime = mime_for(path)
    bundle = extract_document_bundle(data, mime)
    bundle = merge_document_records(bundle)
    normalized = normalize_document_bundle(
        bundle,
        direction="purchase",
        base_currency="SGD",
        client_name=CLIENT,
    )
    docs_out = []
    for i, rec in enumerate(bundle.documents):
        docs_out.append(
            {
                "doc_index": i + 1,
                "doc_kind_guess": rec.doc_kind_guess,
                "parties": [{"name": p.name, "role_hint": p.role_hint} for p in rec.parties],
                "labeled_fields": _field_table(rec),
                "line_items": _line_table(rec),
                "annotations": [{"kind": a.kind, "text": a.text} for a in rec.annotations],
                "tables_count": len(rec.tables),
                "notes": rec.notes,
            }
        )
    norm_out = []
    for inv in normalized:
        norm_out.append(
            {
                "invoice_number": inv.invoice_number,
                "invoice_date": str(inv.invoice_date) if inv.invoice_date else None,
                "supplier": inv.supplier.name if inv.supplier else None,
                "customer": inv.customer.name if inv.customer else None,
                "currency": inv.currency,
                "doc_total": inv.doc_total,
                "needs_fx_review": inv.needs_fx_review,
                "reconciled": inv.reconciled,
                "reconcile_note": inv.reconcile_note,
                "lines": [
                    {
                        "description": l.description[:60],
                        "net_amount": l.net_amount,
                        "tax_keyword": l.tax_keyword,
                    }
                    for l in inv.lines
                ],
            }
        )
    return {
        "file": path.name,
        "path": str(path),
        "documents_captured": len(bundle.documents),
        "skipped_pages": bundle.skipped_pages,
        "phase1": docs_out,
        "phase2": norm_out,
    }


def main() -> None:
    results = []
    for p in DOCS:
        if not p.exists():
            results.append({"file": p.name, "error": f"not found: {p}"})
            continue
        print(f"Extracting {p.name}...", file=sys.stderr)
        try:
            results.append(run_one(p))
        except Exception as exc:
            results.append({"file": p.name, "error": str(exc)})
    print(json.dumps({"client": CLIENT, "results": results}, indent=2, default=str))


if __name__ == "__main__":
    main()
