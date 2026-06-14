# Ledgr-QBS — Master Roadmap & Plan

> Single source for both tracks: the **ADK 2.0 upgrade** (enabler) and the **Phase-1 production feedback** (from the first live Slack run). Consolidates the earlier feedback plan + `.agents-cli-spec.md`. Date: 2026-06-13.

## 0. Where we are now

- **`main` (`Projects/Ledgr-QBS`) = SOURCE OF TRUTH.** Deployed and working in Slack (workspace QBS-AI, app **Ledgr**, per-client channel e.g. `#auditair-international-pte-ltd`). Commit `7b9a163` "Production ready", on **google-adk 1.30.0**, 22 test files green. A real invoice processed end-to-end → `Ledger_FY2026.xlsx` returned to the channel.
- **Worktree (`Ledgr-QBS-adk2`, branch `adk-2.0-migration`) = ADK 2.0 spike.** Stage 1 (decouple `__init__`) + Stage 2 (bump to **google-adk 2.2.0**), 478 tests green — but branched from the *baseline* **before** `7b9a163`, so it does **not** contain production changes.
- **Divergence is benign:** `7b9a163` did **not** touch `invoice_processing/__init__.py` or `uv.lock` (only `pyproject.toml`). So landing 2.0 later = **re-apply** the two small changes on top of `main` + `uv sync` + run the suite — not a messy merge.

## 1. Architecture (decided)

- **Pattern:** deterministic, code-orchestrated pipeline + **LLM-as-tool** + **Slack as trigger & HITL surface**. Not an LLM free-for-all.
- **Agent vs Workflow:** an `LlmAgent` is a *worker node*; a `Workflow` is the *orchestrator*; we build **both, by layer**. Use a **dynamic `@node` workflow** when we need loops / branching / HITL / resume.
- **Export = per-client:** `get_exporter(client.accounting_software)`; get the **live client's software exactly right first**, add others later. *(decided)*
- **`main` is the source of truth.** *(decided)*

## 2. Track A — ADK 2.0 / agents-cli upgrade (ENABLER)

Deep spec: `.agents-cli-spec.md`. Why we want it: **HITL** (`RequestInput`), **pause/resume** (`ResumabilityConfig`), **long-running**, **dynamic workflows**.

**Reality check (important for sequencing):** most Phase-1 feedback (WS2–WS5 + the basic part of WS1) does **NOT** require ADK 2.0. The 2.0 upgrade mainly powers the **advanced Coordinator + HITL approval gates** (full WS1). So 2.0 is a *deliberate* track, not an emergency.

| Stage | Status |
|---|---|
| 1 — decouple `__init__` (lazy `root_agent`) | ✅ done in worktree (`757b703`) |
| 2 — bump to google-adk 2.2.0 | ✅ done in worktree (`6cc1c55`), 478 green |
| 3a — **consolidate** ("consolidate first"): retire dead duplicate (orphan agents, `ledgr_coordinator/`, `process_batch`/`app.processing`, old `build_app`/`fastapi_app`) + unify prod onto the live graph (`build_fastapi_app`, shared with socket) | ✅ done 2026-06-14 (ADR-0001 impl) |
| 3b — deeper re-architect `pipeline.py` → engine as a single `@node` (pipeline.py kept as engine/eval harness for now) | ⏳ not started |
| 4 — eval guard (`agents-cli eval`, ≥0.9) | ⏳ |
| 5 — deploy (Cloud Run, asia-southeast1) | ⏳ |

**Landing strategy onto `main`:** re-apply Stage 1 + Stage 2 on top of `7b9a163`, `uv sync`, run main's full suite on 2.2.0 to validate, commit. Then build Stage 3 on the unified base. Sequence per §4 — paused for now per founder ("nvm the upgrade, consolidate first").

## 3. Track B — Phase-1 production feedback (WS1–WS6)

> Baseline today: upload → pipeline → terse "✅ Batch complete" card → one `Ledger_FY{fy}.xlsx` per batch, files flat in channel.

