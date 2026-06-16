"""Offline extraction tournament — compare V0–V3 variants on Sample Test Group fixtures.

Run:
    .venv/bin/python -m eval.extraction_tournament --fixtures sample_test_group
    .venv/bin/python -m eval.extraction_tournament --variants V0,V1,V2,V3 --output /tmp/report.json
    .venv/bin/python -m eval.extraction_tournament --hermetic  # no Gemini, synthetic bundles
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from invoice_processing.extract.document_extractor import extract_document_bundle, mime_for
from invoice_processing.extract.document_record import (
    AnnotationCapture,
    DocumentRecord,
    DocumentRecordBundle,
    LabeledField,
    LineCapture,
    PartyCapture,
)
from invoice_processing.extract.extraction_spine import (
    CAST_UNITY_CLIENT,
    CAST_UNITY_DOCS,
    ExtractionContext,
    ExtractionVariant,
    result_to_dict,
    run_extraction_spine,
)

REPORT_DIR = Path(__file__).parent.parent / "docs" / "qa"
DEFAULT_OUTPUT = REPORT_DIR / "tournament_round1_report.json"
RUBRIC_PATH = REPORT_DIR / "tournament_round1_rubric.md"
BASELINE_PATH = REPORT_DIR / "tournament_winner_baseline.json"

ALL_VARIANTS = [
    ExtractionVariant.V0,
    ExtractionVariant.V1,
    ExtractionVariant.V2,
    ExtractionVariant.V3,
]


@dataclass
class VariantAggregate:
    variant: str
    total_score: float = 0.0
    runs: int = 0
    avg_completeness: float = 0.0
    avg_reconcile: float = 0.0
    false_splits: int = 0
    details: list[str] = field(default_factory=list)

    @property
    def avg_score(self) -> float:
        return self.total_score / self.runs if self.runs else 0.0


def _synthetic_bundle(fixture_id: str) -> DocumentRecordBundle:
    """Hermetic bundles mimicking Sample Test Group shapes for CI without Gemini."""
    if fixture_id == "vendor_invoice_sample":
        rec = DocumentRecord(
            labeled_fields=[
                LabeledField(label="Invoice Number", value="INV-2026-003"),
                LabeledField(label="From", value="Vendor Alpha Pte Ltd"),
                LabeledField(label="Bill To", value="Acme Client - AC"),
                LabeledField(label="Date Range", value="Jan 2026"),
            ],
            parties=[
                PartyCapture(name="Vendor Alpha Pte Ltd", role_hint="sender_block"),
                PartyCapture(name="Acme Client", role_hint="to_block"),
            ],
            line_items=[
                LineCapture(description="PTTEP/UOA monitoring audit", quantity=1, net_amount=500.0),
                LineCapture(description="Create Report", quantity=1, net_amount=300.0),
            ],
            totals=[LabeledField(label="Total", value="USD 800.00")],
            annotations=[AnnotationCapture(text="Paid 14 Jan 26 AAI Wise PG", kind="payment_stamp")],
        )
        return DocumentRecordBundle(documents=[rec])

    if fixture_id == "management_fees":
        rec = DocumentRecord(
            labeled_fields=[
                LabeledField(label="Invoice Number", value="MGT-2025-011-INV"),
                LabeledField(label="Invoice Date", value="15 Jan 2025"),
                LabeledField(label="From", value="ACME REGIONAL LTD"),
                LabeledField(label="To", value="Acme Client Pte Ltd"),
                LabeledField(label="Currency", value="USD"),
            ],
            parties=[
                PartyCapture(name="ACME REGIONAL LTD", role_hint="letterhead"),
                PartyCapture(name="Acme Client Pte Ltd", role_hint="to_block"),
            ],
            line_items=[
                LineCapture(description="Consultation Management Fee", quantity=1, net_amount=6500.0),
            ],
            totals=[LabeledField(label="Total Amount", value="6500.00")],
            annotations=[AnnotationCapture(text="Paid 22 Jan 29 AAI Wise PG", kind="payment_stamp")],
        )
        return DocumentRecordBundle(documents=[rec])

    # expense claim — multi-doc false split
    claim = DocumentRecord(
        doc_kind_guess="expense claim",
        labeled_fields=[
            LabeledField(label="Employee", value="Supplier Gamma"),
            LabeledField(label="Claim Reference", value="AAI-25-040"),
        ],
        parties=[PartyCapture(name="Supplier Gamma", role_hint="employee")],
        line_items=[LineCapture(description="Travel expense", net_amount=150.0, currency="USD")],
        totals=[LabeledField(label="Total", value="$ 2065.57")],
    )
    receipt = DocumentRecord(
        line_items=[LineCapture(description="Receipt taxi", net_amount=25.0, currency="IDR")],
    )
    return DocumentRecordBundle(documents=[claim, receipt, receipt, receipt])


def _fixture_paths() -> list[tuple[str, Path]]:
    mapping = [
        ("vendor_invoice_sample", CAST_UNITY_DOCS[0]),
        ("vendor_invoice_d12", CAST_UNITY_DOCS[1]),
        ("management_fees", CAST_UNITY_DOCS[2]),
        ("expense_claim", CAST_UNITY_DOCS[3]),
    ]
    out: list[tuple[str, Path]] = []
    for fid, path in mapping:
        if path.exists():
            out.append((fid, path))
    return out


def run_tournament(
    *,
    variants: list[ExtractionVariant],
    hermetic: bool = False,
    context: Optional[ExtractionContext] = None,
) -> dict[str, Any]:
    ctx = context or ExtractionContext(
        direction="purchase",
        base_currency="SGD",
        client_name=CAST_UNITY_CLIENT,
    )
    fixtures = _fixture_paths()
    if hermetic:
        fixtures = [
            ("vendor_invoice_sample", Path("hermetic")),
            ("management_fees", Path("hermetic")),
            ("expense_claim", Path("hermetic")),
        ]

    results: list[dict[str, Any]] = []
    aggregates: dict[str, VariantAggregate] = {
        v.value: VariantAggregate(variant=v.value) for v in variants
    }

    for fixture_id, path in fixtures:
        bundles: dict[str, DocumentRecordBundle] = {}
        if hermetic:
            bundles["shared"] = _synthetic_bundle(fixture_id)
        else:
            data = path.read_bytes()
            mime = mime_for(path)
            bundles["default"] = extract_document_bundle(data, mime, model=ctx.model)
            from invoice_processing.extract.extraction_spine import PHASE1_PROMPT_SEGMENTATION

            bundles["seg"] = extract_document_bundle(
                data, mime, model=ctx.model, phase1_prompt=PHASE1_PROMPT_SEGMENTATION,
            )

        for variant in variants:
            if hermetic:
                bundle = bundles["shared"]
            elif variant == ExtractionVariant.V3:
                bundle = bundles["seg"]
            else:
                bundle = bundles["default"]

            if hermetic:
                result = run_extraction_spine(
                    b"", "application/pdf", variant=variant, context=ctx, bundle=bundle,
                )
                result.path = str(path)
            else:
                result = run_extraction_spine(
                    path.read_bytes(),
                    mime_for(path),
                    variant=variant,
                    context=ctx,
                    bundle=bundle,
                )
                result.path = str(path)

            row = result_to_dict(result)
            row["fixture_id"] = fixture_id
            row["file"] = path.name if not hermetic else fixture_id
            results.append(row)

            agg = aggregates[variant.value]
            agg.total_score += result.metrics.score
            agg.runs += 1
            agg.avg_completeness += result.metrics.completeness
            agg.avg_reconcile += result.metrics.reconcile_rate
            if result.metrics.document_count > 1:
                agg.false_splits += 1

    for agg in aggregates.values():
        if agg.runs:
            agg.avg_completeness /= agg.runs
            agg.avg_reconcile /= agg.runs

    ranking = sorted(aggregates.values(), key=lambda a: a.avg_score, reverse=True)
    winner = ranking[0].variant if ranking else "V1"

    return {
        "client": CAST_UNITY_CLIENT,
        "hermetic": hermetic,
        "fixtures": [f[0] for f in fixtures],
        "variants": [v.value for v in variants],
        "results": results,
        "ranking": [
            {
                "variant": a.variant,
                "avg_score": round(a.avg_score, 3),
                "avg_completeness": round(a.avg_completeness, 3),
                "avg_reconcile": round(a.avg_reconcile, 3),
                "false_splits": a.false_splits,
            }
            for a in ranking
        ],
        "winner": winner,
    }


def write_markdown_report(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# Extraction Tournament Round 1",
        "",
        f"Client: {report.get('client')}",
        f"Hermetic: {report.get('hermetic')}",
        f"**Winner: {report.get('winner')}**",
        "",
        "## Ranking",
        "",
        "| Variant | Avg score | Completeness | Reconcile | False splits |",
        "|---------|-----------|--------------|-----------|--------------|",
    ]
    for row in report.get("ranking") or []:
        lines.append(
            f"| {row['variant']} | {row['avg_score']} | {row['avg_completeness']} "
            f"| {row['avg_reconcile']} | {row['false_splits']} |"
        )
    lines.extend(["", "## Per fixture × variant", ""])
    for r in report.get("results") or []:
        lines.append(
            f"- **{r['fixture_id']}** / {r['variant']}: score={r['score']} "
            f"completeness={r['completeness']} docs={r['document_count']} "
            f"inv#={r['metrics']['has_invoice_number']} total={r['metrics']['has_doc_total']}"
        )
    lines.extend([
        "",
        "## Proposed rubric (post-tournament)",
        "",
        "- 50% header completeness (invoice #, date, lines, doc_total)",
        "- 30% reconcile rate",
        "- 20% line presence",
        "- Penalty: −0.15 per false document split",
        "- Penalty: −0.05 per needs_fx_review flag",
        "",
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Extraction variant tournament")
    parser.add_argument("--fixtures", default="sample_test_group")
    parser.add_argument("--variants", default="V0,V1,V2,V3")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--hermetic", action="store_true")
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    variant_map = {v.value: v for v in ALL_VARIANTS}
    variants = [variant_map[v.strip().upper()] for v in args.variants.split(",") if v.strip()]

    ctx = ExtractionContext(client_name=CAST_UNITY_CLIENT, model=args.model)
    report = run_tournament(variants=variants, hermetic=args.hermetic, context=ctx)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    write_markdown_report(report, RUBRIC_PATH)
    BASELINE_PATH.write_text(json.dumps({"winner": report["winner"], "ranking": report["ranking"]}, indent=2) + "\n")

    print(f"Winner: {report['winner']}")
    for row in report["ranking"]:
        print(
            f"  {row['variant']}: score={row['avg_score']} "
            f"completeness={row['avg_completeness']} splits={row['false_splits']}"
        )
    print(f"Report: {args.output}")
    print(f"Rubric: {RUBRIC_PATH}")


if __name__ == "__main__":
    main()
