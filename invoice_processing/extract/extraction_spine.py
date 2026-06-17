"""Shared two-phase extraction spine — production, eval, and tournament.

All paths call this module so pipeline, ADK nodes, and tournament variants
stay aligned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from ..export.models import NormalizedInvoice
from .document_extractor import (
    PHASE1_PROMPT,
    extract_document_bundle,
    mime_for,
)
from .document_normalizer import normalize_document_bundle
from .document_record import DocumentRecordBundle
from .record_merge import merge_document_records

SAMPLE_TEST_CLIENT = "Company-A"
SAMPLE_TEST_BASE = Path.home() / "Desktop/LocalTest/TestDoc/Sample Test Group" / SAMPLE_TEST_CLIENT
SAMPLE_TEST_DOCS = [
    SAMPLE_TEST_BASE / "Purchase/FY2026/INV-2026-003-sample.pdf",
    SAMPLE_TEST_BASE / "Purchase/FY2025/INV-2025-012-sample.pdf",
    SAMPLE_TEST_BASE / "Purchase/FY2025/MGT-2025-011-sample.pdf",
    SAMPLE_TEST_BASE / "Purchase/FY2026/EXP-2026-040-sample.pdf",
]

PHASE1_PROMPT_SEGMENTATION = PHASE1_PROMPT  # production winner (V3) merged into PHASE1_PROMPT


class ExtractionVariant(str, Enum):
    V0 = "V0"  # baseline
    V1 = "V1"  # enhanced normalizer
    V2 = "V2"  # V1 + package merge
    V3 = "V3"  # V1 + segmentation prompt


@dataclass
class ExtractionContext:
    direction: str = "purchase"
    our_gst_registered: bool = True
    client_country: str = "SG"
    base_currency: str = "SGD"
    client_name: Optional[str] = None
    client_uen: Optional[str] = None
    model: Optional[str] = None
    hint: Optional[str] = None


@dataclass
class LedgerMetrics:
    document_count: int = 0
    invoice_count: int = 0
    completeness: float = 0.0
    reconciled_count: int = 0
    fx_review_count: int = 0
    has_invoice_number: bool = False
    has_invoice_date: bool = False
    has_doc_total: bool = False
    has_lines: bool = False
    details: list[str] = field(default_factory=list)

    @property
    def reconcile_rate(self) -> float:
        if self.invoice_count == 0:
            return 0.0
        return self.reconciled_count / self.invoice_count

    @property
    def score(self) -> float:
        """Tournament composite — higher is better."""
        split_penalty = max(0, self.document_count - 1) * 0.15
        fx_penalty = self.fx_review_count * 0.05
        return (
            self.completeness * 0.5
            + self.reconcile_rate * 0.3
            + (0.2 if self.has_lines else 0.0)
            - split_penalty
            - fx_penalty
        )


@dataclass
class ExtractionResult:
    variant: ExtractionVariant
    path: str
    bundle: DocumentRecordBundle
    normalized: list[NormalizedInvoice]
    metrics: LedgerMetrics
    error: Optional[str] = None


def _prompt_for_variant(variant: ExtractionVariant) -> str:
    if variant == ExtractionVariant.V3:
        return PHASE1_PROMPT_SEGMENTATION
    return PHASE1_PROMPT


def _mapper_for_variant(variant: ExtractionVariant) -> str:
    if variant in (ExtractionVariant.V1, ExtractionVariant.V2, ExtractionVariant.V3):
        return "enhanced"
    return "baseline"


def _apply_merge(bundle: DocumentRecordBundle, variant: ExtractionVariant) -> DocumentRecordBundle:
    if variant == ExtractionVariant.V2:
        return merge_document_records(bundle)
    return bundle


def score_normalized(invoices: list[NormalizedInvoice], document_count: int) -> LedgerMetrics:
    if not invoices:
        return LedgerMetrics(document_count=document_count, details=["no_invoices"])

    checks_per: list[list[bool]] = []
    reconciled = 0
    fx = 0
    for inv in invoices:
        checks_per.append([
            bool(inv.invoice_number),
            bool(inv.invoice_date),
            bool(inv.lines),
            inv.doc_total is not None,
        ])
        if inv.reconciled:
            reconciled += 1
        if inv.needs_fx_review:
            fx += 1

    flat = [c for row in checks_per for c in row]
    completeness = sum(flat) / len(flat) if flat else 0.0
    primary = invoices[0]

    details: list[str] = []
    if document_count > 1:
        details.append(f"false_split: {document_count} documents")
    if not primary.invoice_number:
        details.append("missing invoice_number")
    if not primary.doc_total:
        details.append("missing doc_total")
    if fx:
        details.append(f"fx_review={fx}")

    return LedgerMetrics(
        document_count=document_count,
        invoice_count=len(invoices),
        completeness=completeness,
        reconciled_count=reconciled,
        fx_review_count=fx,
        has_invoice_number=bool(primary.invoice_number),
        has_invoice_date=bool(primary.invoice_date),
        has_doc_total=primary.doc_total is not None,
        has_lines=bool(primary.lines),
        details=details,
    )


def run_extraction_spine(
    data: bytes,
    mime_type: str,
    *,
    variant: ExtractionVariant = ExtractionVariant.V0,
    context: Optional[ExtractionContext] = None,
    bundle: Optional[DocumentRecordBundle] = None,
) -> ExtractionResult:
    """Run Phase 1 (+ optional merge) → Phase 2 for one upload."""
    ctx = context or ExtractionContext()
    mapper = _mapper_for_variant(variant)

    if bundle is None:
        prompt = _prompt_for_variant(variant)
        bundle = extract_document_bundle(
            data,
            mime_type,
            model=ctx.model,
            hint=ctx.hint,
            phase1_prompt=prompt,
        )

    doc_count_before = len(bundle.documents)
    bundle = _apply_merge(bundle, variant)

    normalized = normalize_document_bundle(
        bundle,
        direction=ctx.direction,
        our_gst_registered=ctx.our_gst_registered,
        client_country=ctx.client_country,
        base_currency=ctx.base_currency,
        client_name=ctx.client_name,
        client_uen=ctx.client_uen,
        mapper_version=mapper,
    )

    metrics = score_normalized(normalized, doc_count_before)
    return ExtractionResult(
        variant=variant,
        path="",
        bundle=bundle,
        normalized=normalized,
        metrics=metrics,
    )


def run_extraction_file(
    path: str | Path,
    *,
    variant: ExtractionVariant = ExtractionVariant.V0,
    context: Optional[ExtractionContext] = None,
) -> ExtractionResult:
    path = Path(path)
    data = path.read_bytes()
    mime = mime_for(path)
    result = run_extraction_spine(data, mime, variant=variant, context=context)
    result.path = str(path)
    return result


def result_to_dict(result: ExtractionResult) -> dict[str, Any]:
    invs = []
    for inv in result.normalized:
        invs.append({
            "invoice_number": inv.invoice_number,
            "invoice_date": str(inv.invoice_date) if inv.invoice_date else None,
            "supplier": inv.supplier.name if inv.supplier else None,
            "currency": inv.currency,
            "doc_total": inv.doc_total,
            "needs_fx_review": inv.needs_fx_review,
            "reconciled": inv.reconciled,
            "reconcile_note": inv.reconcile_note,
            "line_count": len(inv.lines),
        })
    return {
        "variant": result.variant.value,
        "path": result.path,
        "error": result.error,
        "document_count": result.metrics.document_count,
        "invoice_count": result.metrics.invoice_count,
        "completeness": round(result.metrics.completeness, 3),
        "reconcile_rate": round(result.metrics.reconcile_rate, 3),
        "score": round(result.metrics.score, 3),
        "metrics": {
            "has_invoice_number": result.metrics.has_invoice_number,
            "has_invoice_date": result.metrics.has_invoice_date,
            "has_doc_total": result.metrics.has_doc_total,
            "has_lines": result.metrics.has_lines,
            "fx_review_count": result.metrics.fx_review_count,
            "details": result.metrics.details,
        },
        "phase2": invs,
    }
