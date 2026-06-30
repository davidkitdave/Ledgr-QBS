#!/usr/bin/env python3
"""Control experiment: minimal one-call extraction vs current pipeline.

Compares the SAME PDF (Starhub 18p/4.4MB or multi-receipt 35p/19.4MB) processed via:

- **Path A (MINIMAL)** — one ``generate_content`` call with the whole PDF inline,
  a simple prompt, and a schema that keeps ``tax_lines[]`` (the clean GST
  breakdown). Uses the same model (``make_client`` + ``lite_model()``) as
  production so the comparison is fair.

- **Path B (CURRENT)** — ``process_document_batch`` (the heavy
  ``invoice_processing`` pipeline that chunks at >10 pages and runs a 75-line
  prompt). Path B uses the playground context + an in-memory credit grant so
  the real engine runs end-to-end.

Prints and writes side-by-side JSON so you can see whether the minimal path
beats the factory on **doc count, line count, tax breakdown, Gemini-call count,
wall-clock**. This is the decisive evidence for whether to port the minimal
approach back into the real pipeline.

Usage::

    uv run python scripts/spike_minimal_extract_vs_pipeline.py \\
        --pdf "~/Desktop/localtest/TestDoc/GST SR:ZR/BV-0002830 Starhub 8.20057598B bill 122025.pdf"

    uv run python scripts/spike_minimal_extract_vs_pipeline.py \\
        --pdf "~/Desktop/localtest/multi receipt.pdf"

Requires GOOGLE_API_KEY (Path A) and either GOOGLE_API_KEY or ADC for Path B.
Output JSON is written to ``--out-dir`` (default ``~/Desktop/localtest/``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO / ".env")

from google.genai import types  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from invoice_processing.shared_libraries.genai_client import lite_model, make_client  # noqa: E402

DEFAULT_STARHUB = "/Users/davidkitdave/Desktop/localtest/TestDoc/GST SR:ZR/BV-0002830 Starhub 8.20057598B bill 122025.pdf"
DEFAULT_MULTI_RECEIPT = "/Users/davidkitdave/Desktop/localtest/multi receipt.pdf"
DEFAULT_OUT_DIR = "/Users/davidkitdave/Desktop/localtest"

# ---------------------------------------------------------------------------
# Path A — Minimal one-call schema. Captures the clean GST breakdown
# (tax_lines[]) directly, instead of forcing the model to enumerate per-call
# detail rows. Mirrors what Google Drive's Gemini sidebar surfaces.
# ---------------------------------------------------------------------------


class MinimalLine(BaseModel):
    description: str
    net_amount: float | None = None
    gst_amount: float | None = None
    tax_label: str | None = Field(default=None, description="e.g. SR, ZR, GST 9%")


class MinimalTaxLine(BaseModel):
    label: str = Field(description="Verbatim tax label as printed, e.g. GST 9%, 0%")
    rate: str | None = None
    base: float | None = None
    amount: float | None = None


class MinimalDocument(BaseModel):
    doc_type: str | None = None
    vendor: str | None = None
    reference: str | None = None
    date: str | None = None
    currency: str | None = None
    subtotal: float | None = None
    tax_total: float | None = None
    grand_total: float | None = None
    presentation: str | None = Field(default=None, description="summary|itemized")
    lines: list[MinimalLine] = Field(default_factory=list)
    tax_lines: list[MinimalTaxLine] = Field(
        default_factory=list,
        description="Every printed GST grouping (any count N, not forced to 2)",
    )


class MinimalBundle(BaseModel):
    documents: list[MinimalDocument] = Field(default_factory=list)
    skipped_pages: list[int] | None = None
    notes: str | None = None


# Short, no-telco-bias prompt. Place AFTER the document per Google's
# documented best practice (gemini-api/docs/document-processing).
MINIMAL_PROMPT = (
    "Extract this bill into the JSON schema. "
    "Fill `tax_lines` with every printed GST grouping exactly as shown "
    "(e.g. Standard Rated, Zero Rated, Exempt, with their amounts). "
    "Emit the summary charge rows as `lines` (one per printed breakdown row); "
    "do not emit appendix/detail sub-rows unless they are the only breakdown on the bill. "
    "Reconcile line nets + tax to `grand_total`."
)


def run_minimal(pdf: Path, *, model: str | None = None) -> dict:
    """One direct ``generate_content`` call with the whole PDF inline.

    Sets ``max_output_tokens=65536`` (Gemini 2.5 Flash-Lite max) so the full
    JSON for any reasonable invoice fits without truncation.
    """
    data = pdf.read_bytes()
    client = make_client()
    chosen_model = model or lite_model()
    part = types.Part.from_bytes(data=data, mime_type="application/pdf")
    t0 = time.perf_counter()
    resp = client.models.generate_content(
        model=chosen_model,
        contents=[part, MINIMAL_PROMPT],
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=MinimalBundle,
            max_output_tokens=65536,
            # Keep thinking off (matches default_llm_config in the repo).
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    elapsed = time.perf_counter() - t0
    bundle = MinimalBundle.model_validate_json(resp.text or "{}")
    return {
        "elapsed_seconds": round(elapsed, 2),
        "model": chosen_model,
        "bytes_sent": len(data),
        "gemini_call_count": 1,
        "bundle": bundle.model_dump(),
        "usage": _usage(resp),
    }


def _usage(resp: object) -> dict:
    """Pull token counts off a GenerateContentResponse when available."""
    meta = getattr(resp, "usage_metadata", None) or getattr(resp, "usage", None)
    if meta is None:
        return {}
    out: dict = {}
    for attr in (
        "prompt_token_count",
        "candidates_token_count",
        "thoughts_token_count",
        "total_token_count",
        "cached_content_token_count",
    ):
        val = getattr(meta, attr, None)
        if val is not None:
            out[attr] = val
    return out


# ---------------------------------------------------------------------------
# Path B — current pipeline (process_document_batch).
# Mirrors scripts/qa_credit_accuracy_localtest.py: grants in-memory credits,
# uses the playground context, runs the real engine end-to-end.
# ---------------------------------------------------------------------------


def _playground_state() -> dict:
    from invoice_processing.shared_libraries.playground_context import (
        playground_default_context,
    )

    state = playground_default_context().to_state()
    state.setdefault("firm_id", "T_PLAYGROUND")
    state.setdefault("slack_team_id", "T_PLAYGROUND")
    return state


def run_pipeline(pdf: Path, *, credits_grant: int = 500) -> dict:
    from app.credit_service import (  # noqa: WPS433 — lazy import keeps spike hermetic when app missing
        CreditService,
        InMemoryCreditStore,
    )
    from ledgr_agent.tools import document_tools  # noqa: WPS433
    from ledgr_agent.tools.document_tools import process_document_batch  # noqa: WPS433

    # Grant credits via the same singleton the engine reads.
    service = CreditService(InMemoryCreditStore())
    document_tools._credit_service_factory = lambda: service
    # Top up so the gate allows the run. CreditService.grant(store, amount, note).
    service.grant("T_PLAYGROUND", credits_grant, note="spike-a/b")

    t0 = time.perf_counter()
    batch = process_document_batch(
        SimpleNamespace(state=_playground_state()),
        paths=[str(pdf.resolve())],
    )
    elapsed = time.perf_counter() - t0

    posted = batch.get("posted_documents") or []
    export_rows = batch.get("export_rows") or []
    per_file = batch.get("per_file") or []
    return {
        "elapsed_seconds": round(elapsed, 2),
        "batch_status": batch.get("status"),
        "credits_used": (batch.get("credits") or {}).get("credits_used"),
        "balance_after": (batch.get("credits") or {}).get("credits_remaining"),
        "block_reason": (batch.get("validation_summary") or {}).get("block_reason"),
        "posted_document_count": len(posted),
        "export_row_count": len(export_rows),
        "per_file_count": len(per_file),
        "per_file_doc_types": [
            pf.get("doc_type") for pf in per_file if isinstance(pf, dict)
        ],
        "posted_documents": posted,
        "export_rows": export_rows,
        "per_file": per_file,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _fmt_minimal(result: dict) -> str:
    b = result.get("bundle") or {}
    docs = b.get("documents") or []
    lines = "\n".join(
        f"      - {ln.get('description')!r:60s} net={ln.get('net_amount')} "
        f"gst={ln.get('gst_amount')} tax_label={ln.get('tax_label')!r}"
        for ln in (docs[0].get("lines") if docs else []) or []
    )
    tax_lines = "\n".join(
        f"      - {tl.get('label')!r:20s} rate={tl.get('rate')!r} base={tl.get('base')} amount={tl.get('amount')}"
        for tl in (docs[0].get("tax_lines") if docs else []) or []
    )
    usage = result.get("usage") or {}
    usage_str = ", ".join(f"{k}={v}" for k, v in usage.items()) or "n/a"
    head = docs[0] if docs else {}
    return (
        f"  doc_count:               {len(docs)}\n"
        f"  presentation:            {head.get('presentation')}\n"
        f"  vendor / reference:      {head.get('vendor')!r} / {head.get('reference')!r}\n"
        f"  grand_total:             {head.get('grand_total')}\n"
        f"  tax_total:               {head.get('tax_total')}\n"
        f"  line_count:              {len(head.get('lines') or [])}\n"
        f"  lines:\n{lines or '      (none)'}\n"
        f"  tax_lines:\n{tax_lines or '      (none)'}\n"
        f"  gemini_call_count:       {result.get('gemini_call_count')}\n"
        f"  bytes_sent:              {result.get('bytes_sent'):,}\n"
        f"  elapsed_seconds:         {result.get('elapsed_seconds')}\n"
        f"  token_usage:             {usage_str}\n"
        f"  model:                   {result.get('model')}"
    )


def _fmt_pipeline(result: dict) -> str:
    rows = result.get("export_rows") or []
    doc_kind_rows = []
    for r in rows[:5]:  # first 5 only — could be many
        if not isinstance(r, dict):
            continue
        doc_kind_rows.append(
            f"      - {r.get('description','')!r:60s} "
            f"tax={r.get('tax_treatment')!r} acct={r.get('account_code')!r} "
            f"src={r.get('source_doc_id')!r}"
        )
    more = "" if len(rows) <= 5 else f"\n      ... ({len(rows) - 5} more)"
    types_str = ", ".join(
        t for t in (result.get("per_file_doc_types") or []) if t
    ) or "(none)"
    return (
        f"  batch_status:            {result.get('batch_status')}\n"
        f"  block_reason:            {result.get('block_reason')}\n"
        f"  posted_document_count:   {result.get('posted_document_count')}\n"
        f"  per_file_doc_types:      {types_str}\n"
        f"  export_row_count:        {result.get('export_row_count')}\n"
        f"  export_rows (first 5):\n"
        + "\n".join(doc_kind_rows or ["      (none)"])
        + more
        + "\n"
        f"  credits_used_reported:   {result.get('credits_used')}\n"
        f"  balance_after_reported:  {result.get('balance_after')}\n"
        f"  elapsed_seconds:         {result.get('elapsed_seconds')}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pdf",
        type=Path,
        default=Path(DEFAULT_STARHUB),
        help="PDF to test (default: Starhub bill)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(DEFAULT_OUT_DIR),
        help="Where to write *_minimal.json and *_pipeline.json",
    )
    parser.add_argument(
        "--skip-pipeline",
        action="store_true",
        help="Skip Path B (useful if you only want the minimal result)",
    )
    parser.add_argument(
        "--credits-grant",
        type=int,
        default=500,
        help="How many credits to grant for Path B (default 500)",
    )
    args = parser.parse_args()

    pdf = args.pdf.expanduser()
    if not pdf.exists():
        print(f"PDF not found: {pdf}", file=sys.stderr)
        return 1
    if not os.environ.get("GOOGLE_API_KEY") and not os.environ.get(
        "GOOGLE_CLOUD_PROJECT"
    ):
        print(
            "ERROR: GOOGLE_API_KEY (or GOOGLE_CLOUD_PROJECT + ADC) required",
            file=sys.stderr,
        )
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    safe_name = pdf.stem.replace(" ", "_").replace("/", "_")[:80]

    print("=" * 72)
    print(f"Spike A/B — {pdf.name}")
    print(f"  pages={_pdf_pages(pdf):>3}  bytes={pdf.stat().st_size:>9,}")
    print("=" * 72)

    print()
    print("[Path A] MINIMAL — one direct generate_content call")
    print("-" * 72)
    minimal = run_minimal(pdf)
    print(_fmt_minimal(minimal))
    minimal_path = args.out_dir / f"{safe_name}_minimal.json"
    minimal_path.write_text(json.dumps(minimal, indent=2, default=str, ensure_ascii=False))
    print(f"  -> wrote {minimal_path}")

    if args.skip_pipeline:
        return 0

    print()
    print("[Path B] CURRENT — process_document_batch (full pipeline)")
    print("-" * 72)
    try:
        pipeline = run_pipeline(pdf, credits_grant=args.credits_grant)
    except Exception as exc:  # noqa: BLE001 — surface to user
        pipeline = {"error": f"{type(exc).__name__}: {exc}"}
        print(f"  ERROR: {pipeline['error']}")
    else:
        print(_fmt_pipeline(pipeline))
    pipeline_path = args.out_dir / f"{safe_name}_pipeline.json"
    pipeline_path.write_text(
        json.dumps(pipeline, indent=2, default=str, ensure_ascii=False)
    )
    print(f"  -> wrote {pipeline_path}")

    print()
    print("=" * 72)
    print("Verdict (heuristic):")
    print(
        f"  - minimal:  {minimal.get('gemini_call_count')} call(s), "
        f"{minimal.get('elapsed_seconds')}s, "
        f"{len((minimal.get('bundle') or {}).get('documents') or [])} doc(s), "
        f"{sum(len((d.get('lines') or [])) for d in (minimal.get('bundle') or {}).get('documents') or [])} line(s) total"
    )
    print(
        f"  - pipeline: status={pipeline.get('batch_status')}, "
        f"{pipeline.get('export_row_count', '?')} export rows, "
        f"{pipeline.get('elapsed_seconds', '?')}s"
    )
    print("=" * 72)
    return 0


def _pdf_pages(pdf: Path) -> int:
    try:
        import pypdfium2 as pdfium  # type: ignore[import-untyped]

        return len(pdfium.PdfDocument(pdf.read_bytes()))
    except Exception:
        return -1


if __name__ == "__main__":
    raise SystemExit(main())
