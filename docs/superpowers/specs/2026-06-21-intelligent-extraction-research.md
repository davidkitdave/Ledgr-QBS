# Intelligent Extraction & Faithful Mapping — Findings + Research (2026-06-21)

Grounded in the official **adk.dev** / **ai.google.dev** corpora (Google-dev-knowledge MCP) plus a
three-part codebase audit (extraction, tax/jurisdiction, normalize/map/deliver).

**Guiding principle (the spec's north star):**
> The **printed document** and the **client's uploaded master data** are the only sources of truth.
> The model TRANSCRIBES each document faithfully into a structured JSON schema (every distinct
> invoice; every line item; subtotal/tax/total exactly as printed). Code maps JSON → ERP columns
> **deterministically**. Unknowns **fail loud / flag for review** — never silently substituted.
> No per-vendor, per-doc-type, or keyword-driven business rules baked into prompts or code.

Everything below is a deviation from that principle, ranked, with file:line and the intelligent
alternative. The findings cluster into **7 recurring anti-patterns** — these are the "classes of
thing we'll keep hitting" to design out.

---

## Anti-pattern 1 — Lossy / single-document extraction (THE CORE ISSUE)

| ID | Sev | Finding | Where |
|----|-----|---------|-------|
| A1 | **HIGH** | Default "Understand" path makes ONE Gemini call → flat `DocumentLedgerExtract` (no `documents[]` list) → wrapped in **list-of-1**. A multi-invoice PDF loses N-1 invoices. **Confirmed live: M PREMIUM 2→1 (RM60 dropped).** | `process_invoice_document.py:171`; `ledger_extract.py:358` |
| A2 | **HIGH** | Prompt forces **bookkeeper-summary granularity** ("ledger_lines… not every itemized row"; "Produce the SMALL set of summary lines"). The JSON is lossy before any mapping — contradicts "exactly as printed". | `ledger_extract.py:67`; `invoice_extractor.py:140` |
| A3 | MED | Multi-document schemas ALREADY EXIST but are **dead code** on the live path: `ExtractedInvoiceBundle.invoices: list`, `DocumentRecordBundle.documents: list`, `DocumentRecord.line_items` ("do NOT collapse"). `EXTRACT_BUNDLE_FN` is wired but never called. | `nodes.py:364`; `document_record.py:63` |

**Fix (single highest-leverage change):** make the default schema a **list of faithfully-transcribed
documents** — `documents: list[ExtractedInvoice]`, each with `lines: list[LineItem]` (verbatim) +
printed `subtotal/tax_total/grand_total` + `presentation: "itemized"|"summary"` + `page_range`. One
multimodal call handles segmentation + itemization + totals (Gemini does up to 1000 pages/call).
This kills A1+A2 together and is **cost-neutral on call count**. Move any bookkeeper-grouping to a
separate, optional, deterministic post-step keyed on ERP column needs — never the extraction prompt.

---

## Anti-pattern 2 — Hardcoded lexicons & vendor/type rules deciding business outcomes

The system repeatedly **string-matches descriptions or vendor names** to pick a tax/line outcome,
instead of reading what the invoice prints. Every one of these silently fails on a document phrased
differently (or in Malay/Chinese), and several OVERRIDE the printed arithmetic.

| ID | Sev | Finding | Where |
|----|-----|---------|-------|
| K1 | **HIGH** | "Telco/utility → **exactly 2 lines (SR+ZR)**" baked into 3 prompts AND a deterministic synthesizer that **replaces real captured lines** with 2 fabricated buckets. | `ledger_extract.py:90`; `invoice_extractor.py:127`; `document_normalizer.py:82,456` (`_telco_ledger_lines`) |
| K2 | **HIGH** | SG `zero_rated`/`exempt`/`no_tax` **signal lexicons** (`idd`, `roaming`, `air freight`, `salary`, `cpf`…) choose tax treatment at 0.9-0.95 conf and run BEFORE the arithmetic reconcile — overriding the printed tax. | `sg_gst.yaml:39-93`; `tax_classifier.py:394-501` |
| K3 | MED | MY `carve_out` keyword list (`telecom/parking/logistics/freight…`) used to narrate 6%-vs-8%. (Arithmetic reconcile already picks the right band — keywords are redundant + brittle.) | `my_sst.yaml:111`; `tax_classifier.py` |
| K4 | MED | Vendor-name markers `_TELCO_MARKERS = ("m1 ","simba","broadband"…)` branch line behavior on brand identity. MY telcos (Maxis/Celcom/Digi) miss; a non-telco line containing "broadband" mis-collapses. | `document_normalizer.py:46` |
| K5 | MED | Expense-claim keyword routing + override that **replaces all receipt lines** with one synthesized "Expense reimbursement" line. | `document_normalizer.py:213,253` |
| K6 | MED | Country inference via prompt heuristic "SG GST regno starts with M / MY SST starts with country code" — a factual oversimplification that mis-jurisdictions. | `ledger_extract.py:79`; `invoice_extractor.py:84` |

**Fix:** the **printed rate/code/amount is authoritative**; reconcile arithmetically (already done well
in `_reconcile_tax_line`). Demote ALL keyword lexicons to soft hints that only break ties when nothing
is printed, and **flag** rather than assert high confidence. Delete `_telco_ledger_lines` and the
reimbursement override — transcribe whatever tax-grouped subtotals the document itself prints (1, 2, 3,
or N lines, with the document's own labels). Jurisdiction from structured fields in the deterministic
router, not a prompt anecdote.

---

## Anti-pattern 3 — Silent error-hiding defaults (substitute instead of fail-loud)

Unknown values are silently replaced with a (usually SG/SGD/QBS) default, masking data loss and
mis-booking for MY / multi-ERP clients. The fix theme is identical everywhere: **default to blank +
flag for review**, or use an explicit client-profile value — never a literal.

| ID | Sev | Finding | Where |
|----|-----|---------|-------|
| D1 | **CRIT** | `tax_classifier` defaults to **SG GST** (`sg_gst.yaml`) on ANY unresolved jurisdiction (`get_tax_classifier(None)`, unknown region → SG). An MY doc whose `reference_yaml` is lost gets SG codes + 9% guards. | `tax_classifier.py:24,66,69,80,114`; `nodes.py:2025` |
| D2 | **HIGH** | `AccNo` (GL account) defaults to the **creditor code** when no COA resolves → posts expense to the AP control account. **Confirmed live: M PREMIUM AccNo=400-M0001.** | account-mapping path (categorize → exporter) |
| D3 | **HIGH** | Unknown `software` → **`qbs`** in 6+ scattered sites (some warn, most silent). A Xero/AutoCount client whose display-string didn't normalize exports as QBS columns. | `nodes.py:1801,2018`; `app/blocks.py:39`; `slack_runner.py:817,1040,1262,1318` |
| D4 | **HIGH** | `_classify_sales` defaults every unmatched line to **SR @ 0.9 conf, not flagged** — books output tax not on the document. | `tax_classifier.py:503` |
| D5 | MED | `is_overseas` property **hardcodes SG** home country; any caller using it treats MY suppliers as overseas. (Deprecated in its own docstring, still callable.) | `models.py:37` |
| D6 | MED | Bank/summary currency `or "SGD"` in 6+ places (sheet title, dedupe ident, exporter, money formatting); dataclass default `currency="SGD"`. Mislabels MYR; can collide dedupe keys. | `nodes.py:1989,1995,2248`; `exporters.py:814,1169`; `app/blocks.py:661+`; `models.py:93,163` |
| D7 | MED | `legacy`/`legacy_profile` → **"SINGAPORE","SGD"** literal; `LEDGR_DEFAULT_REGION` env override. Forces SG on any legacy-tagged profile. | `jurisdiction.py:265`; `client_context.py:367` |

---

## Anti-pattern 4 — Client master not authoritative for tax codes

The **account-code** path is the gold standard (client master → COA → blank+flag, LLM constrained to
the client's own keys, hallucinated keys nulled). The **tax-code** path does NOT match that discipline.

| ID | Sev | Finding | Where |
|----|-----|---------|-------|
| T1 | **CRIT** | **Xero exporter bypasses the client tax-code master** — calls `clf.tax_code(...)` directly instead of `resolve_tax_code(...)`, so a Xero client's uploaded `Tax_Codes` are ignored and the YAML seed always wins. | `exporters.py:263` vs `:367` |
| T2 | **HIGH** | When the client tax-code list is **empty**, the YAML seed string wins with **no blank+flag guard** (unlike account codes) → emits guessed `TX`/`NT`/`SR` that may not be the client's real import codes. | `code_resolver.py:101-107`; `sg_gst.yaml:32` |
| T3 | MED | AutoCount sales `ES: "ESV-8"` hardcoded to the 8% variant (purchase side is correctly rate-keyed) → wrong code for a 6% or future-rate exempt sale. | `my_sst.yaml:90` |

**Fix:** route ALL exporters (incl. Xero) through `resolve_tax_code` with `rate`; treat YAML
`code_map` strictly as a **flagged fallback**; for non-QBS ERPs, surface "tax-code master needed" in
import-readiness rather than emitting a guess.

---

## Anti-pattern 5 — Column-mapping correctness (the JSON→header step)

The deterministic mapping is the right architecture, but several call sites **bypass
`column_for_field`** and guess header strings, and one mapping is arithmetically wrong.

| ID | Sev | Finding | Where |
|----|-----|---------|-------|
| MAP1 | **CRIT** | AutoCount `Amount` & SQL `_AMOUNT` map to **`unit_price` (= net ÷ qty)**, not the line net. Any **qty > 1** line imports a value divided by qty → ledger understated. (Masked today: JBI lines are mostly qty 1.) **Verified:** `exporters.py:392` + `autocount.yaml:96`. | `autocount.yaml`/`sql_account.yaml` `*_fields`; `exporters.py:392` |
| MAP2 | **HIGH** | `compose_confident_note` reads `row.get("Net Amount")` / `row.get("Currency")` — **no exporter emits those keys** → the "reconciles to $X" total is always blank. Half-patched: account-code lookup was made profile-aware, amount/currency was not. | `nodes.py:2250,2256` |
| MAP3 | **HIGH** | `_build_preview_rows` + `collect_export_unmapped_summary` use long `or row.get(...)` header guess-chains that **omit AutoCount/SQL headers** (returns blank date/account/net for profile ERPs; needs an append per new ERP). | `slack_runner.py:1397`; `exporters.py:616` |
| MAP4 | MED | Xero `Total` written onto **every** line of a multi-line invoice (grand-total repeated N times). | `exporters.py:282` |
| MAP5 | MED | AutoCount has **no `invoice_number` field mapping** → `column_for_field("invoice_number")` is None → dedup/attribution loses invoice identity (DocNo is the constant `<<New>>`). | `autocount.yaml` `purchase_fields` |

**Fix:** add a `sub_total`/`line_net` context key and map `Amount`/`_AMOUNT` → it; route EVERY
delivery/preview/dedup column lookup through `exporter.column_for_field(...)` — delete the guess-chains.

---

## Anti-pattern 6 — Architecture debt (drift & duplication that invites new bugs)

| ID | Sev | Finding | Where |
|----|-----|---------|-------|
| AR1 | MED | **Three parallel extraction stacks** with divergent fidelity philosophies (understand=default/lossy, capture_book=opt-in, legacy=SOA/heuristic-dense). Default uses the worst. Consolidate to ONE faithful path. | `ledger_extract.py` / `book.py` / `document_normalizer.py` |
| AR2 | MED | Import-readiness + confident notes render on the **single-file** path (`_post_delivery_card`) but the **live batch-aggregate** path omits them — multi-file droppers (who need the ERP-code checklist most) see nothing. | `slack_runner.py:1071,1107` |
| AR3 | MED | **Three software→label/key functions** hand-kept in sync: `software_label`, `_software_label_for_summary` (byte-identical dup), `_normalize_software`. Add ERP #5 to one → others show raw key / mislabel "QBS Ledger". | `app/blocks.py:21,43`; `nodes.py:2125` |
| AR4 | MED | Slack **preview columns are a hand-maintained second copy** of each ERP's layout (`_AUTOCOUNT_*`/`_SQL_*` lists + 4-function branch ladder). A new ERP needs YAML **and** code edits in 2 files. Derive preview cols from the exporter/profile instead. | `app/blocks.py:60-179` |

---

## Anti-pattern 7 — Real client data & overfitting baked into code (compliance risk)

Violates the project's "no real client/vendor data in repo" rule AND overfits behavior to specific docs.

| ID | Sev | Finding | Where |
|----|-----|---------|-------|
| P1 | MED | Hardcoded client/vendor reference-format regexes: `AAI-\d{2}-\d{3}`, `^(IA|CNA)-\d+$`, `^\d{2}-D\d{2}$` drive merge/SOA-drop decisions. A different firm's refs don't match. | `record_merge.py:25,58`; `document_normalizer.py:45` |
| P2 | MED | The "**YAU LEE** Malaysia receipt was wrongly processed under SG GST" anecdote is quoted in 3 extraction prompts (real vendor name + overfit rule). | `ledger_extract.py:79`; `invoice_extractor.py:84,172` |
| P3 | LOW | SOA "phantom" drop uses an English sentinel set (`{"","INVOICE","INVOICES"}`) + invoice-ref regex `^[A-Z]{2,5}-\d{3,6}$` — can drop a REAL one-line invoice or miss a differently-phrased SOA. | `invoice_extractor.py:554`; `document_normalizer.py:604` |

**Fix:** segmentation/grouping signals come from the model (`document_group_id`, `page_role`,
`skipped_pages`) — schema-driven, not regex on client-specific formats. Scrub real names from prompts.

---

## Recommended build order (eval-gated at every step)

1. **A1 + A2** — new single-call, list-of-documents, faithful-transcription schema as the default
   path; deterministic JSON→column mapping retained. (Closes the core multi-invoice + fidelity ask.)
2. **MAP1 + MAP2 + MAP3** — fix `Amount`=line-net; route all column lookups through `column_for_field`.
   (Correctness — the money bug ships first.)
3. **Anti-pattern 2** — delete `_telco_ledger_lines`, reimbursement override, and keyword-driven
   treatment; printed value authoritative + flag.
4. **Anti-pattern 3** — one `resolve_*` per axis (jurisdiction/software/currency/account) that flags
   unknowns instead of substituting SG/SGD/QBS/creditor-code.
5. **T1/T2** — client tax-code master authoritative for ALL exporters; YAML = flagged fallback.
6. **AR2/AR3/AR4** — single delivery path with notes; one label resolver; profile-derived preview cols.
7. **P1/P2/P3** — model-driven grouping; scrub real client data from prompts.
8. **Cost pass** — Batch API (50% off: Flash-Lite $0.05/$0.20), `mediaResolution=LOW`,
   `thinkingBudget` cap, ADK `ContextCacheConfig` for large shared prefixes; measure tokens.

**Eval golden set (JBI real docs):** M PREMIUM=2 docs · ATOM=11 (SOA) · AUTO LAB=6 (SOA) ·
GDEX=1 line faithful to its own summary · a multi-line parts invoice itemized · a qty>1 line for MAP1.
Chat lane = agents-cli eval; doc lane = pytest integration on `process_file_event`.

---

## What's healthy — keep (don't refactor away)

- `ProfileLedgerExporter` + YAML `purchase_fields`/`constants`/`required_*` — new ERP = YAML add (modulo AR4).
- `column_for_field` inversion — correct single-source mechanism; bugs are where call sites bypass it.
- The **account-code** discipline (client master first, LLM constrained to client keys, blank+flag) —
  this is the template the tax-code path should copy.
- `collect_import_readiness` / `format_import_readiness_note` — genuinely ERP-agnostic.
- Bank continuous-chain rebuild — explicit dedupe-by-signature, no silent row loss.
- **Batch fan-out** (`asyncio.gather` + `_SEM` cap 5 + per-doc sessions + deferred single fan-in
  write, double-locked) — already concurrent and correct (see Part II §3). Don't rebuild it.
- **COA resolution** (`categorizer.py`) — deterministic-first → LLM with the client's full COA inlined
  → exact-key pick, hallucinated keys nulled, no-match blank+flag. Already follows the north star;
  Part II §4 only *adds* confidence + enum-constraint, it does not rewrite this.

---
---

# PART II — Intelligence architecture (ADK/Gemini-grounded answers)

Grounded in the official **ai.google.dev** / **docs.cloud.google.com** (Gemini, Vertex) / **adk.dev**
corpora via google-dev-knowledge + adk-docs MCP, cross-checked against a second read-only codebase
audit (COA, column mapping, batch, confidence). Part I = what's wrong; Part II = the intelligent
target shape for the seven questions the user raised, **with the cheapest correct mechanism for each**.

**Confirmed fault line (user's hypothesis is correct):** the multi-invoice loss is *purely Python-side*.
The default extract call sends Gemini a **single-document** `response_schema` (`DocumentLedgerExtract`,
`ledger_extract.py:382-391`) and wraps the one result in a **list-of-1** (`process_invoice_document.py:179-186`).
Gemini is never *offered* the chance to emit N documents. The model is fine; **our schema is the cage.**

## §1 — ONE call: classify + segment + extract → array of documents

**Verdict: a single multimodal call CAN do all three** — Gemini document understanding "goes beyond
text extraction," processes **up to 1000 pages / 50 MB** per call (258 tokens/page), and emits
**structured JSON** via `responseMimeType:"application/json"` + a nested `responseSchema`. A root
**array of document objects**, each holding a nested **array of line items**, is the exact documented
shape (Vertex "recipes" sample = root `ARRAY`→`OBJECT`→nested `ingredients ARRAY`).

**Target schema (replaces the single-doc cage):**
```
documents: list[ExtractedDocument]
  ExtractedDocument:
    doc_type            # enum — classify in-call (invoice|receipt|statement|credit_note|…)
    page_range          # which pages this doc occupies (segmentation evidence)
    vendor / buyer / reference / date / currency   # verbatim
    lines: list[LineItem]      # every printed row, verbatim, presentation="itemized"|"summary"
    subtotal / tax_total / grand_total              # exactly as printed
    tax_lines: list[{label, rate, base, amount}]    # whatever tax groupings the doc prints (N, not forced 2)
```
This kills A1 (multi-invoice loss) **and** A2 (forced summary) together, **cost-neutral on call count**
(same one call, richer schema). Declare schema keys in the **order you want emitted** — "the model
produces outputs in the same order as the keys in the schema."

**1-call vs 2-call — when each is right (doc-grounded):**
- **1 call (default):** clean digital PDFs, one document per page, or a handful of clearly separated
  docs. Segmentation-in-schema is reliable here. This is our normal Slack drop.
- **2 calls (escalate):** the docs explicitly warn the model "aren't precise at locating
  text/objects" and "might hallucinate handwritten text." So escalate to pass-1 = enumerate documents
  + page ranges, pass-2 = per-document extraction **only when** (a) multiple distinct invoices share
  one page, (b) scanned/handwritten, or (c) you want per-document cost/confidence isolation. Gate the
  second pass on a low confidence signal (§5), not on every upload — keep the common path single-call.

## §2 — Intelligent column mapping + ERP-header PARITY (Slack table === Excel)

**The user's rule:** the client is locked to one ERP; the Slack data table **and** the Excel must show
that ERP's headers. **Today they diverge** (audit Q3): Excel columns are **YAML-driven**
(`ProfileLedgerExporter` reads `purchase_cols`/`purchase_fields` from `erp_profiles/<erp>.yaml`), but
the **Slack preview columns are hand-coded constants** in `app/blocks.py` (`_AUTOCOUNT_*`, `_SQL_*`, …
+ a 4-branch `preview_column_spec` switch). They're coupled only by *convention* — `PreviewColumn.row_key`
strings must be hand-matched to the exporter's emitted keys or the Slack cell renders "—". **A new ERP
must be defined in 3 places** (YAML, blocks.py preview lists, the software-key normalizer).

**Intelligent mapping is already the right idea — it's just not single-sourced.** `column_for_field`
inversion is the correct deterministic JSON→header mechanism. **Fix = derive the Slack preview spec
from the SAME ERP profile that drives Excel** (read `profile["purchase_cols"]` + a small
`display_priority`/`preview` hint in the YAML), deleting the `app/blocks.py` column copies. Then header
parity is structural, not maintained: switch a client to SQL → both surfaces re-key off `sql_account.yaml`
automatically. (This is AR4, now load-bearing for the user's parity requirement.) The mapping itself
stays **deterministic** — ERPs differ only in header *names*, a fixed per-profile dictionary, not a
task that needs model intelligence.

## §3 — Batch & parallelism (two different things — don't conflate)

**(a) In-app parallelism — ALREADY BUILT and correct.** N files dropped in Slack fan out via
`asyncio.gather(*[_run_one(f,i) …])` (`slack_runner.py:6146`), bounded by `_SEM` (default 5,
`LEDGR_MAX_CONCURRENCY`), each with its own per-doc session, then a **single deferred fan-in** ledger
write that is **double-locked** (in-process `threading.Lock` per (client,fy) + cross-instance
`FirestoreLeaseLock`). This is exactly ADK's `ParallelAgent` pattern (concurrent branches, explicit
fan-in merge, explicit locking to avoid races) — ADK even *warns* you must lock shared state, which
validates our client-scoped ledger lock. **Nothing to build here; document it so we stop re-asking.**

**(b) Gemini Batch API (the 50%-off mode the user saw) — WRONG tool for interactive Slack.** It's
**asynchronous, 24h target turnaround** (offline: bulk reprocessing, evals, embedding generation). A
user dropping a PDF expects seconds. Keep **Standard (synchronous)** for live Slack. Batch is only for
**offline backfills/eval runs** (build order step 8). *Middle option:* **Flex inference**
(`service_tier`) — same 50% discount, synchronous, 1–15 min target, sheddable — viable for
non-urgent background reprocessing, still too slow for live drops. **Caveat:** Vertex **2.5 Flash-Lite
does not support Flex or Batch** — verify tier per deployed model.

## §4 — Chart-of-Accounts intelligence (the CRITICAL, trust-defining axis)

**Current state (audit Q2) is already sound and on-principle** — deterministic-first (entity-memory
0.95 → category 0.9 → COA-keyword 0.8) → for still-unresolved lines, ONE LLM call with the **client's
full COA inlined as JSON** ("choose key from these only"); hallucinated keys nulled; conf<0.6 ⇒ flagged;
no-match ⇒ **blank + HITL, never a default code**. **This corrects Part I/D2:** the GL account does NOT
silently default to the creditor code — `resolve_account` returns blank+flag, and the **creditor code
is a separate export field** (`code_resolver.resolve_creditor_code`). The live "M PREMIUM AccNo=400-M0001"
is therefore a **column-mapping** question (a creditor-code field landing in an account-labelled column),
**not** a resolution default. → **Reclassify D2 from HIGH-resolution-bug to a MAP item; verify which
column the creditor code is populating in AutoCount and whether that's the ERP's intended AccNo semantics.**

**How to make COA fully trustworthy (Google's documented "pick the right code from a controlled list
given free-text"), in increasing power:**
1. **Constrained generation — the decisive correctness lever, not yet used.** Today we *post-validate*
   (`if key not in valid_keys: key=None`). Instead, **structurally guarantee** a real code: make each
   line's `account_code` an **enum-typed field constrained to the client's actual COA keys** (Vertex
   documents `responseMimeType:"text/x.enum"` / enum-typed schema properties — "the model selects an
   enum value from a list defined in the schema"). The model then *cannot* emit a non-existent code.
2. **Thinking budget — the "level of thinking" the user wants.** COA matching needs reasoning over
   similar descriptions. Set a **bounded** `thinkingConfig.thinkingBudget` (2.5 Flash: 0–24,576; set
   `0` for trivial extraction, a small budget for the COA-reasoning step only). This is where we *do*
   spend intelligence — deliberately and scoped, not on the whole pipeline.
3. **Confidence + HITL gate (§5).** Read the chosen code's token logprob; below threshold or on a
   near-tie between two plausible accounts ⇒ route to HITL instead of auto-booking. Wrong postings are
   the hardest thing for the user to catch later, so this axis gets the *strictest* gate.
4. **Vector/embedding retrieval — a FUTURE lever, only when COA outgrows in-context.** For JBI's ~158
   codes, the full COA fits trivially in the 1M-token window, so **in-context + enum-constrain is the
   right call now** (cheaper, simpler, no vector store — consistent with the prior
   [[multi-erp-autocount-sql-decisions]] "deterministic, not RAG" decision: the *final pick stays
   constrained to real codes*). When a client's COA grows past what's practical/cheap in-context, add
   the documented retrieval shortlist: embed COA once with `task_type=RETRIEVAL_DOCUMENT`
   (`gemini-embedding-001`, or `text-multilingual-embedding-002` for SG/MY), retrieve top-k with
   `RETRIEVAL_QUERY`, then enum-constrain to those k. Embeddings only *shortlist*; they never pick.

**Net:** COA stays deterministic-first; the LLM fallback gains (1) enum-constraint so it *can't*
invent a code, (2) a scoped thinking budget so it *reasons*, (3) a real confidence gate so low-certainty
postings *stop for a human*. That is the "the intelligence is really there, and the user can rely on it."

## §5 — Confidence: surface what Gemini actually exposes (today we capture NONE)

**Audit Q5: we request zero Gemini confidence signals** — no `responseLogprobs`, no `thinkingBudget`;
the only "confidence" is hand-assigned heuristic floats (0.95/0.9/0.8) + the model's *self-reported*
JSON `confidence` (uncalibrated). The "confidence level" the user remembers from the docs is real and
usable:
- **`avgLogprobs`** (per candidate) — length-normalized confidence scalar; "higher suggests a more
  confident response." One number per document/field to threshold.
- **`logprobsResult.chosenCandidates[].logProbability`** — per-token; "a low log probability can
  indicate the model is hallucinating." Read the chosen **account-code** / **total** / **tax-code**
  token's logprob to gate exactly the fields that hurt most if wrong. Request via
  `responseLogprobs:true` + `logprobs:N` (1–20).
- **Caveat (doc-grounded):** grounding `GroundingMetadata.confidenceScores` are **empty on Gemini 2.5**
  ("ignore for 2.5+") and apply only to Search-grounding, not PDF extraction — so our usable signal is
  **`avgLogprobs`/`logprobsResult`, NOT grounding scores.**
- **ADK HITL wiring:** ADK Action confirmations + graph human-input nodes are the native escalation
  primitives — compute the logprob gate in a callback/tool and route low-confidence docs to a
  human-input node (aligns with our existing ADR-0017 four-lever HITL).

**Recommendation:** add an optional confidence capture (`responseLogprobs`) on the extract + COA calls,
map `avgLogprobs`/chosen-token logprob → the existing `flagged`/HITL machinery, and **replace** the
self-reported JSON confidence as the gate. Cost: logprobs are free to request (no extra tokens).

## §6 — Cost levers (cheapest-correct, doc-grounded numbers)

Per 1M tokens (paid): **Flash** $0.30 in / $2.50 out; **Flash-Lite** $0.10 / $0.40. PDF pages bill as
image input (258 tok/page).
- **Context caching = the biggest lever for our repeated large context** (the client's COA + the static
  extraction instructions sent every doc). Implicit caching is **on by default for 2.5 → 90% discount
  on cached tokens** (min 2,048 tokens; the 90% cache discount does *not* stack with and **takes
  precedence over** the 50% batch discount). Tune via **ADK `ContextCacheConfig`** on the App:
  `min_tokens` (≥2048), `ttl_seconds` (default 1800), `cache_intervals` (default 10) + `static_instruction`
  for the reused prompt prefix. **Caveat:** don't mutate a cached COA/GCS object until the cache expires.
- **`thinkingBudget=0`** on Flash for plain extraction (thinking bills as output); spend a *bounded*
  budget only on the COA-reasoning step (§4).
- **Batch API 50%** — offline only (§3); **Flex 50%** — background reprocessing only.
- **`mediaResolution=LOW` is NOT a real lever for PDFs** — docs state "no cost reduction for pages at
  lower sizes, other than bandwidth." It only helps standalone images/video. Drop it from the PDF plan.

---

## Architecture / cleanup verdict (the user's "codebase is messy" concern)

The intelligence the user wants is **largely already present and correctly shaped** — COA resolution,
deterministic column mapping, parallel fan-out, fail-loud-on-unknown account/jurisdiction. The mess is
**(a) the extraction schema cage** (§1, the one true data-loss bug), **(b) duplicated/divergent
surfaces** that drift (3 extract stacks AR1, 3 label fns AR3, hand-copied preview cols AR4/§2,
guess-chain column lookups MAP3), and **(c) brittle hardcoded lexicons/defaults** (anti-patterns 2–3)
that fight the model instead of trusting the printed document. The cleanup is therefore **consolidation,
not rewrite**: one faithful extraction schema, one profile-derived column source feeding both Excel and
Slack, one resolver per axis, the printed value authoritative, confidence-gated HITL on the fields that
matter. None of it requires abandoning the deterministic spine — it requires *removing the second
copies and the keyword overrides* so the ADK/Gemini intelligence we already invoke is actually trusted.

---
---

# PART III — Verification verdict & MANDATORY hardening gates

Two independent adversarial reviewers (architecture critic + test/eval engineer) attacked Part II.
**Verdict: REVISE — directionally correct, but Part II trades a LOUD known bug (loses N-1 invoices) for
a class of SILENT bugs (mis-segmentation, confident-wrong COA, dropped flags) with no deterministic
gate, and overstates two Gemini guarantees.** The app's defining constraint — Slack-only delivery, **no
Sentry-style view of wrong-but-plausible output** — means every silent-corruption path is undetectable
in production. The items below are therefore **acceptance criteria, not nice-to-haves**: no part of the
implementation plan ships without its gate.

## §1 — Single call: per-FILE, not per-batch (explicit, to kill the recurring confusion)

- **One *file* → one call.** A single PDF (even 30 pages / 10 invoices, or one page with 3 receipts)
  goes to Gemini in ONE request; the array schema returns all its documents. "Up to 1000 pages" is the
  **headroom of that one call** — it is NOT about cramming multiple uploaded files together.
- **N *separate* files → N parallel calls** (the existing `asyncio.gather` + `_SEM` cap-5 fan-out).
  **Do NOT merge separate files into one call** — keep per-file isolation for: (a) error containment (one
  corrupt file fails alone), (b) per-file confidence/retry, (c) the 50 MB / 1000-page per-call limit,
  (d) lower wall-clock via parallelism. Cost is identical (billed per page/token, not per call).
- The two mechanisms **compose**: per-file single-call fixes intra-file multi-invoice loss; cross-file
  fan-out handles multi-file drops. State this in code comments so no future change "optimizes" them together.

## §2 — CRITICAL gates the array schema MUST pass (else it ships silent corruption)

| Gate | Requirement | Why (silent failure it catches) |
|------|-------------|---------------------------------|
| **G1 per-doc reconcile** | For EACH element of `documents[]`: assert `abs(Σ line.net + tax_total − grand_total) ≤ tol`. On fail → `reconciled=False` + flag to Slack. (`reconcile()` exists but is wired only to the single-doc path — wire it per array element.) | A boundary-merge can keep a grand total right while mis-attributing lines/vendor/tax to the wrong invoice. Loud today (total short), SILENT under the array schema without this. |
| **G2 page-coverage** | Union of all `page_range` == input page set; **no gaps, no overlaps**. On violation → flag "segmentation uncertain". | Detects merged/split/dropped documents — the model is documented as "not precise at locating objects," so segmentation MUST be cross-checked deterministically, not trusted. |
| **G3 doc-count surface** | Show "extracted N documents from M pages" on the delivery card. | Makes the segmentation decision human-visible — the only catch for confident mis-segmentation. |
| **G4 rounding tolerance** | Define an explicit per-currency cent tolerance for all reconciles; avoid float drift (consider integer cents). | "Reconcile to total" with no tolerance either false-flags or false-passes. |
| **G5 partial-failure semantics** | Define what ships when the array call returns 3 of 4 docs, or one doc fails G1: deliver the good ones, **flag the gap loudly**, never silently drop. | Undefined today; silent drop = the original bug class returning. |

## §3 — Two Gemini guarantees Part II OVERSTATED (correct these, or an executor removes the safety net)

- **Enum-constraint is NOT a structural guarantee for per-line enum fields inside a nested array.** The
  documented `text/x.enum` sample is a flat top-level classification; nested-array enum is "schema
  guidance," best-effort. **Therefore: KEEP the existing post-validation** (`categorizer.py:265-266`,
  `if key not in valid_keys: key=None`) as belt-and-suspenders EVEN WITH enum-constraint, and treat
  "enum-in-nested-array at ~158 keys on Vertex 2.5 Flash-Lite" as **UNVERIFIED → requires a spike test
  before relying on it.** (Also: a 158-key enum schema counts toward input tokens, and a per-client
  dynamic enum conflicts with the stable-prefix needed for context caching in §6 — measure the interaction.)
- **Logprob confidence is RECALL-ONLY, not a correctness oracle.** A model can be *confidently wrong*
  (high logprob on "Repairs" when the right account is "Motor Vehicle Expenses"). Logprob/avgLogprobs
  catches UNCERTAIN errors; it does nothing for confident-wrong. **The primary COA gate is the
  deterministic arithmetic reconcile (G1) + surfacing the account decision to a human**, not the logprob.
  Frame §4/§5 accordingly.

## §4 — Flag propagation & confidence-to-Slack (the error channel IS the delivery card)

- **M2 — resolution flags are dropped at the boundary.** `categorize_invoice` computes `flagged` per
  `AccountResolution` but the write-back (`categorizer.py:355-356`) only sets `account_code`; there is
  **no `account_flagged` on `InvoiceLine`** (only `tax_flagged`). A conf<0.6 COA pick is indistinguishable
  downstream from a confident one. **Add `account_flagged`, carry it to the row + delivery card +
  import-readiness note.** (Verify no current caller reads `res.flagged` — it appears dropped.)
- **M4 — the common path is blind.** The batch-aggregate delivery path (multi-file drop = what users
  actually do) **omits import-readiness + confident notes** (AR2), and `compose_confident_note` reads
  literal keys `"Net Amount"`/`"Currency"` that **no exporter emits** (`nodes.py:2250,2256`) → the
  "reconciles to $X" total is **always blank**. Combined, the multi-file dropper sees neither a reconcile
  total nor a readiness flag. **Reprioritize MAP2 (fix via `column_for_field("sub_total"/"currency")`) +
  AR2 (render notes on the batch path) into the CORRECTNESS tier (step 2), not cleanup (step 6).**
- **Add a per-document "reconciles ✓/✗" cell + a flag-reason breakdown** (blank-account / tax-unresolved /
  jurisdiction-unresolved) to the batch delivery card. Counts are already computed; the breakdown is discarded.

## §5 — Runtime silent-failure self-detection (fail LOUD to Slack, ranked by danger)

These are runtime assertions feeding `detect_struggle`/HITL, distinct from tests. Several are absent today:

1. **Blank `account_code` on a delivered line** → flag `"blank_account_code"`. (Today passes through; AutoCount may accept blank AccNo as a silent default GL.) **CRIT.**
2. **`account_code` not in the client COA** → force blank + flag `"account_code_not_in_coa"`. (The LLM path nulls hallucinated keys, but a wrong-client entity-memory entry would bypass that.) **HIGH.**
3. **`jurisdiction_unresolved`** before `get_tax_classifier(None)` silently SG-defaults (D1) → flag + HITL. **CRIT.**
4. **Per-row required-field check** in `ProfileLedgerExporter.rows()` — every YAML `required_field` non-blank, else flag (MAP1–MAP3 land here). **HIGH.**
5. **Multi-doc reconcile (G1)** wired per array element. **HIGH.**
6. **`currency` == default "SGD" but jurisdiction MY** → flag `"currency_mismatch"` (D6). **MED.**

## §6 — COA EVAL (the highest-leverage missing test; wrong postings are invisible to the user)

There is **zero end-to-end COA test today** — unit tests cover `resolve_account` mechanics, but nothing
runs the full PDF → extract → categorize → ERP row path and asserts `account_code == expected`. Build
`tests/integration/test_coa_eval_jbi.py`, gated on the JBI data existing locally (same pattern as
`test_erp_golden_format.py`). Ground truth: JBI `COA & List.xlsx` (Party List + COA) +
`LocalRecon_VertexPrompt_LedgerRows.json` + ~30 min of manual line→code annotation (the single
highest-ROI eval investment in the repo).

**Golden scenarios (input → expected):** entity-memory exact vendor; entity-memory by reg-no (vendor
printed differently); category-mapping hit; COA-keyword hit; **ambiguous (two competing accounts) → must
flag, code ∈ COA**; brand-new vendor clear description → correct account; **no account should match
(e.g. "salary") → blank + flag**; multi-line invoice different account per line; qty>1 line → Amount=net
not unit_price (MAP1); MY vs SG → correct COA, no cross-contamination; credit-note → sign-flip preserves code.

**Metrics + the hard rule:**
- top-1 account accuracy ≥ 0.85 to gate release (deterministic paths alone ≥ 0.95 for known vendors).
- **flag-recall = 1.0 (HARD)** — every "must-flag" scenario flagged; a miss is a test failure, not a metric.
- flag-precision ≥ 0.80 (don't drown the accountant).
- **ZERO-TOLERANCE GATE:** no exported row may carry an `account_code` not in the client's COA (or blank).
  Asserted end-to-end through the exporter, not just at the categorizer.

**Extraction eval (array schema):** `jbi_golden.json` with `expected_doc_count` + per-doc
`grand_total`/`must_reconcile` + the page-coverage assertion. Confirmed counts: **M PREMIUM=2** (the 2→1
regression guard), **ATOM=11**, **AUTO LAB=6** (SOA, skip cover page), GDEX=1 faithful. **ADD a
segmentation-stress doc (≥3 invoices on one page) and a non-English (Malay/Chinese) doc** — both absent
and both are exactly where the array schema + the (deleted) keyword lexicons fail.

## §7 — Observability without Sentry (Sentry MCP IS connected — use it for trends only)

Slack delivery card is the primary error channel (§4). **Additionally**, after the in-pipeline flags
exist, log structured events to the connected Sentry for **cross-document trend detection** only —
`{client_id, vendor, reconciled:false, reason, confidence}` on `reconciled=False` / `blank_account_code`.
Value: "30% of client JBI's docs failed reconcile this week" (prompt drift / format change) is invisible
from individual Slack messages. **Do this AFTER §5 — there's no value logging silently-wrong output the
pipeline itself can't detect.**

## §8 — Other corrections from review

- **MAP1 is a ~4-line YAML fix, not a new context key:** `_row_context` already emits `"sub_total": net`
  (`exporters.py:405`) distinct from `unit_price`. Defect is purely `Amount: unit_price`/`_AMOUNT: unit_price`
  in `autocount.yaml:95`/`sql_account.yaml:78` → change to `sub_total`. **CAVEAT: confirm AutoCount `Amount`
  semantics first** — if it's the tax-INCLUSIVE line total, map to `total_amount`, not `sub_total`.
- **M3 dedup under the array schema:** one file now yields N ledger documents — re-dropping the same PDF
  must not double-post. **Define the per-array-element dedup identity** (file_id + page_range + reference)
  and prove idempotency. Note AutoCount already loses invoice identity (`DocNo="<<New>>"`, MAP5).
- **D1 (SG-default on unresolved jurisdiction) is CRIT silent corruption — move it ahead of/with the COA
  work**, not buried in Anti-pattern 3 / build step 4.
- **"Deterministic post-step for bookkeeper-grouping" (M1) must default to NO grouping — import lines
  verbatim.** Grouping only when a specific ERP profile *declares* it needs it, with the rule in YAML, not
  code. Otherwise the telco/expense keyword logic just moves from prompt to Python and stays brittle.
- **Invariant to state for executors:** `tax_system_hint` / `direction_reason` / segmentation fields are
  model evidence — **never `if hint == "SST"`-branch on them in Python.** Guard against re-introducing it.

## Revised build order (gates inlined)

1. **Array schema (A1+A2) + G1–G5 gates + D1 jurisdiction-flag** — faithful multi-doc, but ONLY behind
   per-doc reconcile + page-coverage + partial-failure semantics. Spike enum-in-nested-array first.
2. **Correctness + visibility tier:** MAP1 (YAML, verify semantics) · MAP2/AR2 (batch-path notes + real
   reconcile total) · §5 runtime flags (blank/not-in-COA account, required-field) · `account_flagged`
   propagation · per-doc reconcile ✓/✗ on the card.
3. **COA eval (§6)** stood up as the gate for everything COA-related.
4. Kill keyword lexicons / overrides (Anti-pattern 2), verbatim-by-default grouping (M1).
5. One resolver per axis, blank+flag not substitute (Anti-pattern 3); tax-code master authoritative (T1/T2).
6. Consolidate: one extraction path, profile-derived preview columns (=Excel/Slack parity), one label fn.
7. Scrub real client data from prompts (P1/P2/P3); model-driven grouping signals.
8. Cost pass: context caching (measure vs dynamic enum), thinkingBudget=0 for extract, Batch/Flex offline only.
9. Sentry trend logging (§7), then optional `responseLogprobs` confidence (§5) once reconcile gates prove out.

## §9 — SPIKE RESULTS (decision-grade, run 2026-06-21)

Two risky assumptions from Part III were tested live before planning. Both resolved.

### Spike A — enum-in-nested-array: **STRUCTURAL (confirmed), with one critical caveat**
- **Setup:** `gemini-2.5-flash` (AI Studio), 158 REAL JBI COA keys as a per-line `account_code` enum
  inside `Doc{lines: list[Line{description, account_code: ENUM[158]}]}`; 18 runs × 6 deliberately
  out-of-scope line descriptions (max pressure to hallucinate); temperature 0.
- **Result: 0 / 108 out-of-set emissions.** The enum is enforced at the constrained-decoding layer —
  the model **cannot** emit a code outside the set even under adversarial pressure. Both raw-dict
  (`{"type":"STRING","enum":[...]}`) and Pydantic-Enum schemas were accepted. **Token cost of the
  158-key enum = +0 prompt tokens** (schema is out-of-band, not in the prompt window). Latency +~2–4s
  for constrained decoding (~4s→~6–9s) — acceptable.
- **UPGRADE to §3:** enum-constraint IS a structural validity guarantee → post-validation downgrades
  from a *correctness gate* to a *semantic-plausibility check*. (Caveat: tested on AI Studio Flash,
  **re-confirm on Vertex 2.5 Flash-Lite (prod) during build** — the mechanism is API-config-level so
  expected to hold, but verify.)
- **NEW CRITICAL REQUIREMENT (the spike's most important finding):** under temp 0 the model **picks the
  "nearest plausible" in-set code rather than ABSTAINING** — it never left a code blank even for
  "Unicorn-based transport reimbursement." So a **hard non-nullable enum BREAKS the "no account should
  match → blank+flag" rule** (the salary-line case): it would force a wrong-but-in-set code. **Therefore
  the `account_code` field MUST be nullable OR include an explicit `"UNMAPPED"` sentinel enum value**, so
  the model can signal "none fit" → blank+flag+HITL. This also **empirically confirms C3**: enum
  guarantees *validity*, not *correctness* — confident-wrong-but-in-set codes are invisible to any
  structural check, so the semantic/description-match + reconcile + HITL layer stays mandatory.

### Spike B — AutoCount/SQL "Amount" semantics: **tax-EXCLUSIVE line net → map to `sub_total`**
- **Evidence (real `Import-AP-Invoice.xls` template):** detail columns `… TaxType | TaxableAmt |
  TaxAdjustment | Amount`, master flag `InclusiveTax` = **F** in every sample. With `InclusiveTax=F`,
  **`Amount` is the tax-EXCLUSIVE extended line amount** (the value posted to `AccNo`); `TaxableAmt` is
  the separate taxable base (sample shows Amount=100 with TaxableAmt=50 — they are independent columns).
- **Resolves the MAP1 caveat:** map AutoCount **`Amount → sub_total`** (line net), NOT `total_amount`
  and NOT `unit_price`. Keep the exporter writing `InclusiveTax=F`. `TaxableAmt → sub_total` too for the
  common fully-taxable line.
- **SQL bonus:** the SQL purchase template has DEDICATED `_UNITPRICE`, `_QTY`, `_TAX`, `_TAXAMT`,
  `_TAXINCLUSIVE`, `_AMOUNT` columns. Fix `_AMOUNT → sub_total` AND map `_UNITPRICE → unit_price`,
  `_QTY → qty` so unit price/qty are preserved in their own columns (currently lost). SQL also exposes a
  rich e-invoice header block (IRBM UUID/MSIC/classification) — out of scope now, note for future MyInvois.
- **Net:** MAP1 is confirmed a small YAML change (`Amount`/`_AMOUNT`: `unit_price → sub_total`), no
  semantics ambiguity remains.

## §10 — COA DECISION: "do we build LLM thinking?" → NO (doc-grounded), spend intelligence elsewhere

Focused research (ai.google.dev/adk.dev via MCP) on the single most correctness-critical step. The
answer corrects §4's earlier "bounded thinkingBudget on the COA step" suggestion.

**Thinking is OFF on the default path.** Gemini's thinking guide explicitly files **classification under
"Easy tasks — thinking could be OFF"** (example: *"Is this email asking for a meeting or just providing
information?"* — isomorphic to "which COA bucket?"). Thinking budgets are for multi-step math/coding/
planning. Flash-Lite (the invoice/"lite" model) **does not think by default** — keep it that way.
Spending thinking tokens on the per-line pick buys latency/cost with **no documented accuracy gain**.

**The intelligence that makes COA reliable is NOT reasoning tokens — it's four doc-backed levers:**
1. **Deterministic spine, LLM as matcher only** (already implemented): COA = authoritative client master,
   loaded deterministically; model only *matches*, structurally constrained so it can't invent a code.
2. **ONE in-context structured call — not RAG, not an agent loop — at 150–500 codes.** Google's guidance
   (small dataset → in-context KNN; large → ANN/vector search) makes retrieval a **scale optimization not
   yet reached**. Embeddings (`gemini-embedding-001`, RETRIEVAL_DOCUMENT/QUERY, hybrid for part-numbers
   like "195/65R15") become worth it at **thousands** of codes — defer; if added early, use only as a
   *recall floor + second similarity signal*, never the decider. Agentic reflection (ADK Generate-and-
   review / LoopAgent) is over-engineered for a single-label pick — reserve for failed-gate lines only.
3. **`UNMAPPED` sentinel in the enum (resolves the Spike-A abstention gap).** Schema = object
   `{account_code: enum(<real client codes> + "UNMAPPED"), confidence: number, reasoning: string,
   alternative_codes: string[]}` (nullable type also allowed), with a `description` + prompt instruction
   that **abstaining is correct when nothing fits**. Use an object schema (not bare `text/x.enum`) so the
   reasoning + runner-ups are captured in one call for audit. Keep the enum **flat** (docs warn very
   large/deeply-nested schemas can be rejected; 500 flat enum strings is safe).
4. **Logprobs as the calibrated gate — NOT the self-reported `confidence`.** Docs are explicit: structured
   output guarantees *syntax, not semantics — "always validate."* So a model-emitted `confidence:0.9` is
   advisory only. Request `responseLogprobs:true` + `logprobs:5` (range 0–20); gate acceptance on
   **`avgLogprobs`** + the **top-1→top-2 margin** in `logprobsResult.topCandidates` (narrow margin = torn
   between two accounts = escalate). This is the *only* calibrated confidence signal Google points to.

**Where a small thinking budget DOES earn its place:** ONLY on the **abstain boundary** — the borderline
lines the confidence gate flags — where "does any code genuinely fit?" crosses into the docs' "Medium /
some step-by-step" tier. Even there its higher value is **auditability** (`includeThoughts:true` → a
thought summary explaining *why* it abstained, for the human reviewer), not accuracy. Never a full/high
budget or reflection loop on the default path.

**Human verification (ADK primitive):** ADK **Tool Confirmation** — wrap COA assignment as a
`FunctionTool(require_confirmation=threshold_fn)` where `threshold_fn` mirrors the docs' `amount>1000`
example but reads the logprob gate (`low avgLogprobs OR narrow margin OR code=="UNMAPPED"` → confirm);
advanced confirmation lets the reviewer pick from `alternative_codes[]` via `payload`. **★ BUILD-TIME
CAVEAT:** Tool Confirmation docs state it does **NOT support `DatabaseSessionService`/
`VertexAiSessionService`** — **we use Firestore sessions** → verify compatibility, else fall back to a
**`RequestInput` human-input graph node** (non-AI, pauses the graph, carries top-k candidates as payload).

**Verdict on the user's question:** the reliable intelligence lives in **structured enum + UNMAPPED
abstention + logprob calibration + ADK human gate** — *not* in turning on thinking. This is also why the
current `categorizer.py` LLM fallback is close to right: it just needs (a) the `UNMAPPED` sentinel so it
can abstain, (b) enum-constraint on the real keys (spike-confirmed structural), (c) the logprob gate
replacing the self-reported-confidence threshold, (d) the dropped flag carried to delivery (M2). No
thinking, no RAG, no agent loop at this scale.
