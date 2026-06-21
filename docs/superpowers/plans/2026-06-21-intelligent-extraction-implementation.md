# Implementation Plan — Intelligent Extraction & Faithful Mapping (2026-06-21)

**Authoritative research:** [`docs/superpowers/specs/2026-06-21-intelligent-extraction-research.md`](../specs/2026-06-21-intelligent-extraction-research.md)
(Parts I–III findings + §9 live spikes + §10 COA decision). This plan turns that spec into sequenced,
eval-gated work. **Branch:** `feat/ledgr-my-sst-correctness` (all work lands here; no separate branch).

**North star (every task serves this):**
> The printed document + the client's uploaded master are the only sources of truth. The model
> transcribes faithfully into structured JSON; code maps to ERP columns deterministically; unknowns
> **fail loud / flag for review**, never silently substituted. No per-vendor/per-type/keyword business
> rules. Because this is a Slack app with **no Sentry view of wrong-but-plausible output**, every silent-
> corruption path must be gated by a deterministic self-check or a human flag.

**Delegation model** ([[lead-not-do-delegate-execution]]): the orchestrator writes/owns this plan, ADRs,
eval design, and drives live QA; **all code + test authoring goes to an `executor` (Sonnet, `opus` for
the array-schema/COA reasoning work); a separate `code-reviewer`/`verifier` pass approves** — never self-
approve in the same context. Each task below names its delegate and its acceptance gate.

**Global test command:** `uv run pytest -q 2>&1 | tail -20` · lint `ruff check`. Baseline before WS-1
must be captured (current suite green count). No task is "done" until its eval gate passes AND a verifier
pass confirms evidence.

**Guiding constraints (carried, non-negotiable):** record currency as printed, no FX
([[currency-record-as-shown]]); never commit real client/vendor names ([[no-real-client-data-in-repo]]);
restart bot + adk web from HEAD before live QA ([[restart-bot-before-qa]]); ledger accumulates, never
delete prior versions ([[ledger-accumulates-keep-record]]).

---

## WS-0 — Eval harness & build-time verifications (FIRST — the measuring stick for everything)

Nothing downstream is trustworthy without the gate it's measured against. Build the evals and resolve
the two open build-time checks before touching production logic.

| Task | What | Acceptance gate | Delegate |
|------|------|-----------------|----------|
| 0.1 | **COA golden eval** `tests/integration/test_coa_eval_jbi.py`, gated on JBI data present locally (pattern of `test_erp_golden_format.py`). Ground truth = JBI `COA & List.xlsx` (Party List + COA) + `LocalRecon_VertexPrompt_LedgerRows.json` + ~30 min manual line→code annotation (orchestrator supplies annotations; executor wires the test). Scenarios per spec §6: entity-exact, entity-by-regno, category, keyword, **ambiguous→must-flag**, new-vendor, **no-match(salary)→blank+flag**, multi-line diff accounts, qty>1, MY-vs-SG, credit-note sign-flip. | Test runs end-to-end (PDF→categorize→ERP row). Metrics computed: top-1 ≥ 0.85, **flag-recall = 1.0 (hard)**, flag-precision ≥ 0.80, and the **ZERO-TOLERANCE assertion: no exported `account_code` outside the client COA (or blank)**. Initially may FAIL (pre-fix) — that's the baseline. | executor + orchestrator (annotations) |
| 0.2 | **Extraction golden** `tests/integration/fixtures/jbi_golden.json` + a test calling `process_file_event`/the extraction fn. Entries: `expected_doc_count`, per-doc `grand_total`/`must_reconcile`, page-coverage. Confirmed: M PREMIUM=2, ATOM=11, AUTO LAB=6 (skip cover), GDEX=1. **ADD a ≥3-invoices-on-one-page segmentation-stress doc and a Malay/Chinese doc.** | Test asserts doc-count, per-doc reconcile (±tol), page-coverage (union==pages, no gaps/overlaps). M PREMIUM=2 is the regression guard. Baseline may fail pre-WS-2. | executor |
| 0.3 | **Build-time verification A:** re-run the enum-in-nested-array spike against **Vertex 2.5 Flash-Lite (prod backend)**, not just AI Studio Flash (§9 caveat). | Documented result: structural (0 out-of-set) on Vertex Flash-Lite, or a recorded deviation that changes the COA design. | executor |
| 0.4 | **Build-time verification B:** confirm ADK **Tool Confirmation** compatibility with our **Firestore session service** (docs exclude `DatabaseSessionService`/`VertexAiSessionService`). | Decision recorded: Tool Confirmation usable, OR fall back to `RequestInput` human-input graph node for COA HITL. Feeds WS-3. | executor + orchestrator |

