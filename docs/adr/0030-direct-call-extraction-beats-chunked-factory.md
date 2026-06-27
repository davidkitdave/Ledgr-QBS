# 0030 — One direct Gemini call beats the chunked extraction factory; minimal `FunctionTool` extraction is now part of `ledgr_agent`

- **Status:** Accepted
- **Date:** 2026-06-26
- **Deciders:** Ledgr team
- **Relates to:** ADR-0011 (Understand layer), ADR-0026 (AI reads, rules apply),
  ADR-0029 (deterministic chunking for fan-out), ADR-0031 (light policy ladder).
  **Amends** ADR-0029's pre-chunk heuristic — see "Amendment" below.
- **Branch / evidence:** `feat/minimal-extract-control-experiment`,
  `scripts/spike_minimal_extract_vs_pipeline.py`,
  `~/Desktop/localtest/{BV-0002830_Starhub_*,multi_receipt_*}.json`

## Context

Issue #28 surfaced a user-visible failure: extracting the real
`BV-0002830 Starhub 8.20057598B bill 122025.pdf` (18 pages, 4.4 MB)
returned 12 fragmented `invoice` documents and 216 noisy ledger lines, most
with empty `description`, in ~210 s and `status=needs_review`. The same PDF
opened in Google Drive's Gemini sidebar returns 1 bill, 3 lines, 5 GST
groupings, ~9 s. The hypothesis was that `invoice_processing` is too heavy
for a single bill; the codebase proved it on a fresh branch.

### What the A/B test actually proved

Branch `feat/minimal-extract-control-experiment` runs
`scripts/spike_minimal_extract_vs_pipeline.py` against the real PDFs. The
script builds two paths sharing the same model, client, and Gemini API key:

