"""Ledger document-processing evaluation harness.

Measures pipeline health and accuracy-proxy metrics across a sample of
invoice / receipt PDFs:
  - classification rate (doc_type resolved, not "unknown" / error)
  - reconciliation pass-rate (among invoice/receipt docs)
  - COA categorization fill-rate (lines with account_code populated)
  - direction resolution rate (sales-vs-purchase resolved, not "unknown")
  - per-target completeness: per-required-header fill for each export
    target (QBS Ledger + Xero), aligned to ADR-0005's completeness contract

Run:
    uv run python -m eval.ledger_eval [--limit 6] [--root /path/to/TestDoc]

The ``run_eval`` function is hermetically testable — inject a stub
``process_fn`` to avoid any Gemini / network calls.
"""

from __future__ import annotations

import argparse
import os
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from invoice_processing.export.exporters import (
    QbsLedgerExporter,
    XeroLedgerExporter,
    _is_empty,
)

# ---------------------------------------------------------------------------
# Public data models
# ---------------------------------------------------------------------------

@dataclass
class DocEval:
    path: str
    doc_type: str
    direction: Optional[str]
    reconciled: bool
    n_lines: int
    n_lines_with_account: int       # account_code filled
    tax_treatments: list[str]
    note: str
    error: Optional[str]


@dataclass
class HeaderFill:
    """Fill tally for one required header in one export target."""
    header: str
    filled: int = 0
    total: int = 0

    @property
    def rate(self) -> float:
        return self.filled / self.total if self.total else 0.0


@dataclass
class TargetCompleteness:
    """Per-required-header fill table for one export target (QBS or Xero).

    ``headers`` is keyed by required-header name (preserving exporter order);
    each :class:`HeaderFill` tallies non-empty cells across every exported row
    of every processed NormalizedInvoice. ``n_rows`` is the total rows tallied.
    """
    target: str                     # "QBS Ledger" | "Xero"
    headers: dict[str, HeaderFill] = field(default_factory=dict)
    n_rows: int = 0


@dataclass
class EvalReport:
    n_docs: int
    classify_ok: int                # doc_type resolved (not "unknown" / error)
    recon_pass: int                 # reconciled True among invoices / receipts
    recon_rate: float
    categorized_lines: int
    total_lines: int
    categorization_fill_rate: float
    errors: int
    # Direction resolution (among doc_type == "invoice")
    direction_eligible: int = 0     # invoices considered for direction
    direction_resolved: int = 0     # direction in {sales, purchase}
    direction_rate: float = 0.0
    # Per-target completeness tables, keyed by target name ("QBS Ledger"/"Xero")
    completeness: dict[str, TargetCompleteness] = field(default_factory=dict)
    docs: list[DocEval] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core evaluation logic (injectable / hermetically testable)
# ---------------------------------------------------------------------------

_INVOICE_RECEIPT_TYPES = {"invoice", "receipt"}
_UNKNOWN_TYPES = {"unknown", ""}
_RESOLVED_DIRECTIONS = {"sales", "purchase"}


def _tally_completeness(
    table: TargetCompleteness,
    exporter,
    inv,
    side: str,
) -> None:
    """Build *inv*'s export rows for *exporter* and tally each required header.

    *side* is the exporter doc_type ("purchase" / "sales"). Uses the exporter's
    OWN ``required_fields`` + ``rows`` so the header contract is never re-derived
    here. A required header is "filled" on a row when its cell is non-empty
    (per the exporter's ``_is_empty``). Aggregates in place into *table*.
    """
    required = exporter.required_fields(side)
    rows = exporter.rows([inv], side)
    for col in required:
        hf = table.headers.get(col)
        if hf is None:
            hf = HeaderFill(header=col)
            table.headers[col] = hf
    for row in rows:
        table.n_rows += 1
        for col in required:
            hf = table.headers[col]
            hf.total += 1
            if not _is_empty(row.get(col, "")):
                hf.filled += 1