**Exit:** evals exist and run (red is fine); both verifications resolved. ADR stub: `docs/adr/00XX-faithful-extraction-and-coa-confidence.md` opened by orchestrator.

---

## WS-1 — Correctness quick wins (independent, low-risk, ship first — restores visibility)

These don't depend on the array schema and directly fix money + the blind delivery path the user is most
worried about. Land + verify before the bigger WS-2/3.

| Task | What | Acceptance gate |
|------|------|-----------------|
| 1.1 | **MAP1 money bug (verified YAML):** `autocount.yaml`/`sql_account.yaml` `Amount`/`_AMOUNT`: `unit_price → sub_total`. SQL also map `_UNITPRICE → unit_price`, `_QTY → qty`. Keep `InclusiveTax=F`. (§9 Spike B — `Amount` is tax-exclusive line net.) | A qty>1 line exports `Amount` = line net, not net÷qty (0.2 qty>1 case passes). Golden ERP format tests still green. |
| 1.2 | **MAP2 reconcile note:** `compose_confident_note` reads via `exporter.column_for_field("sub_total"/"currency", doc_type)`, not literal `"Net Amount"`/`"Currency"`. (`nodes.py:2250,2256`) | The "reconciles to $X" total renders non-blank on a real delivery. |
| 1.3 | **AR2 batch-path notes:** render import-readiness + confident notes on the **batch-aggregate** delivery path (`_build_batch_aggregate_blocks`), not only `_post_delivery_card`. | Multi-file drop shows readiness + reconcile note (the common path is no longer blind). |
| 1.4 | **Delivery visibility:** add per-document **reconciles ✓/✗** cell + a **flag-reason breakdown** (blank-account / tax-unresolved / jurisdiction-unresolved) to the batch card. | Card shows ✓/✗ per doc and a reason breakdown; counts already computed, breakdown no longer discarded. |
| 1.5 | **Runtime fail-loud flags** into `detect_struggle`/HITL: `blank_account_code` (CRIT), `account_code_not_in_coa` (force blank+flag), **`jurisdiction_unresolved` (D1 CRIT — flag before `get_tax_classifier(None)` silently SG-defaults)**, per-row required-field check in `ProfileLedgerExporter.rows()`, `currency_mismatch` (MY doc defaulted SGD). | Each flag unit-tested; an MY doc with lost `reference_yaml` flags instead of silently booking SG codes. |

**Delegate:** executor (Sonnet). **Verify:** code-reviewer + run the suite + a live Slack QA of a multi-
file MY drop (orchestrator drives; restart bot from HEAD first). **Exit:** money correct, delivery card no
longer blind, suite green.

---

## WS-2 — Faithful multi-document extraction (THE core fix — gated)

Replace the single-document cage with a faithful array, behind deterministic self-checks. **Do not ship
without G1–G5.** Model: `opus` executor for schema/prompt design.

| Task | What | Acceptance gate (the G-gates) |
|------|------|-------------------------------|
| 2.1 | **Array schema as default:** `documents: list[ExtractedDocument]`, each `{doc_type(enum, classify in-call), page_range, vendor/buyer/reference/date/currency (verbatim), lines: list[LineItem] (verbatim, presentation="itemized"\|"summary"), subtotal/tax_total/grand_total (as printed), tax_lines: list (N, not forced 2)}`. One call per FILE (spec §1). Replace the list-of-1 wrap (`process_invoice_document.py:179`). | 0.2 doc-count assertions pass incl **M PREMIUM=2** (regression guard) and the segmentation-stress doc. |
| 2.2 | **G1 per-doc reconcile:** wire `reconcile()` over EACH array element; fail → `reconciled=False`+flag. | Every doc reconciles within **G4 tolerance** (defined per-currency, integer cents); a planted mismatch flags. |
| 2.3 | **G2 page-coverage:** union of `page_range` == input pages, no gaps/overlaps → else flag "segmentation uncertain". | Planted merge/split/drop is caught by the assertion (0.2 page-coverage test). |
| 2.4 | **G3 doc-count surface:** "extracted N documents from M pages" on the delivery card. | Visible on card. |
| 2.5 | **G5 partial-failure semantics:** deliver good docs, **flag the gap loudly**, never silent drop (e.g. model returns 3 of 4, or one fails G1). | Defined + tested behavior; no silent drop. |
| 2.6 | **M1 grouping = verbatim by default:** import lines as printed; grouping ONLY if an ERP profile declares it needs it, rule in YAML not code. Remove the summary-granularity prompt pressure (A2). | GDEX faithful-to-its-own-summary + a multi-line parts invoice stays itemized. |

