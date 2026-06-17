# Slack delivery, currency, and batch UX — QA 2026-06-16

**Plan:** `slack_delivery_batch_ux_8e549468.plan.md` (Phases A–C)  
**Channel (live target):** `#acme-client-test` (`C0123456789`)  
**Client:** Company-A — Oct FYE (`fye_month=10`), Xero

---

## 1. Automated verification

| Area | Tests | Result |
|------|-------|--------|
| Currency column (Xero/QBS/bank preview) | `tests/test_app_blocks.py` — `test_xero_purchase_header_order` (9 cols, ends with Currency), bank 6-col shape | ✅ pass |
| Consolidated delivery (one post) | `tests/test_slack_runner.py` — `test_delivery_two_batches_posts_two_preview_messages` asserts `len(data_table_posts) == 1` | ✅ pass |
| Delivery summary software label | `tests/test_nodes.py` — `test_deliver_invoice_names_client_scoped_ledger` | ✅ pass |
| Plan stage outputs / terminal collapse | `tests/test_slack_runner.py` — processing plan snapshot tests (`understand` output frozen, failed stage) | ✅ pass |
| Batch defer + job summary | `tests/test_batch_drop_posts_one_job_summary_then_threads_per_doc`, `test_single_file_drop_still_posts_summary_then_thread` | ✅ pass |
| HITL delivery card lockstep | `test_approve_path_emits_full_delivery_card_not_bare_string`, `test_clean_path_emits_full_delivery_card` | ✅ pass |
| **Full suite** | `uv run pytest tests/ -q --ignore=tests/eval` | **1434 passed, 1 skipped** |

---

## 2. Live Slack attempts

### 2.1 Single-doc upload script

**Command:**

```bash
GOOGLE_GENAI_USE_VERTEXAI=FALSE uv run python scripts/slack_upload_and_process.py \
  "~/Desktop/LocalTest/.../FY2025/INV-2025-015-sample.pdf" \
  --channel C0123456789 --approve \
  --comment "[UX test] single doc consolidated delivery"
```

**Result:** ❌ **Blocked** — `slack_sdk.errors.SlackApiError: files.completeUploadExternal → internal_error` (Slack-side, not Ledgr). Pipeline did not run.

**Workaround for live QA:** multi-select upload in Slack UI (triggers real `file_share` batch handler), or retry when Slack API is healthy.

### 2.2 Batch stress (FY2025 + FY2026 mix)

**Planned corpus** (from plan Phase C):

| Doc | Expected FY |
|-----|-------------|
| `INV-2025-012-sample.pdf` | FY2025 |
| `MGT-2025-011` (Jan 2025) | FY2025 |
| `INV-2026-003-sample.pdf` | FY2026 |
| `INV-2026-031-sample.pdf` | FY2026 |
| 1× unreadable / wrong ext | `rejected=1` in tally |
| Optional duplicate re-drop | `duplicates=1` |

**Result:** ⏸ **Not executed live** (same Slack upload API failure). Behaviour covered by unit tests above; manual pass criteria below for next live run.

---

## 3. Expected UX (post-implementation)

### Single document

1. **One delivery message** — emoji summary + `data_table` in a single Block Kit post (`delivery_card_blocks()`). No separate “Recent Purchase rows…” preface.
2. **Summary copy** — e.g. `Added 1 line from 1 document to your **Acme Client … – Ledger FY2026 (Xero)**`.
3. **Currency column** visible on Xero/QBS purchase/sales preview rows.
4. **Terminal status** — per-doc thread message collapses to `✅ Added to Ledger FY2026 (Xero)` without the processing-plan accordion.
5. **Plan outputs** — understand stage shows vendor/amount; categorize no longer overwrites understand output.

### Multi-file batch (2+)