- **Path A — MINIMAL.** One `client.models.generate_content` call with the
  whole PDF inline (`types.Part.from_bytes`), a short prompt placed *after*
  the document (per Google's documented best practice), and a Pydantic
  `response_schema` that captures `vendor`, `reference`, `date`,
  `grand_total`, `lines[]`, and crucially **`tax_lines[]`** (the clean
  SR/ZR GST breakdown Drive surfaces). `max_output_tokens=65536` set
  explicitly. No chunking, no `pdf_chunks`, no `pdf_chunks.should_chunk_pdf`,
  no per-line "copy every row" prompt.

- **Path B — CURRENT.** `process_document_batch` via the production
  pipeline (chunking, the 75-line `FAITHFUL_EXTRACT_STATIC_INSTRUCTION`,
  tax_classifier, categorize, export).

### Headline numbers (run on the real Starhub bill + multi-receipt PDF)

| Bill (pages, bytes) | Path | Gemini calls | Docs | Lines | Wall-clock | Status |
|---|---|---|---|---|---|---|
| Starhub (18p, 4.4 MB) | A minimal | **1** | **1** | **3** | **8.9 s** | success |
| Starhub (18p, 4.4 MB) | B pipeline | ≥12 | 12 (fragmented) + 1 dropped SOA cover | 216 (mostly empty) | 209.8 s | `needs_review` |
| multi-receipt (35p, 19.4 MB) | A minimal | **1** | **96** | **852** | 190 s | success |
| multi-receipt (35p, 19.4 MB) | B pipeline | many | 96 | **680** (truncated) | 758 s | `needs_review` |

**Path A reproduces exactly what Drive shows** for Starhub:
`vendor="StarHub Ltd"`, `grand_total=1328.15`, 3 service lines (Internet IP /
Mobile / Switch), and 5 clean `tax_lines` (`GST @ 9% on $1,164.42` for SR,
`GST @ 0% on $58.93` for ZR, plus three zero-amount rows and "Charges not
subject to GST"). One Gemini call, ~5K tokens total.

**Path B's chunking caused the very truncation it was added to prevent.**
The 35-page multi-receipt PDF loses 172 of 852 lines (20%) because each
5-page chunk's `generate_content` call has the SDK's default (too-low)
output budget; the chunked output is then re-assembled into a bundle that
fails downstream validation and lands in `needs_review`.

### Why the factory was failing (the actual root cause, three pieces)

1. **`default_llm_config` did not set `max_output_tokens`.** The SDK default
   was too low to hold the full structured JSON for a 35-page / 96-receipt
   PDF — silently truncated, output unparseable. Chunking was a workaround
   for the truncation but **does not raise the per-call budget**, so each
   chunk still truncates at the same per-call ceiling.
2. **`pdf_chunks.should_chunk_pdf` pre-chunked at `page_count > 10` (and
   `bytes > 10 MB`).** That threshold is far below Google's documented
   inline limit (50 MB / 1000 pages per call) and below Gemini 2.5
   Flash-Lite's `max_output_tokens` ceiling (65,536). An 18-page bill is
   one logical document — chunking it into 4 blind 5-page pieces fragments
   the SR/ZR buckets, drops the clean per-page context, and adds 4 round
   trips. The Starhub bill is **4.4 MB** and the multi-receipt PDF is
   **19.4 MB** — both fit comfortably inline.
3. **`FAITHFUL_EXTRACT_STATIC_INSTRUCTION` had conflicting telco rules.**
   "Copy every visible charge row" vs "summary only, do NOT emit per-call
   detail" — both true depending on the bill, but presented as mandatory
   rules. The model produced 216 noisy lines for one bill, then collapsed.

### Google's documented guidance, verified via the ADK + Google Dev MCPs

- **Direct `generate_content` + `response_schema` is the recommended pattern
  for "highly structured, repeatable" extraction.** It is *not* the
  multi-agent / sub-agent pattern — sub-agents add a delegation decision
  and extra latency without improving schema-constrained output (Google
  Dev Knowledge MCP, 2026-06-26).
- **Function Tools, not Agents-as-a-Tool, for deterministic extraction.**
  `adk.dev/tools-custom/function-tools` describes three integration shapes
  (Function Tools, Long Running Function Tools, Agents-as-a-Tool). The
  Agents-as-a-Tool pattern is for when the parent must *decide* whether to
  delegate; here the parent just calls the extractor. ADK wraps a plain
  Python function as `FunctionTool` automatically when added to
  `Agent.tools`.
- **Native vision over the whole document, prompt after the document.**
  "Foundation models are recommended as the first option" for PDF
  understanding. The Files API / `GcsArtifactService` is a *latency*
  optimisation, not a correctness requirement, and our bills are well under
  the 50 MB inline cap.

## Decision

**1. Use one direct `generate_content` call (the "minimal path") as the
default extraction. The chunked factory remains only as a `ValidationError`
fallback, with a hard byte guard retuned to Google's 50 MB inline limit.**

**2. The minimal extractor is exposed as a plain Python function and
registered on `ledgr_agent` as a `FunctionTool` alongside
`process_document_batch`** (not as a sub-agent and not as a replacement).
Both tools are now in `_build_root_tools()`; the agent decides which to
call per user request:
- `process_document_batch` — full pipeline with credit gating, multi-doc
  fan-out, SOA cover handling. Use for batches and unknown-shape PDFs.
- `extract_one_bill_minimal` — one direct call, returns the bundle + clean
  `tax_lines[]` directly. Use when the user wants fast, clean single-bill
  extraction with the same output shape Drive's Gemini surfaces.

**3. `tax_lines[]` is now wired downstream into `NormalizedInvoice.tax_breakdown`**
so the clean SR/ZR breakdown survives into export / review. Previously the
field was extracted and then discarded.

## Amendment to ADR-0029

ADR-0029 (multi-document fan-out via deterministic page-window chunking)
introduced `pdf_chunks.should_chunk_pdf` with `LARGE_PDF_PAGE_THRESHOLD=10`
and `LARGE_PDF_BYTE_THRESHOLD=10 MB`. **This ADR narrows that gate to the
byte-only check against Google's 50 MB inline limit**; page count is no
longer a chunk trigger. `iter_pdf_page_chunks` / `merge_chunk_bundles` /
`extract_document_ledger_chunked` are kept for the `ValidationError`
fallback at `ledger_extract.extract_document_ledger`, unchanged.

The chunked path still wins when one PDF genuinely holds dozens of logical
documents (a 200-page SOA bundle, a 500-page multi-receipt scan) — that is
the regime ADR-0029 covered. What it does NOT win on is a single clean
bill that happens to be 18 or 35 pages — that is a single Gemini call
today, full stop.

## Concrete changes (in this branch)

| File | Change |
|---|---|
| `invoice_processing/shared_libraries/gemini_call_config.py` | Add `DEFAULT_MAX_OUTPUT_TOKENS=65536`; `default_llm_config` now pins it. Real issue-#16 fix. |
| `invoice_processing/extract/pdf_chunks.py` | Remove page-count branch from `should_chunk_pdf`; retune byte guard to 50 MB; rename `LARGE_PDF_PAGE_THRESHOLD` removed. `iter_pdf_page_chunks` / `merge_chunk_bundles` unchanged. |
| `invoice_processing/extract/ledger_extract.py` | Replace telco-shaped prompt lines (`:91-95`, `:363-366`) with the same neutral summary-vs-detail rule the minimal prompt uses. `extracted_document_to_normalized` now copies `doc.tax_lines` into `inv.tax_breakdown`. |
| `invoice_processing/extract/invoice_extractor.py` | Same prompt relaxation for `_PROMPT` (lines around `:142-165`). |
| `invoice_processing/export/models.py` | Add `tax_breakdown: list[dict] = field(default_factory=list)` to `NormalizedInvoice`. |
| `ledgr_agent/tools/minimal_extract_tool.py` | **NEW.** The minimal extractor as a plain Python function: one direct call, returns a dict matching `process_document_batch`'s outer shape (status, path, documents, tax_lines, grand_total, vendor, errors). |
| `ledgr_agent/tools/__init__.py` + `ledgr_agent/agent.py` | Register `extract_one_bill_minimal` in `_build_root_tools()`; update the system instruction to describe both tools. |
| `scripts/spike_minimal_extract_vs_pipeline.py` | **NEW.** The A/B harness. Re-runnable; writes `_minimal.json` and `_pipeline.json` next to each PDF in `~/Desktop/localtest/`. |
| `tests/test_pdf_chunk_extract.py` | Replace page-threshold test with hard-50MB-limit + 18p/35p regression tests; add `max_output_tokens` assertion on `default_llm_config`. |

## Consequences

- Starhub-class and multi-receipt-class bills now extract cleanly in **one
  Gemini call** with no truncation, no fragmentation, and no
  `needs_review` bounce.
- `tax_lines[]` (the clean SR/ZR breakdown Drive shows) now survives into
  `NormalizedInvoice.tax_breakdown` and is reachable from exporters and
  reviewers. Was previously extracted and discarded.
- `process_document_batch` still owns the full bookkeeping path (categorize,
  tax, COA match, export, workbook build). The minimal tool surfaces the
  extracted bundle + tax_lines for direct review; full booked rows still
  come from the engine path. Both tools share the same model and the same
  output schema, so they remain interchangeable for the agent's reasoning.
- The chunked fallback remains; it triggers **only** when a single PDF
  exceeds 50 MB inline (rare in this domain — our largest test bill is
  19.4 MB) or when `validate_extracted_document` fails on the single-call
  result and we want to retry per-page.
- No new vendor-specific hardcoding was added. The fix is structural: pin
  the output budget, remove the over-aggressive pre-chunk, and let the
  model see the whole document with a clean, neutral prompt.

## Alternatives considered

- **Multi-agent / sub-agent extraction** (an `LlmAgent` wrapping the
  minimal call, with auto-delegation from the parent). Google's docs are
  explicit: sub-agents add a delegation decision and overhead, with no
  quality gain for schema-constrained output. Rejected.
- **Keep chunking, just set `max_output_tokens` higher.** The chunking
  itself causes fragmentation (Starhub 12 fake docs from 1 real bill) and
  ~4× wall-clock cost. The byte-guard alone, with the budget pinned,
  removes the truncation root cause without losing per-page context.
- **Document AI Invoice Parser** (NotebookLM suggestion). A separate
  Google product optimised for high-scale procurement ingestion;
  overkill for a per-firm accountant app, and pulls data into a different
  schema than the rest of Ledgr.

## Sources (verified 2026-06-26)

- ADK function tools / Agents-as-a-Tool: `adk.dev/tools-custom/function-tools`
- Gemini document processing: `ai.google.dev/gemini-api/docs/document-processing`
- Gemini structured output / `max_output_tokens`: `ai.google.dev/gemini-api/docs/structured-output`
- Google Dev Knowledge MCP answer_query (2026-06-26): "Direct `generate_content`
  with `response_schema`... best for highly structured, repeatable processes";
  "Use a direct model call for simpler tasks to reduce latency and operational
  costs compared to the orchestration overhead of multi-agent systems."
- Phase-0 A/B raw evidence: `~/Desktop/localtest/BV-0002830_Starhub_8.20057598B_bill_122025_{minimal,pipeline}.json`,
  `~/Desktop/localtest/multi_receipt_{minimal,pipeline}.json`.
