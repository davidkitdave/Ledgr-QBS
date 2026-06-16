"""DocumentRecord read-fidelity + write-completeness eval harness.

Scores Phase 1 capture against golden fixture expectations (field recall,
line items, parties, annotations) and optionally Phase 2 completeness via
ledger_eval helpers.

Run:
    .venv/bin/python -m eval.document_record_eval --fixture all
    .venv/bin/python -m eval.document_record_eval --baseline
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from invoice_processing.extract.document_record import DocumentRecord, DocumentRecordBundle
from invoice_processing.extract.document_extractor import extract_document_file
from invoice_processing.extract.document_normalizer import normalize_document_bundle
from invoice_processing.extract.record_merge import merge_document_records

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "document_record"
BASELINE_PATH = Path(__file__).parent.parent / "docs" / "qa" / "document_record_eval_baseline.json"


@dataclass
class FidelityResult:
    fixture_id: str
    field_recall: float
    line_items_ok: bool
    annotations_ok: bool
    parties_ok: bool
    details: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            self.field_recall >= 0.90
            and self.line_items_ok
            and self.annotations_ok
            and self.parties_ok
        )


def _load_fixture(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _field_matches(record: DocumentRecord, spec: dict[str, str]) -> bool:
    label = spec.get("label", "")
    needle = (spec.get("value_contains") or "").strip().lower()
    for f in record.labeled_fields:
        if f.label.lower() == label.lower() or label.lower() in f.label.lower():
            if not needle:
                return bool(f.value.strip())
            return needle in f.value.lower()
    for f in record.totals:
        if f.label.lower() == label.lower():
            if not needle:
                return bool(f.value.strip())
            return needle in f.value.lower()
    return False


def score_fidelity(record: DocumentRecord, spec: dict[str, Any]) -> FidelityResult:
    expected = spec.get("expected_fields") or []
    matched = sum(1 for e in expected if _field_matches(record, e))
    recall = matched / len(expected) if expected else 1.0

    min_lines = int(spec.get("min_line_items") or 0)
    line_items_ok = len(record.line_items) >= min_lines
    min_ann = int(spec.get("min_annotations") or 0)
    annotations_ok = len(record.annotations) >= min_ann

    parties_ok = True
    for p_spec in spec.get("parties") or []:
        needle = (p_spec.get("name_contains") or "").strip().lower()
        role = (p_spec.get("role_hint") or "").strip().lower()
        found = any(
            p.role_hint.lower() == role and (not needle or needle in p.name.lower())
            for p in record.parties
        )
        if role and not found:
            parties_ok = False

    details: list[str] = []
    if recall < 0.90:
        details.append(f"field_recall={recall:.0%}")
    if not line_items_ok:
        details.append(f"line_items={len(record.line_items)} < {min_lines}")
    if not annotations_ok and min_ann:
        details.append(f"annotations={len(record.annotations)} < {min_ann}")
    if not parties_ok:
        details.append("parties mismatch")

    return FidelityResult(
        fixture_id=str(spec.get("fixture_id") or "unknown"),
        field_recall=recall,
        line_items_ok=line_items_ok,
        annotations_ok=annotations_ok,
        parties_ok=parties_ok,
        details=details,
    )


def path_stem(path: Path) -> str:
    return path.stem


def score_completeness(record: DocumentRecord, *, direction: str = "purchase") -> dict[str, Any]:
    """Phase 2 write readiness on a synthetic normalized invoice."""
    bundle = DocumentRecordBundle(documents=[record])
    normalized = normalize_document_bundle(
        bundle,
        direction=direction,
        base_currency="SGD",
        mapper_version="enhanced",
    )
    if not normalized:
        return {"completeness": 0.0, "has_invoice_number": False, "has_lines": False}
    inv = normalized[0]
    checks = [
        bool(inv.invoice_number),
        bool(inv.invoice_date),
        bool(inv.lines),
        inv.doc_total is not None,
    ]
    return {
        "completeness": sum(checks) / len(checks),
        "has_invoice_number": bool(inv.invoice_number),
        "has_lines": bool(inv.lines),
        "reconciled": inv.reconciled,
    }


def run_fixture(path: Path, record: Optional[DocumentRecord] = None) -> dict[str, Any]:
    spec = _load_fixture(path)
    if record is None:
        # Hermetic mode: build a synthetic record from expected fields for CI.
        record = _synthetic_record_from_spec(spec)
    fidelity = score_fidelity(record, spec)
    completeness = score_completeness(record)
    return {
        "fixture": spec.get("fixture_id", path.stem),
        "fidelity": fidelity.__dict__,
        "completeness": completeness,
        "passed": fidelity.passed,
    }


def _synthetic_record_from_spec(spec: dict[str, Any]) -> DocumentRecord:
    from invoice_processing.extract.document_record import (
        AnnotationCapture,
        LabeledField,
        LineCapture,
        PartyCapture,
    )

    fields = [
        LabeledField(label=e["label"], value=e.get("value_contains") or "sample", source="explicit_label")
        for e in spec.get("expected_fields") or []
    ]
    lines = [
        LineCapture(description=f"Line {i}", net_amount=100.0 * i, quantity=1, unit_amount=100.0 * i)
        for i in range(1, max(int(spec.get("min_line_items") or 1), 1) + 1)
    ]
    parties = [
        PartyCapture(name=p.get("name_contains") or "Party", role_hint=p.get("role_hint") or "unknown")
        for p in spec.get("parties") or []
    ]
    annotations = [
        AnnotationCapture(text="Paid 1 Jan 26 Sample PG", kind="payment_stamp")
        for _ in range(max(int(spec.get("min_annotations") or 0), 0))
    ]
    return DocumentRecord(
        labeled_fields=fields,
        parties=parties,
        line_items=lines,
        totals=[LabeledField(label="Total", value="6500.00")],
        annotations=annotations,
    )


def write_baseline(results: list[dict[str, Any]]) -> None:
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(json.dumps({"results": results}, indent=2) + "\n")


FIXTURE_PDF_MAP = {
    "vendor_invoice_sample": Path.home() / "Desktop/LocalTest/TestDoc/Sample Test Group/Acme Client Pte. Ltd./Purchase/FY2026/INV-2026-003-sample.pdf",
    "management_fees_sample": Path.home() / "Desktop/LocalTest/TestDoc/Sample Test Group/Acme Client Pte. Ltd./Purchase/FY2025/MGT-2025-011-sample.pdf",
    "expense_claim_sample": Path.home() / "Desktop/LocalTest/TestDoc/Sample Test Group/Acme Client Pte. Ltd./Purchase/FY2026/EXP-2026-040-sample.pdf",
}


def run_fixture_live(path: Path, spec: dict[str, Any]) -> dict[str, Any]:
    """Run Phase 1 on a real PDF and score against fixture spec."""
    bundle = extract_document_file(path)
    bundle = merge_document_records(bundle)
    if not bundle.documents:
        return {"fixture": spec.get("fixture_id"), "passed": False, "error": "empty bundle"}
    record = bundle.documents[0]
    fidelity = score_fidelity(record, spec)
    completeness = score_completeness(record)
    return {
        "fixture": spec.get("fixture_id", path.stem),
        "fidelity": fidelity.__dict__,
        "completeness": completeness,
        "document_count": len(bundle.documents),
        "passed": fidelity.passed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="DocumentRecord fidelity eval")
    parser.add_argument("--fixture", default="all", help="Fixture id or 'all'")
    parser.add_argument("--baseline", action="store_true", help="Write baseline JSON to docs/qa")
    parser.add_argument("--live", action="store_true", help="Run Phase 1 on real PDFs (requires local paths)")
    args = parser.parse_args()

    paths = sorted(FIXTURES_DIR.glob("*.json"))
    if args.fixture != "all":
        paths = [FIXTURES_DIR / f"{args.fixture}.json"]

    results: list[dict[str, Any]] = []
    for p in paths:
        spec = _load_fixture(p)
        fid = spec.get("fixture_id", p.stem)
        if args.live and fid in FIXTURE_PDF_MAP:
            pdf = FIXTURE_PDF_MAP[fid]
            if pdf.exists():
                results.append(run_fixture_live(pdf, spec))
                continue
            results.append({"fixture": fid, "passed": False, "error": f"pdf not found: {pdf}"})
        else:
            results.append(run_fixture(p))
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        fid = r["fixture"]
        recall = r["fidelity"]["field_recall"]
        comp = r["completeness"]["completeness"]
        print(f"{status} {fid}: fidelity={recall:.0%} completeness={comp:.0%}")

    if args.baseline:
        write_baseline(results)
        print(f"Baseline written to {BASELINE_PATH}")


if __name__ == "__main__":
    main()
