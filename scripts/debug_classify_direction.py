#!/usr/bin/env python3
"""Debug classify + direction for a single PDF (live QA / eval triage tool).

Drops straight into the production code path:
- classify_document(pdf_bytes, mime) → ClassificationResult (Gemini Flash)
- resolve_direction(result, client_name, client_uen) → "purchase"|"sales"|...
- extract_ledger_file(pdf_path) (understand path) → vendor/customer/summary table

Prints a side-by-side view so you can spot drift between Gemini's party reads
and the resolved direction. Use this when the bot's sales/purchase routing
disagrees with the human's expectation — it tells you exactly which side
(classifier vs deterministic resolve) drifted and why.

Examples:

    uv run python scripts/debug_classify_direction.py path/to/invoice.pdf
    uv run python scripts/debug_classify_direction.py path/to/invoice.pdf \\
        --client-name "Acme Client Pte. Ltd." --client-uen "201712345A"
    uv run python scripts/debug_classify_direction.py path/to/invoice.pdf --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")
import os
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")

from invoice_processing.classify.document_classifier import (
    classify_document,
    resolve_direction,
)
from invoice_processing.extract.ledger_extract import extract_ledger_file


def _print_json(obj: dict) -> None:
    print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="Classify a PDF and print direction debug info")
    parser.add_argument("pdf", type=Path, help="Path to PDF (or PNG/JPG/WEBP/GIF)")
    parser.add_argument(
        "--client-name",
        default="Acme Client Pte. Ltd.",
        help="Channel client name (from Firestore profile)",
    )
    parser.add_argument(
        "--client-uen",
        default="",
        help="Channel client UEN (improves fuzzy match for direction)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a human report",
    )
    args = parser.parse_args()

    pdf: Path = args.pdf.expanduser()
    if not pdf.exists():
        raise SystemExit(f"PDF not found: {pdf}")

    data = pdf.read_bytes()
    mime = "application/pdf" if pdf.suffix.lower() == ".pdf" else "application/octet-stream"
    if pdf.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        mime = f"image/{pdf.suffix.lower().lstrip('.')}"

    cls = classify_document(data, mime)
    direction = resolve_direction(cls, client_name=args.client_name, client_uen=args.client_uen)

    # Understand extract (Gemini, may be slow) — only if classifier succeeded.
    extract_summary: dict = {"skipped": True}
    try:
        ex = extract_ledger_file(pdf)
        extract_summary = {
            "vendor_name": ex.vendor_name,
            "customer_name": ex.customer_name,
            "summary_table": ex.summary_table,
        }
    except Exception as exc:  # noqa: BLE001 - extraction is best-effort
        extract_summary = {"skipped": True, "reason": str(exc)}

    if args.json:
        _print_json({
            "pdf": str(pdf),
            "client": {"name": args.client_name, "uen": args.client_uen},
            "classification": cls.model_dump(),
            "resolved_direction": direction,
            "understand_extract": extract_summary,
        })
        return

    print(f"PDF: {pdf}")
    print(f"Client: {args.client_name!r} (UEN={args.client_uen!r})")
    print()
    print("--- Classifier (Gemini Flash) ---")
    print(f"  doc_type:      {cls.doc_type}")
    print(f"  issuer_name:   {cls.issuer_name!r}")
    print(f"  bill_to_name:  {cls.bill_to_name!r}")
    print(f"  currency:      {cls.currency!r}")
    print(f"  total_amount:  {cls.total_amount!r}")
    print(f"  confidence:    {cls.confidence:.2f}")
    print(f"  reason:        {cls.reason}")
    print()
    print(f"--- Resolved direction: {direction} ---")
    print()
    print("--- Understand extract (Gemini, slower) ---")
    if extract_summary.get("skipped"):
        print(f"  skipped: {extract_summary.get('reason', 'n/a')}")
    else:
        print(f"  vendor_name:    {extract_summary['vendor_name']!r}")
        print(f"  customer_name:  {extract_summary['customer_name']!r}")
        if extract_summary.get("summary_table"):
            print("  summary_table:")
            for line in extract_summary["summary_table"][:5]:
                print(f"    - {line}")
            if len(extract_summary["summary_table"]) > 5:
                print(f"    ... and {len(extract_summary['summary_table']) - 5} more")
    print()
    drift = (
        extract_summary.get("customer_name")
        and cls.bill_to_name
        and extract_summary["customer_name"] != cls.bill_to_name
    )
    if drift:
        print("[DRIFT] understand-extract customer_name != classifier bill_to_name")
        print(f"  understand customer: {extract_summary['customer_name']!r}")
        print(f"  classifier bill_to:  {cls.bill_to_name!r}")


if __name__ == "__main__":
    main()