**Delegate:** executor (opus). **Verify:** verifier + live adk-web QA (segmentation-stress + non-English
docs) + live Slack. **Exit:** 0.2 green; multi-invoice PDFs no longer lose docs; mis-segmentation is
caught, not shipped. **ADR:** record the single-faithful-schema decision.

---

## WS-3 — COA trustworthiness (the correctness-critical step — gated by 0.1)

Per spec §10: **no thinking, no RAG, no agent loop at this scale** — add the four levers to the existing
`categorizer.py` LLM fallback. It's close to right; this hardens it.

| Task | What | Acceptance gate |
|------|------|-----------------|
| 3.1 | **`UNMAPPED` sentinel + abstention:** object schema `{account_code: enum(<client codes> + "UNMAPPED"), confidence, reasoning, alternative_codes[]}`, nullable allowed, flat enum; prompt instructs abstaining is correct when nothing fits. (Fixes Spike-A no-abstain.) | 0.1 **no-match (salary) → UNMAPPED → blank+flag**, never force-fit. |
| 3.2 | **Enum-constraint on real keys** (spike-confirmed structural; keep post-validation as a semantic sanity check, downgraded from correctness gate). | 0.1 **zero-tolerance: no code outside client COA**, asserted through the exporter. |
| 3.3 | **Logprob confidence gate** replaces the self-reported-confidence threshold: `responseLogprobs:true, logprobs:5`; gate on `avgLogprobs` + top-1→top-2 margin → low/narrow → HITL. (Self-reported `confidence` advisory only.) | 0.1 **flag-recall = 1.0**; ambiguous-two-accounts case escalates. |
| 3.4 | **M2 flag propagation:** add `account_flagged` to `InvoiceLine`; carry from `AccountResolution.flagged` (dropped today at `categorizer.py:355`) → row → delivery card → readiness note. | Low-confidence COA pick is visibly distinct from a confident one on the card. |
| 3.5 | **HITL wiring** per 0.4 decision: ADK Tool Confirmation `require_confirmation=threshold_fn` (logprob gate, mirror docs `amount>1000`), reviewer picks from `alternative_codes[]`; OR `RequestInput` node if Firestore-incompat. | A flagged/UNMAPPED line routes to a human in Slack; resume applies the chosen code. |

**Delegate:** executor (opus). **Verify:** verifier + 0.1 metrics + live QA of an ambiguous + a no-match
line. **Exit:** 0.1 passes all gates incl flag-recall=1.0 and zero-tolerance. Thinking stays OFF (small
budget reserved only for abstain-boundary audit summaries, optional, not in this WS).

---

## WS-4 — De-hardcode & restore authority (kill the brittle lexicons/defaults)

| Task | What | Acceptance gate |
|------|------|-----------------|
| 4.1 | **Kill keyword lexicons/overrides** (Anti-pattern 2): delete `_telco_ledger_lines` synthesizer + telco-2-line prompt rule, reimbursement override, SG zero/exempt signal-word override, `_TELCO_MARKERS`, country-prefix heuristic. Printed value authoritative; keywords demoted to tie-break-only soft hints + flag. | Telco/expense/zero-rated docs transcribe printed lines (1/2/3/N) with the document's own labels; arithmetic reconcile still picks the band. Non-English doc no longer mis-handled. |
| 4.2 | **One resolver per axis, blank+flag not substitute** (Anti-pattern 3): jurisdiction/software/currency/account each resolve via one function that flags unknowns. Remove SG/SGD/qbs/SINGAPORE literal defaults (D1–D7). | An unresolved axis flags; no silent SG/SGD/QBS substitution. |
| 4.3 | **Tax-code master authoritative for ALL exporters** (T1/T2/T3): route Xero through `resolve_tax_code` (not `clf.tax_code` direct, `exporters.py:263`); empty client list → blank+flag (not YAML guess); fix AutoCount `ES` rate-keying. | Xero client's uploaded `Tax_Codes` win; missing master surfaces in import-readiness, not a guessed code. |

**Delegate:** executor. **Verify:** code-reviewer + suite + targeted eval (telco/zero-rated/Xero cases).
**Exit:** no keyword business rules remain on the live path; tax-code path matches account-code discipline.

---

## WS-5 — Consolidate architecture (remove the second copies that drift)