1. **Top-level job message** — `Received N documents — starting…` → live `Processing N documents — X/N done (Y posted, Z needs review)…` → final `job_summary_text()` tally.
2. **Per-doc thread** — processing status + HITL cards still appear; **no** per-doc delivery summary/data_table when batched.
3. **Aggregate delivery** — one `_post_batch_aggregate_delivery()` at batch end: combined summary + one `data_table` per `(fy, sheet)` group; mixed FY batches get multiple tables/summary segments.

---

## 4. Manual pass checklist (next live session)

When Slack upload API is available:

- [ ] Single FY2025 doc → exactly **1** delivery `chat_postMessage` (count posts in channel/thread)
- [ ] Currency column present in data_table header row
- [ ] No duplicate processing-plan accordion after delivery
- [ ] Batch 5–10 PDFs (FY2025+FY2026 mix) → **1** aggregate delivery at top level, not N stacks
- [ ] Job summary updates live during batch (watch message edit)
- [ ] Unreadable file increments `rejected` in final tally
- [ ] HITL pause still shows approval card in thread (delivery deferred until batch end or per-doc approve path)

---

## 5. Known gaps / follow-ups

| ID | Item | Severity |
|----|------|----------|
| QA-L1 | Live batch stress not run — Slack `internal_error` on `files.upload_v2` | Blocker for live sign-off only |
| QA-L2 | `scripts/slack_upload_and_process.py` is single-file only; batch QA needs Slack UI multi-upload or `--batch` flag | Nice-to-have |
| QA-L3 | Prior round (`2026-06-16-round4-relive-findings.md`) noted HITL-approve bare `"Document processed."` — lockstep tests now green; re-verify live after upload API works | P0 if still reproduces |

---

## 6. Files changed (this plan)

| File | Change |
|------|--------|
| `app/blocks.py` | Currency cols; `delivery_card_blocks`, `job_progress_text`, `compose_batch_delivery_summary`; caption-only data_table |
| `accounting_agents/nodes.py` | `(Xero)` / software in `compose_delivery_summary` |
| `accounting_agents/slack_runner.py` | Consolidated delivery; batch defer/aggregate; understand output fix; terminal plan collapse |
| `docs/block-kit-ui.md` | Delivery + batch sections updated |
| `tests/test_app_blocks.py`, `test_slack_runner.py`, `test_nodes.py` | Regression coverage |

---

# Round 2 — Batch UX consolidation (2026-06-16, evening)

**Plan:** `batch_ux_and_eval_a4892803.plan.md`  
**Trigger:** Screenshot showed 6 top-level "Processing [dev] 'file.pdf'" accordions on a 6-file drop, plus a job summary and 6 file-share announcements. Goal: one message per batch, with one combined data_table and a fix for the underlying race.

## R2.1 Automated verification

| Area | Test | Result |
|------|------|--------|
| `file_shared` no longer processes documents | `tests/test_slack_runner.py::test_file_shared_does_not_process_documents`, `tests/test_event_dedup.py::test_file_shared_dedup_does_not_call_process` | ✅ |
| 6-file drop → 1 top-level message | `tests/test_slack_runner.py::test_batch_six_files_one_top_level_message` (asserts 1 top-level post + delivery in the same `chat_update` on `summary_ts`) | ✅ |
| `batch_mode=True` suppresses per-doc status | `tests/test_slack_runner.py::test_batch_mode_skips_per_doc_status_post` | ✅ |
| Same-FY/sheet batch → 1 merged `data_table` | `tests/test_slack_runner.py::test_batch_aggregate_one_table_same_fy` (3 docs → 1 table with 4 rows incl. header) | ✅ |
| `n_docs` counts documents, not sheets | `tests/test_slack_runner.py::test_batch_aggregate_n_docs_counts_documents_not_batches` (1 doc, 2 sheets → "1 document", "2 lines") | ✅ |
| Summary table on HITL pause | `tests/test_slack_runner.py::test_paused_run_posts_summary_table_before_approval_card` | ✅ |
| **Full suite** | `uv run pytest tests/ -q --ignore=tests/eval` | **1440 passed, 1 skipped** |

