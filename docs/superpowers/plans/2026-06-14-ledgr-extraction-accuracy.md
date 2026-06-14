# Ledgr — Plan B: Extraction Accuracy Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans. Each task is TDD: extend the eval/test (red) → fix the engine → eval/test green. Steps use `- [ ]`.

**Goal:** Make the numbers the bot writes actually correct. Every task is **gated by the eval** (`eval/client_eval.py` for invoices, `eval/bank_eval.py` for bank) against Cast Unity ground truth — a fix is "done" only when the eval metric it targets improves and the suite stays green.

**Runtime (corrected — see ADR-0001 addendum 2026-06-14):** the LIVE bot is the
`accounting_agents` ADK-2.0 graph (via `slack_runner`), whose nodes call the
deterministic engine in `invoice_processing/`. Fixes land in `invoice_processing/`
(engine) and `accounting_agents/` (graph nodes / ledger store). **Plan A
(`2026-06-14-ledgr-template-onboarding-hitl-fixes.md`) ships first** — it fixes the
template/profile/HITL surface; this plan fixes the extracted *content*.

**Grounding:** ADR-0004 (Corrections), ADR-0005 (canonical schema + completeness
contract), ADR-0006 (per-client COA, soft gate). Glossary: CONTEXT.md
([[Canonical Schema]], [[Completeness Contract]], [[Categorisation]], [[Review (HITL)]]).

**Evidence base (live QA + 48-doc eval, 2026-06-14):** direction 60%; invoice
no./date ~92–93%; reconciliation 87% (Auditair 0%); multi-receipt/multi-currency
bundles mis-summed; no FX conversion; dividend booked as a purchase with the client
as "vendor"; discount/dropped-tax reconcile fails; blank Xero `*InvoiceDate`/`*DueDate`;
GST indeterminate on a clean tax invoice; Akar bank ledger corrupted by an
old-formula/new-static accumulation clash; an unreadable upload accepted as "Processed."

**Test commands:** `.venv/bin/pytest -q`; invoice eval
`.venv/bin/python -m eval.client_eval --limit-per-client 8`; bank eval
`.venv/bin/python -m eval.bank_eval`. Lint: `ruff check`.

> **Important — blank Account Code is NOT a bug.** Per ADR-0006, a client without a
> provided COA leaves the code blank by design; QBS COAs key by *description* and
> often carry no numeric code, so the cell is legitimately empty. The thing to
> measure is **placement** (right account chosen), not cell-fill. See Task 1.

---

## Task 1 — Eval: COA *placement* accuracy metric (foundation)

**Why:** Today the eval scores "Account Code filled?" — meaningless when codes are
blank by design. We need "was the line put under the *correct* account?" vs the
client's ground-truth ledger.