| Task | What | Acceptance gate |
|------|------|-----------------|
| 5.1 | **Single extraction path** (AR1): retire `capture_book` + `legacy` divergence into the one faithful WS-2 path (or clearly quarantine legacy SOA behind an explicit, tested switch). | One default path; dead multi-doc schemas removed or promoted. |
| 5.2 | **Profile-derived preview columns = Excel/Slack parity** (AR4 + user's header-parity requirement): derive the Slack preview spec from the SAME `erp_profiles/<erp>.yaml` that drives Excel; delete the hand-coded `_AUTOCOUNT_*`/`_SQL_*` lists + `preview_column_spec` switch. | Switching a client to SQL re-keys BOTH Excel and the Slack table automatically; new ERP = YAML only. |
| 5.3 | **One software-label fn** (AR3): collapse `software_label`/`_software_label_for_summary`/`_normalize_software` to one. | Adding an ERP touches one label site. |
| 5.4 | **M3 dedup under array schema:** define per-array-element identity (file_id + page_range + reference); prove re-dropping a multi-invoice PDF is idempotent (no double-post). Address AutoCount lost invoice identity (MAP5, `DocNo="<<New>>"`). | Re-drop test: N docs, no duplicate ledger rows. |

**Delegate:** executor. **Verify:** verifier + live QA (switch a channel ERP; re-drop a batch). **Exit:**
new ERP = YAML-only; one file→N docs is idempotent; header parity is structural.

---

## WS-6 — Compliance & cost (last; measure, don't guess)

| Task | What | Acceptance gate |
|------|------|-----------------|
| 6.1 | **Scrub real client data from prompts** (P1/P2/P3): remove `AAI-`/`IA-`/`CNA-` ref regexes, the "YAU LEE" anecdote, English SOA sentinels; replace with model-driven grouping signals (`document_group_id`/`page_role`). | No real vendor/ref strings in prompts/code; grep clean. |
| 6.2 | **Context caching** (biggest cost lever): ADK `ContextCacheConfig` (`min_tokens≥2048`, `ttl_seconds`, `cache_intervals`) + `static_instruction` for the reused extraction-instruction + COA prefix. Measure token delta. Note: enum schema is out-of-band (+0 prompt tokens, §9), so caching the prompt prefix is unaffected by per-client enums. | Measured cached-token discount on repeated docs; don't mutate cached COA mid-TTL. |
| 6.3 | **`thinkingBudget=0`** on the extraction + default COA calls (spec §10); small budget reserved only for flagged abstain-boundary (optional, audit summaries). Drop `mediaResolution=LOW` from the PDF plan (no page savings). | Verified config; no thinking on the easy path. |
| 6.4 | **Sentry trend logging** (Sentry MCP is connected) — structured events `{client_id, vendor, reconciled:false, reason, confidence}` on `reconciled=False`/`blank_account_code`, **after** WS-1.5 flags exist. Optional `responseLogprobs` capture surfaced once reconcile gates prove out. | Reconcile-fail trend visible in Sentry; no value before the pipeline can self-detect. |

**Delegate:** executor. **Verify:** code-reviewer + cost measurement evidence. **Exit:** prompts clean of
client data; cost levers measured; trend observability live.

---

## Sequencing & dependencies

```
WS-0 (evals + verifications)  ──┬──────────────────────────────────────────────►
                                │
WS-1 (correctness quick wins) ──┤ independent, ship first  ─────────────────────►
                                │
WS-2 (faithful array) ◄─ gated by 0.2 ──┐
WS-3 (COA trust)      ◄─ gated by 0.1 ──┤ (2 & 3 parallelizable after WS-0/1)
                                         │
WS-4 (de-hardcode)   ◄─ after 2 (printed-value authoritative depends on faithful lines)
WS-5 (consolidate)   ◄─ after 2 & 4 (single path needs the faithful path proven)
WS-6 (compliance+cost) ◄─ last (measure on the consolidated path)
```

**Eval-gated discipline:** every WS closes only when its named eval gate is green AND a verifier pass
records evidence. WS-0 evals are the contract; if a later change reddens them, that change isn't done.

## Decisions needing an ADR (orchestrator writes)
- **ADR-00XX** Faithful single-schema multi-document extraction (replaces single-doc cage; G1–G5 gates).
- **ADR-00XX** COA confidence & abstention (enum + `UNMAPPED` + logprob gate + HITL; thinking OFF; no RAG
  at current scale — record the scale threshold that would flip to embeddings).
- (Possibly) ADR on profile-derived preview columns as the single ERP-layout source (Excel/Slack parity).

## Out of scope (explicitly deferred)
- Embeddings/RAG for COA (until COA reaches thousands of codes — record threshold in ADR).
- Gemini Batch/Flex API (offline backfills/evals only — never interactive Slack, §3).
- SQL e-invoice/MyInvois header block (IRBM/MSIC) — future.
- Agentic reflection loop for COA — reserved for failed-gate triage only, not the default path.

## First action on approval
Orchestrator opens the two ADR stubs and supplies the JBI COA annotations; executor starts **WS-0.1 +
WS-0.2** (evals) and **WS-1.1 (MAP1)** in parallel — the evals establish the baseline, MAP1 is the
lowest-risk money fix. Restart bot + adk web from HEAD before any live QA.
