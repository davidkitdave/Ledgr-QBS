"""Round 2 tournament scaffold — telco/SOA segmentation (offline).

Run when SOA/telco PDF paths are available locally:

    .venv/bin/python -m eval.extraction_tournament_soa --hermetic

Live run (set LEDGR_SOA_PDF to a multi-invoice SOA package):

    LEDGR_SOA_PDF=/path/to/soa.pdf .venv/bin/python -m eval.extraction_tournament_soa
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from invoice_processing.extract.extraction_spine import (
    ExtractionContext,
    ExtractionVariant,
    run_extraction_spine,
)
from invoice_processing.extract.document_extractor import mime_for

REPORT_PATH = Path(__file__).parent.parent / "docs" / "qa" / "tournament_round2_soa_report.json"


def run_soa_tournament(*, hermetic: bool = False) -> dict:
    """Compare V0 vs V3 on SOA segmentation (document count + phantom detection)."""
    variants = [ExtractionVariant.V0, ExtractionVariant.V3]
    ctx = ExtractionContext(direction="purchase", base_currency="SGD")
    results = []

    if hermetic:
        from invoice_processing.extract.document_record import (
            DocumentRecord,
            DocumentRecordBundle,
            LabeledField,
            LineCapture,
        )

        # SOA cover phantom + 2 real invoices (V0 might keep phantom; V3 should skip cover)
        bundle_v0_shape = DocumentRecordBundle(
            documents=[
                DocumentRecord(line_items=[LineCapture(description="INVOICE", net_amount=0)]),
                DocumentRecord(
                    labeled_fields=[LabeledField(label="Invoice Number", value="INV-1")],
                    line_items=[LineCapture(description="Service A", net_amount=100.0)],
                    totals=[LabeledField(label="Total", value="100")],
                ),
            ]
        )
        for variant in variants:
            result = run_extraction_spine(
                b"", "application/pdf", variant=variant, context=ctx, bundle=bundle_v0_shape,
            )
            results.append({
                "variant": variant.value,
                "hermetic": True,
                "document_count": result.metrics.document_count,
                "invoice_count": result.metrics.invoice_count,
                "score": result.metrics.score,
            })
        return {"round": 2, "topic": "soa_segmentation", "hermetic": True, "results": results}

    pdf = os.environ.get("LEDGR_SOA_PDF")
    if not pdf or not Path(pdf).exists():
        return {
            "round": 2,
            "topic": "soa_segmentation",
            "error": "Set LEDGR_SOA_PDF to a local SOA package path",
            "results": [],
        }

    path = Path(pdf)
    data = path.read_bytes()
    mime = mime_for(path)
    for variant in variants:
        result = run_extraction_spine(data, mime, variant=variant, context=ctx)
        results.append({
            "variant": variant.value,
            "file": path.name,
            "document_count": result.metrics.document_count,
            "invoice_count": result.metrics.invoice_count,
            "score": result.metrics.score,
            "details": result.metrics.details,
        })

    return {"round": 2, "topic": "soa_segmentation", "results": results}


def main() -> None:
    parser = argparse.ArgumentParser(description="SOA/telco segmentation tournament (round 2)")
    parser.add_argument("--hermetic", action="store_true")
    args = parser.parse_args()
    report = run_soa_tournament(hermetic=args.hermetic)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    print(f"Report: {REPORT_PATH}")


if __name__ == "__main__":
    main()