### WS1 — Conversational UX ("chat agent" / acknowledgement) · HIGH
The bot is silent during work and the final card is too terse — the user can't tell it's working.
- 1.1 **Immediate ack** on file_shared: "👋 Got 3 file(s) — extracting now…" *before* the pipeline runs.
- 1.2 **Progress narration** via Slack `chat.update` (extracting → categorising → writing).
- 1.3 **Rich completion**: per-doc supplier, invoice no, date, total, FY/workbook, and **fields missing / flagged for review**.
- 1.4 **Coordinator + Q&A agent** (file-vs-question dispatch; answers "what did you process?", "why this category?"). Full version uses ADK 2.0 (Track A Stage 3); the ack/progress part can ship on 1.30.0.
- *Seam:* `app/processing.py::process_shared_files` (`say_fn`). *Accept:* ack ~1s; completion shows per-doc detail + flags.

### WS2 — Extraction completeness vs accounting schema · HIGH (correctness)
Export doesn't fill every required column (invoice date, due date, total amount missing). A half-filled row can't be imported.
- 2.1 Lock each target software's import schema (per-client; start with live client). Required vs optional columns.
- 2.2 Map extraction → every column; list gaps.
- 2.3 **Enforce required fields** (invoice_date, due_date, total_amount) → missing ⇒ **flag for review**, never emit half-filled rows silently.
- 2.4 Strengthen extraction to capture dates/totals.
- 2.5 Schema-validator step (reconcile guard already covers totals).
- *Seam:* `export/models.py`, `extract/invoice_extractor.py`, `export/exporters.py`. *Accept:* all required columns filled or flagged.

### WS3 — Export format (Xero multi-line total rule) · HIGH (correctness)
Xero import needs the **invoice total repeated on every line row** of a multi-line invoice (2-line 2k → each row Total 2k) so Xero groups them.
- 3.1 Confirm exact target columns/order (Xero precoded: ContactName, InvoiceNumber, InvoiceDate, DueDate, Description, Quantity, UnitAmount, AccountCode, TaxType…) + how total sits per line.
- 3.2 Exporter emits exact header order per software.
- 3.3 Multi-line grouping (shared InvoiceNumber; total-per-line).
- *Seam:* `export/exporters.py` (`get_exporter`, column defs, `rows()`). *Accept:* a 2-line invoice imports cleanly into the target software.

### WS4 — Single consolidated workbook (no many "weird excels") · MED
- 4.1 **Upsert into one** `Ledger_FY{fy}.xlsx` per client (load → append → re-publish same logical file).
- 4.2 **Idempotency key** `client_id + FY + supplier + invoice_no + total` so re-runs never duplicate rows.
- 4.3 Slack file handling: keep ONE visible file (Slack files aren't edited in place → delete-old+upload-new, or a Canvas link).
- *Seam:* `pipeline.py::process_batch`, `app/archive.py::save_workbook` (idempotency chokepoint). *Accept:* one workbook, all rows, zero dupes across sessions.

### WS5 — Folder structure (Slack Folders/Canvas + GCS) · MED
- 5.1 Confirm GCS structure in prod: `{client_id}/FY{fy}/{purchase|sales|bank}/…` + `/workbooks/`.
- 5.2 Slack-side: check API feasibility for Folders (may be UI-only); else a **Canvas** index linking docs + workbook per FY/category.
- 5.3 Consistent naming (`Ledger_FY2026.xlsx`, `BankStatement_FY2026.xlsx`).
- *Accept:* organised by FY+category in GCS (certain), Slack where the API allows.

### WS6 — Integration & merge hygiene · DO FIRST / ONGOING
Parallel agents added divergent tests; production and the 2.0 work diverged (see §0).
- 6.1 `main` is SoT (confirmed). Stop parallel divergent test-writing.
- 6.2 Land Track A Stage 1+2 onto `main` via re-apply (clean — see §2), validate full suite on 2.2.0.
- 6.3 One green suite on one branch; deployed == repo.
- *Accept:* single integrated branch, full suite green.

## 4. Unified sequence (recommended)

1. **WS6** — stop divergence; (later) land 2.0 onto `main`, validated.
2. **WS2 + WS3** — extraction completeness + per-client export format (Xero total rule). *Core value, version-independent — can start on 1.30.0 now.*
3. **WS1** — conversational UX: ack + progress + rich completion now (1.30.0); full Coordinator + HITL on 2.0 (Track A Stage 3).
4. **WS4** — single workbook + idempotency.
5. **WS5** — folder structure.

## 5. Open items
- Per-software exact column lists (start with the live client's accounting software).
- Slack Folders API feasibility (WS5) — confirm before committing to that vs Canvas.
- When to pull the trigger on Track A landing onto `main` (after WS2/WS3, or before WS1's full coordinator).