def run_eval(
    paths: list[str | Path],
    client,
    *,
    process_fn: Callable = None,
) -> EvalReport:
    """Run ``process_fn(path, client)`` for each path and aggregate metrics.

    A document that raises an exception becomes a :class:`DocEval` with
    ``error`` set and is counted in ``errors``; it never crashes the run.

    Args:
        paths:      Iterable of PDF file paths.
        client:     A :class:`~invoice_processing.export.client_context.ClientContext`.
        process_fn: Callable ``(path, client) -> ProcessedDoc``.  Defaults to
                    the real ``invoice_processing.pipeline.process_document``.

    Returns:
        An :class:`EvalReport` with aggregate metrics and per-doc detail.
    """
    if process_fn is None:
        from invoice_processing.pipeline import process_document
        process_fn = process_document

    doc_evals: list[DocEval] = []

    classify_ok = 0
    recon_pass = 0
    recon_eligible = 0      # only invoice / receipt docs count for recon
    categorized_lines = 0
    total_lines = 0
    errors = 0

    direction_eligible = 0
    direction_resolved = 0

    qbs_exporter = QbsLedgerExporter()
    xero_exporter = XeroLedgerExporter()
    qbs_table = TargetCompleteness(target="QBS Ledger")
    xero_table = TargetCompleteness(target="Xero")

    for p in paths:
        path_str = str(p)
        try:
            doc = process_fn(path_str, client)

            # Detect errors recorded inside the ProcessedDoc (pipeline catches internally)
            doc_error: Optional[str] = None
            if doc.note and doc.note.startswith("ERROR"):
                doc_error = doc.note
                errors += 1
            else:
                # Classification OK: doc_type is resolved and not "unknown"
                dt = (doc.doc_type or "").strip().lower()
                if dt and dt not in _UNKNOWN_TYPES:
                    classify_ok += 1

                # Reconciliation: only invoice / receipt docs
                if dt in _INVOICE_RECEIPT_TYPES:
                    recon_eligible += 1
                    if doc.reconciled:
                        recon_pass += 1

                # Direction resolution: among invoices (doc_type == "invoice")
                dir_norm = (doc.direction or "").strip().lower()
                if dt == "invoice":
                    direction_eligible += 1
                    if dir_norm in _RESOLVED_DIRECTIONS:
                        direction_resolved += 1

                # Categorization lines
                if doc.normalized is not None:
                    for line in doc.normalized.lines:
                        total_lines += 1
                        if line.account_code:
                            categorized_lines += 1

                # Per-target completeness: evaluate BOTH exporters for the
                # appropriate side. Unknown direction is evaluated as purchase
                # (but does NOT get direction credit above).
                if doc.normalized is not None:
                    side = dir_norm if dir_norm in _RESOLVED_DIRECTIONS else "purchase"
                    _tally_completeness(qbs_table, qbs_exporter, doc.normalized, side)
                    _tally_completeness(xero_table, xero_exporter, doc.normalized, side)

            # Per-doc tax treatments
            tax_treatments: list[str] = []
            n_lines = 0
            n_lines_with_account = 0
            if doc.normalized is not None:
                for line in doc.normalized.lines:
                    n_lines += 1
                    if line.account_code:
                        n_lines_with_account += 1
                    if line.tax_treatment:
                        tax_treatments.append(line.tax_treatment)

            doc_evals.append(DocEval(
                path=path_str,
                doc_type=doc.doc_type or "unknown",
                direction=doc.direction,
                reconciled=doc.reconciled,
                n_lines=n_lines,
                n_lines_with_account=n_lines_with_account,
                tax_treatments=tax_treatments,
                note=doc.note or "",
                error=doc_error,
            ))

        except Exception as exc:  # noqa: BLE001
            errors += 1
            tb = traceback.format_exc()
            doc_evals.append(DocEval(
                path=path_str,
                doc_type="unknown",
                direction=None,
                reconciled=False,
                n_lines=0,
                n_lines_with_account=0,
                tax_treatments=[],
                note=f"ERROR: {exc}",
                error=f"{exc}\n{tb}",
            ))

    n_docs = len(doc_evals)

    # Recon rate: among recon-eligible docs; 0.0 when none eligible
    recon_rate = recon_pass / recon_eligible if recon_eligible else 0.0

    # Fill rate: lines with account_code / total lines; 0.0 when no lines
    fill_rate = categorized_lines / total_lines if total_lines else 0.0

    # Direction rate: resolved / eligible (invoices); 0.0 when none eligible
    direction_rate = direction_resolved / direction_eligible if direction_eligible else 0.0

    return EvalReport(
        n_docs=n_docs,
        classify_ok=classify_ok,
        recon_pass=recon_pass,
        recon_rate=recon_rate,
        categorized_lines=categorized_lines,
        total_lines=total_lines,
        categorization_fill_rate=fill_rate,
        errors=errors,
        direction_eligible=direction_eligible,
        direction_resolved=direction_resolved,
        direction_rate=direction_rate,
        completeness={"QBS Ledger": qbs_table, "Xero": xero_table},
        docs=doc_evals,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BANK_KEYWORDS = ("bank", "statement", "bankstatement", "bsmt")


def _looks_like_bank(path: Path) -> bool:
    """Heuristic: skip obvious bank-statement PDFs."""
    name_lower = path.stem.lower()
    return any(kw in name_lower for kw in _BANK_KEYWORDS)


def discover_samples(
    root: str | Path = "~/Desktop/LocalTest/TestDoc",
    limit: int = 6,
) -> list[str]:
    """Glob a small set of invoice / receipt PDFs under *root*.

    Searches ``GST SR:ZR/`` and ``MYDoc/`` subdirectories first; falls back
    to any PDF found under *root*. Skips obvious bank-statement files.
    Caps at *limit* paths.

    Args:
        root:  Root directory to search (tilde-expanded).
        limit: Maximum number of paths to return.

    Returns:
        A list of absolute path strings (≤ *limit* entries).
    """
    root = Path(root).expanduser().resolve()
    found: list[Path] = []

    # Priority dirs: invoice / receipt areas
    priority_patterns = [
        root / "**" / "GST SR*" / "*.pdf",
        root / "**" / "GST ZR*" / "*.pdf",
        root / "**" / "MYDoc" / "**" / "*.pdf",
        root / "**" / "Invoice*" / "*.pdf",
        root / "**" / "Receipt*" / "*.pdf",
    ]

    seen: set[str] = set()
    for pattern in priority_patterns:
        for p in sorted(Path(root).glob(str(pattern.relative_to(root)))):
            if str(p) not in seen and not _looks_like_bank(p):
                seen.add(str(p))
                found.append(p)
            if len(found) >= limit:
                break
        if len(found) >= limit:
            break

    # Fallback: any PDF under root (excluding bank statements)
    if len(found) < limit:
        for p in sorted(root.rglob("*.pdf")):
            if str(p) not in seen and not _looks_like_bank(p):
                seen.add(str(p))
                found.append(p)
            if len(found) >= limit:
                break

    return [str(p) for p in found[:limit]]


def default_client():
    """Return a ready-to-use :class:`ClientContext` seeded with the standard COA.

    Uses:
        - ``fye_month=3`` (March year-end)
        - ``accounting_software="QBS Ledger"``
        - ``tax_registered=True``
        - ``coa`` seeded from :func:`~app.coa_ingest.standard_coa_rows`
    """
    from app.coa_ingest import standard_coa_rows
    from invoice_processing.export.client_context import ClientContext, CoaAccount

    rows = standard_coa_rows()
    coa = [
        CoaAccount(
            code=r.get("code") or None,
            description=r.get("description") or "",
            account_type=r.get("account_type") or None,
            financial_statement=r.get("financial_statement") or None,
            nature=r.get("nature") or None,
            keywords=r.get("keywords") or None,
        )
        for r in rows
    ]
    return ClientContext(
        client_id="eval-default",
        client_name="Eval Default Client",
        fye_month=3,
        accounting_software="QBS Ledger",
        tax_registered=True,
        coa=coa,
    )


# ---------------------------------------------------------------------------
# Pretty-print report
# ---------------------------------------------------------------------------

def _print_report(report: EvalReport) -> None:
    print()
    print("=" * 100)
    print("LEDGER EVAL REPORT")
    print("=" * 100)
    print(
        f"{'Path':<55} {'Type':<14} {'Dir':<10} {'Rec':>4} "
        f"{'Lines':>6} {'w/Acct':>7} {'Treatments':<20} {'Note'}"
    )
    print("-" * 100)
    for d in report.docs:
        rec_s = "YES" if d.reconciled else "no"
        treats = ",".join(sorted(set(d.tax_treatments))) or "-"
        note_s = (d.error or d.note or "")[:40]
        print(
            f"{d.path[-55:]:<55} {d.doc_type:<14} {(d.direction or '-'):<10} "
            f"{rec_s:>4} {d.n_lines:>6} {d.n_lines_with_account:>7} "
            f"{treats:<20} {note_s}"
        )
    print("=" * 100)
    print()
    print("AGGREGATE METRICS")
    print(f"  Documents processed:          {report.n_docs}")
    print(f"  Classify OK (not unknown):    {report.classify_ok} / {report.n_docs}")
    print(f"  Recon pass (invoice/receipt): {report.recon_pass}  rate={report.recon_rate*100:.1f}%")
    print(f"  Lines with account_code:      {report.categorized_lines} / {report.total_lines}"
          f"  fill={report.categorization_fill_rate*100:.1f}%")
    print(f"  Errors:                       {report.errors}")
    print()

    # ------------------------------------------------------------------ #
    # DIRECTION (sales-vs-purchase resolution among invoices)
    # ------------------------------------------------------------------ #
    print("DIRECTION")
    print(
        f"  direction_resolved {report.direction_resolved}/{report.direction_eligible} "
        f"rate={report.direction_rate*100:.1f}%"
        + ("  (no invoice docs)" if report.direction_eligible == 0 else "")
    )
    print()

    # ------------------------------------------------------------------ #
    # COMPLETENESS (per target) — per-required-header fill table
    # ------------------------------------------------------------------ #
    print("COMPLETENESS (per target)")
    print("  Per-required-header fill across all exported rows (filled/total, rate%).")
    for target_name in ("QBS Ledger", "Xero"):
        table = report.completeness.get(target_name)
        if table is None:
            continue
        print()
        print(f"  [{table.target}]  rows={table.n_rows}")
        if not table.headers:
            print("    (no NormalizedInvoices to evaluate)")
            continue
        print(f"    {'Header':<22} {'Filled/Total':>14} {'Rate':>8}")
        print(f"    {'-'*22} {'-'*14:>14} {'-'*8:>8}")
        for hf in table.headers.values():
            ft = f"{hf.filled}/{hf.total}"
            print(f"    {hf.header:<22} {ft:>14} {hf.rate*100:>7.1f}%")
    print()

    # Verdict
    if report.n_docs == 0:
        print("VERDICT: No documents processed.")
    elif report.errors == report.n_docs:
        print("VERDICT: ALL documents errored — check pipeline configuration.")
    else:
        classify_rate = report.classify_ok / report.n_docs if report.n_docs else 0
        verdicts = []
        if classify_rate >= 0.80:
            verdicts.append(f"classify OK ({classify_rate*100:.0f}%)")
        else:
            verdicts.append(f"classify BELOW 80% ({classify_rate*100:.0f}%)")
        if report.recon_rate >= 0.80:
            verdicts.append(f"recon OK ({report.recon_rate*100:.0f}%)")
        elif report.recon_pass == 0 and report.recon_rate == 0.0:
            verdicts.append("recon n/a (no invoice/receipt docs)")
        else:
            verdicts.append(f"recon BELOW 80% ({report.recon_rate*100:.0f}%)")
        print("VERDICT: " + " | ".join(verdicts))
    print()


# ---------------------------------------------------------------------------
# __main__ runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ledger document eval harness")
    parser.add_argument(
        "--limit",
        type=int,
        default=6,
        help="Max number of PDFs to evaluate (default: 6)",
    )
    parser.add_argument(
        "--root",
        type=str,
        default="~/Desktop/LocalTest/TestDoc",
        help="Root directory to discover sample PDFs",
    )
    args = parser.parse_args()

    # Load .env before any AI-client imports (mirrors bank_eval.py)
    from dotenv import load_dotenv
    load_dotenv()

    # Force AI Studio dev mode (avoid Vertex quota during eval)
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "FALSE"

    client = default_client()
    paths = discover_samples(root=args.root, limit=args.limit)

    if not paths:
        print(f"[WARN] No sample PDFs found under: {args.root}")
        print("       Pass --root to specify a directory containing invoice/receipt PDFs.")
    else:
        print(f"Evaluating {len(paths)} document(s) from: {args.root}")
        for p in paths:
            print(f"  {p}")

    report = run_eval(paths, client)
    _print_report(report)
