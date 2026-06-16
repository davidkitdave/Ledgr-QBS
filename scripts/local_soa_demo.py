#!/usr/bin/env python3
"""Local SOA package demo — Phase 1 + Phase 2 on Sample Vendor Inc (or LEDGR_SOA_PDF)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

import os as _os
if "GOOGLE_GENAI_USE_VERTEXAI" not in _os.environ:
    _os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "FALSE"

from invoice_processing.extract.document_extractor import extract_document_file, mime_for
from invoice_processing.extract.document_normalizer import normalize_document_bundle
from invoice_processing.extract.record_merge import merge_document_records

DEFAULT = (
    Path.home()
    / "Desktop/LocalTest/TestDoc/MYDoc/Sample Auto Enterprise/Purchase/SOA-SAMPLE-DEC-2025_.pdf"
)

EXPECTED_NUMBERS = {
    "CNA-00176", "IA-07465", "IA-07467", "IA-07514", "IA-07522",
    "IA-07526", "IA-07527", "IA-07573", "IA-07588", "IA-07590",
}
PHANTOM_NUMBERS = {
    "IA-07316", "IA-07330", "IA-07332", "IA-07365", "IA-07368",
    "IA-07383", "IA-07392", "IA-07428",
}


def main() -> None:
    path = Path(os.environ.get("LEDGR_SOA_PDF", str(DEFAULT)))
    if not path.exists():
        print(json.dumps({"error": f"not found: {path}"}))
        sys.exit(1)

    print(f"Extracting {path.name}...", file=sys.stderr)
    bundle = extract_document_file(path)
    bundle = merge_document_records(bundle)
    normalized = normalize_document_bundle(
        bundle, direction="purchase", base_currency="MYR",
    )

    numbers = [inv.invoice_number for inv in normalized]
    number_set = {n for n in numbers if n}
    report = {
        "file": path.name,
        "skipped_pages": bundle.skipped_pages,
        "phase1_documents": len(bundle.documents),
        "phase2_invoices": len(normalized),
        "phase2_lines": sum(len(inv.lines) for inv in normalized),
        "invoice_numbers": numbers,
        "expected_count": 10,
        "expected_lines": 22,
        "missing_expected": sorted(EXPECTED_NUMBERS - number_set),
        "phantom_found": sorted(number_set & PHANTOM_NUMBERS),
        "invoices": [
            {
                "invoice_number": inv.invoice_number,
                "currency": inv.currency,
                "doc_total": inv.doc_total,
                "line_count": len(inv.lines),
                "reconciled": inv.reconciled,
            }
            for inv in normalized
        ],
    }
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