## R2.2 Live Slack — single doc (Acme Client FY2025)

`scripts/slack_upload_and_process.py INV-2025-015-sample.pdf --channel C0123456789 --approve`

```json
{"status": "delivered",
 "append": {"slack_file_id": "F0BAR87D0SF", "appended": 2, "deduped": 0,
            "filename": "Company-A - Ledger_FY2025.xlsx",
            "kind": "invoice", "software": "Xero", "fy": "2025"}}
```

✅ Pipeline ran end-to-end via the message handler path. 2 rows appended to `Ledger_FY2025.xlsx` (Xero).

## R2.3 Live Slack — 6-file batch

**Status:** ⏸ Not yet executed live. Slack UI multi-select upload needed (the upload script handles one file per run; the 6-file drop in your screenshot came from drag-select in the Slack composer). Behaviour covered by:

- `test_batch_six_files_one_top_level_message` — mock-pfe path with all 6 docs producing `deferred_delivery`; asserts exactly 1 top-level post + delivery lives in `chat_update` on `summary_ts`.
- `test_batch_six_files_one_top_level_message` also asserts every `process_file_event` call carries `batch_mode=True` (the contract that suppresses per-doc accordions).

**Manual checklist for the next live drop:**

- [ ] 1 top-level `Received N documents — starting…` message
- [ ] No top-level `Processing [dev] 'file.pdf'` accordions
- [ ] Final tally + delivery card live in the same message (`chat_update` with `blocks=[section, ...data_table]`)
- [ ] HITL pause cards only in thread (if any doc paused)

## R2.4 Why the Phase 1 fix matters

Before the fix, the `file_shared` Bolt handler was the document owner. It
fired 6 times in parallel for a 6-file drop — each invocation called
`process_file_event` with **no `thread_ts` and no `defer_slack_delivery`**
and posted 6 top-level "Received `file.pdf`" + plan accordions before the
`message` handler could post its single job summary. Once the message
handler ran, the dedup kicked in but the noise was already posted.

After the fix (`_file_shared` drops document processing), the message
handler is the sole owner and:

- `batch_mode=True` flips `status_ts` to `None` so `_post_status` and
  `_update_status` no-op, suppressing the per-doc "Received" + plan
  accordion. (HITL cards still post — they need a thread_ts=summary_ts.)
- `_build_batch_aggregate_blocks` groups rows by `(fy, sheet, workbook)`
  and emits one `data_table` per group. With same-FY/Purchase docs, all
  rows merge into a single 100-row table.
- The final `chat_update` rewrites the placeholder with both the job
  summary text AND the delivery blocks — one top-level message carries
  the whole batch.

## R2.5 Debug tooling for sales/purchase triage

- [`scripts/debug_classify_direction.py`](../../scripts/debug_classify_direction.py) — given a PDF, prints
  - Gemini classifier's `issuer_name` / `bill_to_name` / `doc_type` / `confidence`
  - `resolve_direction()` outcome (purchase / sales / unknown / self_referential)
  - Understand-extract `vendor_name` / `customer_name` / `summary_table`
  - Drifts between classifier and understand extract

  Use when a doc routes the wrong way: the script tells you whether
  Gemini misread the parties (classifier) or the parties came through
  the understand extract but the deterministic direction disagreed.

- `eval/client_eval.py` — run with `--client "Company-A" --limit-per-client 20` for direction accuracy. Baseline pre-Task-2 was ~60%; live retest is the next QA step (this round did not run it — needs the user's local Acme Client corpus).

## R2.6 ADR — Cloud Pub/Sub job queue (proposed, not implemented)

[`docs/adr/0012-batch-job-queue.md`](../../adr/0012-batch-job-queue.md) documents the future worker pattern (Bolt publishes → Cloud Run worker processes with per-channel lease). Implementation deferred — Phase 1 already delivers the user-visible UX win at zero infrastructure cost.