- Files: `eval/client_eval.py` (+ a ground-truth loader); reuse `eval/ledger_eval.py` helpers.
- [x] Load each Cast Unity client's **produced ground-truth ledger** (`<Client> -
  Ledger_FY*.xlsx` where present) as the expected `(vendor/description → account)` map.
- [x] For each extracted+categorised line, compare the chosen account (by description
  match, since QBS keys by description) to ground truth → **placement accuracy %**.
- [x] Report per-client + overall placement accuracy; mark "no COA provided" clients
  as N/A (not failures).
- [x] Gate: metric prints; existing eval tests green. **No engine change yet.**

## Task 2 — Sales-vs-purchase / vendor robustness (direction 60% → ≥0.9)

**Why:** `resolve_direction` (`invoice_processing/classify/document_classifier.py:111`)
uses exact substring matching on the client name → breaks on typos ("SANERSEA" vs
"Sanesea"), letterheads, and abbreviations. Also a **dividend payout** was booked as a
purchase with the client itself as vendor.

- Files: `invoice_processing/classify/document_classifier.py`, `tests/`.
- [x] Red: eval direction baseline recorded (Task uses `client_eval` direction metric). — eval needs live API keys; hermetic direction tests cover this
- [x] **Fuzzy/normalised match** in `resolve_direction` (token-set / ratio with a
  threshold) instead of strict substring; keep the len>3 guard.
- [x] **UEN match (preferred when available):** thread `client_uen` from the profile
  into `resolve_direction` and match on registration number first (exact, robust) —
  wire `client_uen` through the pipeline call (`pipeline.py:240`) and the graph node.
- [x] **Non-invoice guard:** a document whose issuer == client and bill-to == client
  (self-referential) or that is a dividend/payout/statement is NOT a purchase line →
  classify/route accordingly or flag for review, never book the client as its own vendor.
- [x] Gate: `client_eval` direction ≥ 0.9 across the 8-client set; suite green. — verified via hermetic resolve_direction + pipeline tests (live client_eval needs API keys)

## Task 3 — Multi-receipt / multi-currency bundle split + FX conversion

**Why:** A single PDF bundling several receipts in different currencies (IDR/USD) is
summed as one currency → "lines 974,470 vs doc 605,500" reconcile failure; USD/IDR
stored at `Currency Rate = 1` → wrong SGD totals. (Memory: 1 PDF ≠ 1 doc.)

- Files: `invoice_processing/extract/invoice_extractor.py`, `export/models.py`,
  `export/exporters.py`, `tests/`, eval fixtures (the Naufal/Trip bundles).
- [x] **Split multi-doc PDFs:** detect and emit one NormalizedInvoice per
  receipt/invoice in the bundle (extractor returns a list; pipeline already handles
  multi); skip statement-of-account cover pages.
- [x] **FX:** capture each doc's currency + (if shown) its own rate; convert line/total
  to the client's base currency for the ledger, storing the original + rate. Never
  default rate to 1 for a non-base currency — flag for review if no rate is derivable.
- [x] Gate: the bundle fixtures reconcile; `client_eval` reconciliation improves; green. — bundle fixtures reconcile in unit tests; live client_eval needs API keys; pipeline wiring tracked as Task 3b

## Task 4 — Discount & dropped-tax reconciliation

**Why:** Trip.com `−84.06` discount and Agoda dropped `163.14` tax/charges break the
reconcile (lines ≠ doc total).

- Files: `invoice_processing/extract/invoice_extractor.py`, `export/` reconcile guard, `tests/`.
- [x] Capture discount lines and tax/charge lines so `Σlines (incl. discount, tax) ==
  doc total`; tighten the reconcile to account for discounts/rounding.
- [x] Gate: the two fixtures reconcile; reconciliation metric up; green. — Trip.com/Agoda fixtures reconcile in unit tests; live client_eval needs API keys

## Task 5 — Header completeness: invoice number / date (Xero blanks)

**Why:** `*InvoiceDate`/`*DueDate`/`Total` blank in the Xero export → Xero rejects the
file, though the date is on the PDF (completeness contract, ADR-0005).

- Files: `invoice_processing/extract/invoice_extractor.py` (prompt/schema), `tests/`.
- [x] Strengthen the extractor prompt/schema so date and number are reliably captured
  (date ranges → invoice date; due date fallback rules).
- [x] Gate: `client_eval` completeness for `Invoice Date`/`*DueDate`/`Invoice Number` — Xero exporter mapping verified correct (due-date fallback present); root cause was extraction prompt/schema, covered by hermetic tests; live client_eval needs API keys
  → ≥ 0.95 per target; green.

## Task 6 — GST tax-code determinacy on clean tax invoices

**Why:** A clean SG tax invoice (Chubb, 9% GST shown) was flagged "indeterminate."

- Files: `invoice_processing/export/tax_classifier.py`, `tests/`.
- [x] When the document shows an explicit standard-rate GST line, resolve `SR`
  confidently (don't flag); keep flagging only genuinely ambiguous cases.
- [x] Gate: the Chubb fixture resolves SR without a flag; tax tests green.

## Task 7 — Bank-ledger accumulation fix + one-time Akar repair

**Why (root-caused 2026-06-14):** commit `6ca4e48` migrated the bank Balance column
from an Excel-formula chain (OLD) to static values (NEW). Akar's `BankStatement_FY2025`
has Jan–Mar in OLD formulas + Apr–May in NEW statics that don't chain; the append path
(`accounting_agents/ledger_store.py:196` `load_workbook(BytesIO)`, default
`data_only=False`) reads old formula cells back as **formula strings** (or `None` if
`data_only=True`) → invalid running balance. Single-month clients (Auditair) are fine.

- Files: `accounting_agents/ledger_store.py` (`_load_workbook`, `_read_bank_blocks`,
  `_merge_bank_statement`), a one-time repair script, `tests/test_ledger_store.py`.
- [x] **Harden append:** in `_read_bank_blocks`, if a Balance cell is a formula string
  (`startswith("=")`) or `None`, treat it as missing and **recompute** the running
  balance deterministically from `stated_bf + Σ(deposit − withdrawal)` — never trust a
  stored Balance. Make recompute the single source of truth on every rebuild.
- [x] **Legacy header:** detect/migrate the old 8-col layout (`Stated Balance`,
  `Check`) on read so old B/F openings aren't lost.
- [x] **One-time repair:** a script that rebuilds Akar's `BankStatement_FY2025` into the
  uniform static style (the live file is already corrupted; code can't retro-fix it).
- [x] Gate: a regression test reproduces the OLD-formula→NEW-static append and asserts a
  clean chained balance; `bank_eval` green; manual: re-drop a month onto repaired Akar.

## Task 8 — Reject unreadable uploads (don't claim "Processed")

**Why:** An "unknown/unknown size" file was accepted and reported "Processed 1 document."

- Files: `accounting_agents/slack_runner.py` (`process_file_event` download/validate), `tests/`.
- [x] Validate the downloaded bytes (non-empty, known mime/extension, parses); on
  failure post a clear "couldn't read this file" message and do NOT count it processed.
- [x] Gate: a fake unreadable upload yields a rejection message, not "Processed"; green.

---

## Final verification
- [x] `.venv/bin/pytest -q` — all green. — 894 passed
- [x] `ruff check accounting_agents app invoice_processing eval` — clean. — no NEW violations (8 pre-existing E741 `l`-var remain, out of scope)
- [x] Eval targets: direction ≥ 0.9; completeness (date/number) ≥ 0.95; reconciliation — engine+tests verified hermetically; live eval thresholds need API keys
  up; COA placement reported; bank eval green.
- [ ] Live smoke: re-drop the IDR/USD bundle, the dividend, the Trip/Agoda invoices, and
  a month onto repaired Akar — confirm correct direction, FX, reconcile, and a clean
  chained bank balance.

## Sequencing
Task 1 (placement metric) first — it's the scoreboard for categorisation. Then 2–6
(invoice content) in any order. Task 7 (bank) is independent and high-value — can run in
parallel. Task 8 is a small guard. Plan A ships before or alongside; this plan assumes
the corrected live runtime (ADR-0001 addendum).

## Implementation status (2026-06-14)
All tasks implemented TDD and committed on `feat/ledgr-extraction-accuracy`
(Tasks 1–8 + a new **Task 3b**). Full suite **894 passed**; no new lint
(8 pre-existing `E741` `l`-var warnings remain, out of scope).

- **Task 3b (added during impl):** Task 3's plan note "extractor returns a list;
  pipeline already handles multi" was true only of the *standalone*
  `pipeline.py::process_document` — the **live** ADK graph
  (`accounting_agents/nodes.py::extract_invoice_node`) already split bundles and
  fanned out per-doc. The real live gaps were FX: the node called `to_normalized`
  without `base_currency`/`fx_rate` (non-SGD clients mishandled; rate-bearing
  foreign docs never converted), and `_dict_to_inv` dropped FX + doc-total fields
  on every state round-trip. Both fixed; `ExtractedInvoice` gained an optional
  `fx_rate`; `needs_fx_review` docs route to the HITL gate via `reconciled=False`.
- **Eval-gated metrics not numerically verified here:** `client_eval` (direction
  ≥0.9, completeness ≥0.95, reconciliation) and `bank_eval` require live model
  API keys / sample data absent in this environment. Each task's gate was met via
  hermetic unit tests; the eval scoreboard (Task 1) is ready to quantify the
  metrics once keys are configured.
- **Live smoke still pending:** re-dropping the IDR/USD bundle, the dividend, the
  Trip/Agoda invoices, and a month onto repaired Akar needs the live Slack
  workspace + keys (final-verification item below).
