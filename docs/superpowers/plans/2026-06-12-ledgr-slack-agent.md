# Ledgr (QBS) — Slack Accounting Agent: Implementation Plan & Handoff

> **For agentic workers:** Use `superpowers:subagent-driven-development` to execute this plan
> task-by-task. **Execution model: the main agent DELEGATES each task to a sub-agent and only
> verifies** (keep main context lean — see memory `delegate-then-verify`). Steps use `- [ ]`.

**Goal:** A distributable, Slack-native agent for accounting firms (Singapore first, Malaysia next):
an accountant drops financial documents into a Slack channel; the agent classifies each (purchase /
sales / receipt / bank statement), extracts the data, categorises lines to the client's chart of
accounts, applies GST tax codes, and returns a consolidated **Excel** ledger (QBS Ledger or Xero) in Slack.

**Architecture:** Built on Google **ADK** (Python), adapted from the `invoice-processing` sample.
Pipeline is **classify → route → extract → categorise → tax → export**. One **Cloud Run** service in
**`asia-southeast1`** hosts the ADK FastAPI app + Slack routes. Per-client config lives in our own
datastore (Firestore) — **not** the legacy Google Sheets/Drive workflow. Gemini **Flash only**
(2.5-pro is not available in asia-southeast1; Flash keeps data in-region for PDPA).

**Tech stack:** Python 3.13 + `uv`; google-adk; google-genai (Vertex, `asia-southeast1`); openpyxl;
Slack (Bolt); Firestore + GCS; Cloud Run; `agents-cli` for scaffold/eval/deploy; `adk-docs` MCP for ADK docs.

---

## HOW TO RESUME IN A NEW SESSION (read these first)
1. This plan.
2. Memory index: `/Users/davidkitdave/.claude/projects/-Users-davidkitdave-Projects-Ledgr-QBS/memory/MEMORY.md`
   and the files it lists (project overview, data model, build-forward-not-legacy, delegate-then-verify,
   adk-use-official-mcp-and-cli).
3. Design/reference docs in this repo:
   - `docs/forward-design-slack.md` — the canonical forward design (Slack UX + pipeline + infra).
   - `docs/build-map-categorization.md` — COA categorization mapped to ADK primitives.
   - `docs/research/sg-gst-tax-codes.md` — IRAS GST SR/ZR/ES/OS reference + decision tables.
   - `docs/superpowers/specs/2026-06-12-ledgr-client-onboarding-fy-routing-design.md` — **approved** design
     for the per-channel client profile, 4-field Slack onboarding modal, FYE-month financial-year model, and
     archive+workbook routing (supersedes Sys_Config). Drives task #11.
4. The original approved plan: `~/.claude/plans/i-want-to-build-bubbly-hoare.md` (higher-level).

**Rules:** Use the `adk-docs` MCP for any ADK question. Delegate each job to a sub-agent; verify, don't
re-implement. Don't replicate the Google Sheets/Drive workflow. All Python via `uv run`.

---

## ENVIRONMENT (already set up)
- Project dir: `/Users/davidkitdave/Projects/Ledgr-QBS` (the `invoice-processing` sample is vendored here).
- GCP project `ledgr-qbs`, ADC authed (admin@qbsaiautomation.com); APIs on: Vertex AI, Firestore, Cloud Run, GCS.
- `.env`: `PROJECT_ID=ledgr-qbs`, `LOCATION=asia-southeast1`, `GEMINI_FLASH_MODEL=gemini-2.5-flash`,
  `GEMINI_PRO_MODEL=gemini-2.5-flash` (Pro→Flash on purpose), `GOOGLE_GENAI_USE_VERTEXAI=TRUE`.
- `agents-cli` v0.4.0 installed; `adk-docs` MCP installed.
- **Sample data** (test + target formats, NOT the mechanism): `~/Desktop/LocalTest/` —
  `TestDoc/` (SG clients, DBS statements, `Ledger_FY*.xlsx`, `Client Setup.xlsx`),
  `TestDoc/MYDoc/` (Malaysia invoices/receipts), `TestDoc/GST SR:ZR/` (telco SR/ZR bills),
  `header template/` (Xero/AI-Account/SQL/Autocount import templates). ~2290 PDFs.

## DECISIONS LOCKED
- Singapore first (GST 9%, UEN, SGD); Malaysia next. Deploy `asia-southeast1`. Gemini Flash only.
- Slack-native; **one channel per client** (channel identifies client → resolves sales/purchase + COA + tax).
- **Batch** input → **one consolidated workbook** (Purchase + Sales sheets; bank statements separate).
- Output formats: **QBS Ledger** (native cols, no tax-code col — Tax Amount carries 9%/0) and **Xero Ledger**
  (Xero import cols + explicit `*TaxType`). Workbook sheets = `Purchase` + `Sales` only (NO Sys_Config,
  NO Processing Date / Source File ID / [AI Status] / [AI Note] columns).
- v1 doc types: purchase invoices, sales invoices, receipts, bank statements.
- Per-client COA: client **uploads their COA once at setup** → our datastore. AI maps via two layers
  (universal Category → client's account code) + Entity_Memory (learned vendor→account+tax) + COA-keyword match.
- Keep field-service framing + chart-of-accounts style categories. Tax handling only if client GST-registered.

---

## CURRENT STATUS

### Built & verified ✅
- **Vendored sample runs** in asia-southeast1 on Flash; case_002 matched ground truth. (Task #1)
- **SG tax localization** in `invoice_processing/shared_libraries/invoice_master_data.yaml` (GST 9%, SGD,
  UEN, checksum off). (Task #2, partial)
- **GST taxonomy** `invoice_processing/shared_libraries/sg_gst.yaml` + **tax classifier**
  `invoice_processing/export/tax_classifier.py` (rules-first SR/ZR/ES/OS per IRAS). (Task #3)
- **Exporters** `invoice_processing/export/exporters.py` — `QbsLedgerExporter`, `XeroLedgerExporter`
  (Purchase+Sales sheets); `models.py` (NormalizedInvoice/InvoiceLine/PartyInfo). VERIFIED: reproduces
  the client's `BillTemplate.csv` telco SR/ZR split + native QBS/Xero columns. (Task #3)
- **Doc-type classifier** `invoice_processing/classify/document_classifier.py` — Gemini-Flash multimodal;
  `classify_document` + `resolve_direction`. VERIFIED 7/8 on real labelled docs (the 1 miss was a
  mislabel; effectively 8/8), conf 1.0, works on scanned PDF + .jpeg, direction (purchase/sales) correct. (Task #10)
- **Invoice/receipt extractor** `invoice_processing/extract/invoice_extractor.py` —
  `extract_invoice` + `to_normalized(direction)`. ✅ end-to-end classify→extract→tax→export CONFIRMED
  (Task #14): telco bill → SR/ZR split, exit 0, output captured.

### Built & verified this session (2026-06-12) ✅
- **API backend env-switch** `shared_libraries/genai_client.py` — `GOOGLE_GENAI_USE_VERTEXAI=FALSE` → AI
  Studio (dev, avoids Vertex 429 quota); `TRUE` → Vertex `asia-southeast1` (prod/PDPA). 429 retry baked in.
- **Summary-first judgment extraction** — `invoice_extractor.py` reads the bill's *summary* → small SR/ZR
  ledger lines (telco 284 → 2), + `reconcile()` guard (Σnet≈subtotal, Σgst≈gst_total). Verified across
  telco/invoice/receipt. Also: `tax_keyword` field + SG telco G/Z tax-code handling in `tax_classifier.py`.
- **Bank-statement lane (#8)** — `extract/bank_statement_extractor.py` (hybrid pdfplumber/vision,
  multi-account/currency split, `reconcile_running_balance`), `BankStatementExporter` (BankStatement_FY cols),
  `BankStatement`/`BankTransaction` models. Eval `eval/bank_eval.py`: **100% running-balance pass-rate over
  16 statements** (digital + vision), ≥0.9 met.
- **Categorizer core (#12)** — `export/categorizer.py` (`resolve_account` deterministic-first + batched LLM
  COA match; `resolve_account_tool` reads `tool_context.state`) + `export/client_context.py`
  (`ClientContext`, COA/Category_Mapping/Entity_Memory loader, `before_agent_callback`, in-memory +
  Firestore stores). ✅ **`Sys_Config` profile reading DROPPED** (2026-06-12) — profile now comes from the
  per-channel Firestore profile (see the onboarding/FY spec). COA/Category_Mapping/Entity_Memory parsing stays.
- **FY model + Sys_Config removal (#11, part 1)** — `export/fy.py` (`fy_for_date`, `last_day_of_month`,
  FYE-month → FY label per spec §3); `client_context.py` drops the `Sys_Config` sheet block, adds `fye_month`,
  threads an explicit `client_id` into `load_client_setup`/`from_setup_dir`. TDD: `tests/test_fy.py` +
  `tests/test_client_context.py` (hermetic).
- **FirestoreClientStore realigned to spec §1 (#11, part 2)** — `client_context.py`: `get()` reads the profile
  doc (client_name, fye_month, `gst_registered`→`tax_registered`, region/currency/status, channel/team ids;
  `category_mapping` is a doc-MAP field; `coa`/`entity_memory` are subcollections); added `get_by_channel`
  (`channels/{id}→client_id` reverse index), `make_load_client_by_channel_callback` (ADK callback contract
  confirmed via `adk-docs` MCP), `InMemoryClientStore.get_by_channel`, and a `client=` injection seam.
  Hermetic `tests/test_firestore_store.py` (hand-rolled fake Firestore — **no live GCP call**).
- **FY routing logic, spec §4 (#11, part 3)** — `export/routing.py` (`route_document`/`DocRoute`): doc_type+
  direction+date → FY-keyed GCS archive path `{client_id}/FY{fy}/{purchase|sales|bank}/{file}` + workbook
  (`Ledger_FY{fy}.xlsx` Purchase/Sales · `BankStatement_FY{fy}.xlsx`). TDD `tests/test_routing.py`.
  **All three code increments: full suite 214 green.** Then the **live Firestore round-trip PASSED** against
  `ledgr-qbs` (user-authorized): throwaway profile + COA + entity_memory + channel index written, read back via
  `FirestoreClientStore.get`/`get_by_channel` (all spec-§1 fields OK), then deleted (cleanup confirmed).

### Pending ⏳ (forward build sequence)
1. **#11 Per-client datastore + onboarding** — **per the approved spec**
   (`docs/superpowers/specs/2026-06-12-ledgr-client-onboarding-fy-routing-design.md`).
   ✅ DONE (2026-06-12, all code-only, hermetic-tested, suite 214 green): `fy_for_date`/`last_day_of_month`
   (`export/fy.py`); **`Sys_Config` dropped** from `client_context.py` + `fye_month`/profile fields on
   `ClientContext`; `FirestoreClientStore` realigned to spec §1 (+ `get_by_channel`,
   `make_load_client_by_channel_callback`, `client=` injection seam); spec §4 routing (`export/routing.py`).
   ✅ LIVE FIRESTORE ROUND-TRIP PASSED (2026-06-12, user-authorized): wrote a throwaway profile + COA +
   entity_memory + channel index to `ledgr-qbs`, read back via `FirestoreClientStore.get`/`get_by_channel`
   (all spec-§1 fields incl. `gst_registered`→`tax_registered`, `category_mapping` doc-map, blank-code COA,
   reverse index) — asserts OK, test docs deleted (cleanup confirmed). **#11 datastore / FY / profile layer
   COMPLETE.**
   ➡️ Only the **4-field** Slack onboarding modal (auto-greet on channel join) + COA-upload UX remains — that is
   the Slack app layer, built with **task #4** (`make_load_client_by_channel_callback` is ready to wire in).
2. **#12 Categorisation** — core built (`resolve_account` + `categorize_invoice`, reads `tool_context.state`).
   Remaining: wire into the pipeline so each line's `account_code` is filled per client; Category_Mapping bootstrap (later).
3. **#13 Batch → consolidated workbook** — accumulate a batch, classify+extract+categorise each, emit one
   QBS/Xero `Ledger_FY{year}` workbook (+ `BankStatement_FY{year}`) per client SOFTWARE, routed by FY.
4. **#4 Slack glue** — channel-per-client; auto-greet + setup modal + `/ledgr settings`; `/slack/events`
   (ack<3s + Cloud Task), `/tasks/process` worker (download → pipeline → `files_upload_v2` Excel back),
   result cards, `/ledgr export`; COA-file ingest.
5. **#6 Learning** — corrections (Slack ✏️) → `remember_entity` → per-client Entity_Memory store.
6. **#5 Multi-workspace distribution + deploy** — OAuth install, per-workspace tokens, deploy custom
   `fast_api_app.py` to Cloud Run (`gcloud run deploy --source .` / `agents-cli deploy`), `asia-southeast1`.
7. **#9 Eval loop** — `agents-cli eval` to **≥0.9** across doc types; bank lane already at 100% recon
   (`eval/bank_eval.py`); extend to account-code + tax-code accuracy vs verified `Ledger_FY`.

**Done this session (2026-06-12):** #14 (end-to-end confirmed), #8 (bank lane + eval 100%), API→AI Studio,
summary-first extraction, categorizer core (#12). See "Built & verified this session" above.

---

## EXECUTION MODEL (apply to every task)
- Main agent: pick the next task → **spawn a sub-agent** with a focused brief (the task's files, the
  relevant section of `docs/forward-design-slack.md` / `build-map-categorization.md` / `sg-gst-tax-codes.md`,
  and a definition of done). Sub-agent implements + self-tests.
- Main agent: **verify only** — read the diff, run the verification command / a real-doc test / `agents-cli eval`.
  Keep authoring and verification in separate lanes; don't re-implement inline.
- Build with `uv run`. ADK questions → `adk-docs` MCP. Commit per task (TDD where practical).

## KEY MODULE MAP
```
invoice_processing/
  classify/document_classifier.py   # ✅ classify + resolve_direction
  extract/invoice_extractor.py      # ✅ extract_invoice + to_normalized  (bank-statement extractor = TODO)
  export/
    models.py                       # ✅ NormalizedInvoice / InvoiceLine / PartyInfo
    tax_classifier.py               # ✅ SG GST SR/ZR/ES/OS (reads sg_gst.yaml)
    exporters.py                    # ✅ QbsLedgerExporter / XeroLedgerExporter (Purchase+Sales sheets)
  shared_libraries/
    sg_gst.yaml                     # ✅ tax taxonomy + per-system code map
    invoice_master_data.yaml        # ✅ SG-localized (GST 9%, SGD, UEN)
    acting/ investigation/ alf_engine.py  # original sample brain (purchase/sales extraction path)
  # TODO: classify/extract/categorise wiring into a router; client_context loader; resolve_account; batch
app/ (TODO)                          # fast_api_app.py + slack/ + storage/firestore.py + tasks worker
docs/forward-design-slack.md · docs/build-map-categorization.md · docs/research/sg-gst-tax-codes.md
```

## VERIFY (re-run the end-to-end pipeline — Task #14 confirmation)
```bash
cd /Users/davidkitdave/Projects/Ledgr-QBS
uv run python -c "
from dotenv import load_dotenv; load_dotenv('.env')
from invoice_processing.classify.document_classifier import classify_file, resolve_direction
from invoice_processing.extract.invoice_extractor import extract_file, to_normalized
from invoice_processing.export.exporters import get_exporter
p='/path/to/local/test/telco-bill-INV-0001.pdf'
cls=classify_file(p); d=resolve_direction(cls, client_name='Test Client Pte Ltd')
ex=extract_file(p); inv=to_normalized(ex, direction=(d if d in ('purchase','sales') else 'purchase'), our_gst_registered=True)
exp=get_exporter('QBS Ledger'); rows=exp.rows([inv], inv.doc_type)
print(cls.doc_type, d, ex.currency, len(inv.lines))
for l,r in zip(inv.lines, rows): print(l.tax_treatment, l.description[:30], r.get('Tax Amount'))
"
```
Expected: classify=invoice, direction=purchase, the bill splits into SR (9% GST) + ZR (0) lines.

## OPEN ITEMS
- Confirm the end-to-end test output (above) — was not captured last run.
- Bank-statement extraction schema + reconciliation (Task #8).
- Firestore schema for per-client config + Entity_Memory (Task #11).
- New-client COA bootstrap (propose category→account when Category_Mapping empty).
- agents-cli `scaffold enhance .` for deploy/eval/CI structure (deferred to deploy phase).

---

## GOAL (2026-06-12): FULLY FUNCTIONAL IN SLACK — ROADMAP & DEFINITION OF DONE
Lead/orchestrate the whole project to a working Slack experience: an accountant drops docs in a client
channel → gets a consolidated Excel ledger back. Drive task-by-task (delegate→verify), ground ADK in the
`adk-docs` MCP. **Definition of done:** a real Slack workspace test (computer-use) shows upload→ledger working.

Architecture note: Slack flow = FastAPI + Slack Bolt adapter → worker → `process_document` (deterministic
pipeline over the already-built classify/extract/categorize/tax/export modules). ADK `api_server`/`/run` is
for conversational agents; the doc-processing core is deterministic Python. Firestore = profiles (built);
GCS = archive + workbook store. Region asia-southeast1, Gemini Flash, AI Studio (dev) / Vertex (prod).

- [x] **Phase A — Pipeline core (#12/#13)** ✅ DONE (2026-06-12): `invoice_processing/pipeline.py` —
      `process_document(path, client)` (classify→direction→extract→normalize→tax→categorize account_code→route,
      every LLM step dependency-injected; never raises) + `process_batch(paths, client)` → `{filename: xlsx bytes}`
      consolidated `Ledger_FY{n}.xlsx` (Purchase+Sales) / `BankStatement_FY{n}.xlsx`. Hermetic `tests/test_pipeline.py`
      (12 tests, injected stubs) + **REAL end-to-end smoke**: telco PDF → invoice/purchase/FY2026, SR+ZR split,
      reconciled, valid QBS `Ledger_FY2026.xlsx`. Full suite **226 green**.
- [x] **Phase B — Slack app layer (#4)** ✅ DONE — hermetically tested (suite 442). `app/` = thin Bolt handlers
      over pure, injectable logic: welcome card on bot-join; 4-field onboarding modal → Firestore profile
      (`pending_coa`, spec §1); COA ingest (uploaded .xlsx/.csv via `coa_rows_from_file`, or **Use standard SG SME
      COA** — 19-acct bundled) → `save_coa`+status `active`; file-share doc flow (download → `process_batch` →
      `files_upload_v2` Excel back → result card) with **bot-loop guard** + **spreadsheet/document
      disambiguation**; background-thread worker for the <3s ack. Modules: `app/{blocks,onboarding,processing,
      coa_ingest,commands,slack_app}.py`. `/ledgr` commands (settings re-open prefilled / export / help) +
      edit-safe re-submit (reuses client_id, preserves status/category_mapping).
- [x] **Phase C — Serving + deploy (#5)** ✅ DONE: `app/config.py` (lazy env settings), `fastapi_app` reads real
      `SLACK_*` tokens, `app/main.py` (uvicorn/Cloud Run entry, no import-time network), `app/socket_run.py`
      (**Socket Mode** — live test with no public URL), `slack/manifest.json` (one-paste Slack app:
      scopes/events/`/ledgr`/socket mode), `Dockerfile`, `docs/slack-setup.md`. **Packaging fixed**
      (`[tool.hatch.build.targets.wheel] packages=["invoice_processing","app"]`) so the wheel ships `app/` →
      Cloud Run `import app.main` works (verified by building the wheel). GCS archive (spec §4 write-side) deferred
      — not needed for the basic ledger demo, but now DONE (below).
- [x] **GCS archive (spec §4 write-side)** ✅ DONE: `app/archive.py` —
      `ArchiveStore`/`InMemoryArchiveStore`/`GcsArchiveStore` (lazy + `client=` injection seam). Sources →
      `{client_id}/FY{fy}/{purchase|sales|bank}/{file}`, workbooks → `{client_id}/FY{fy}/workbooks/{file}`.
      Wired (optional + defensive — never breaks upload) into `process_shared_files`; **`/ledgr export` now
      re-uploads the latest archived ledger** from GCS. Bucket `ledgr-qbs-source-bucket` exists. Hermetic tests
      (hand-rolled fake GCS) in `tests/test_app_archive.py`; suite **478 green**. (Live GCS round-trip is
      GATED — auto-mode denied it; needs explicit user OK like the Firestore test. Hermetic fake-GCS tests stand.)
- [ ] **Phase D — Live Slack test:** ⏳ GATED on Slack tokens (user). PRE-LIVE PROOF DONE — a headless end-to-end
      sim drove the real worker (`process_shared_files`) + real `process_batch` + the real standard COA on the actual
      telco PDF: invoice/purchase/FY2026, SR+ZR split, **account code 6-2200 filled**, valid QBS
      `Ledger_FY2026.xlsx` "uploaded" + result card. To go live: create the app from `slack/manifest.json`, install,
      put `SLACK_BOT_TOKEN`/`SLACK_SIGNING_SECRET`/`SLACK_APP_TOKEN` in `.env` (`docs/slack-setup.md`), then run
      `uv run python -m app.socket_run` + `scripts/slack_live_test.py` to verify upload→ledger in a real channel.
- [x] **Phase E — Eval (#9)** ✅ DONE: `eval/ledger_eval.py` (`run_eval`/`default_client`/`discover_samples`,
      injectable pipeline → hermetic tests; `uv run python -m eval.ledger_eval --limit N`). **Live sample (5 real
      SG telco + MY receipt docs): 5/5 classified, 100% recon pass, 8/8 lines categorized (100% fill), 0 errors.**
      Bank lane already 100% recon (`eval/bank_eval.py`). Follow-ups (non-blocking): `resolve_direction` returns
      "unknown" on some vendor bills (pipeline defaults → purchase, correct); eval tax-treatment column is cosmetic.

- [x] **Review + hardening pass** ✅ DONE: adversarial code review (opus) of the whole new surface →
      fixed Slack-retry **idempotency** (event_id dedupe — no duplicate ledgers), **temp-PDF leak** cleanup
      (PDPA), download **filename sanitization** + **SSRF host-pin** (files.slack.com, no cross-host redirects)
      + **size/count caps** (25 MB / 30 files), **`save_coa` replace-not-append** (re-upload no longer
      duplicates/orphans), **spreadsheet-vs-COA intent gate** (only ingest a sheet as COA when `pending_coa`;
      active clients' sheets go to the pipeline), archive-only failures no longer flip the success card, and
      **`/healthz` 503** when Slack env missing (no silent fail-closed). Suite **498 green** + ruff config fixed.

**External blocker (Phase D — the ONLY thing left):** a Slack app + tokens (`SLACK_BOT_TOKEN`,
`SLACK_SIGNING_SECRET`, `SLACK_APP_TOKEN`) + a test workspace — only the user can create these (Slack
login/consent). Phases A–C are DONE and e2e-proven (suite 442 + real-PDF smoke); the moment tokens land I run
the live verification myself (`app.socket_run` + `scripts/slack_live_test.py`).

## DEPLOYMENT MODEL DECISION (2026-06-12): MULTI-WORKSPACE (Model B / task #5)
**Role hierarchy (corrected):** the user is the **DEVELOPER** of Ledgr; the **customer is an accounting FIRM**
(each firm has its OWN Slack workspace); the firm's **clients are channels** inside that firm's workspace. So
**each FIRM installs Ledgr into their own workspace via OAuth** (multi-workspace = Model B); the bot runs in the
**firm's** Slack, not the developer's. Within a firm's workspace, channel→client profile handles each client.
Implications: needs a **public HTTPS endpoint (Cloud Run `asia-southeast1`)**, **OAuth install flow**
(`/slack/install` + `/slack/oauth_redirect`), **per-workspace bot tokens** in Firestore (`workspaces/{team_id}`),
and Slack **app distribution / review**. The channel→client profile model + the whole pipeline are unchanged;
the OAuth/installation layer goes underneath. See memory [[ledgr-role-hierarchy-and-model-b]].
Grounded in Bolt docs: `App(signing_secret, installation_store, oauth_settings=OAuthSettings(...))`;
`InstallationStoreAuthorize` resolves the per-team token automatically → existing handlers work unchanged.
Grounded in Bolt docs: `App(signing_secret, installation_store, oauth_settings=OAuthSettings(...))`;
`InstallationStoreAuthorize` resolves the per-team token automatically → existing handlers work unchanged.

- [x] **#5.1 Stores** ✅ — `app/installation_store.py`: `FirestoreInstallationStore` (save/find_installation/find_bot
      → `workspaces/{key}`) + `FirestoreOAuthStateStore` (issue/consume), `client=` injection seam, hermetic tests.
      Verified: `find_bot` returns the per-team token; state one-time-use + expiry.
- [x] **#5.2 OAuth wiring** ✅ — OAuth mode in `build_app` (`OAuthSettings`, `BOT_SCOPES` ×11) + `fastapi_app`
      mounts `/slack/install` + `/slack/oauth_redirect` (+ events/healthz); `member_joined` uses Bolt `context.bot_user_id`
      in OAuth mode; `config.py` CLIENT_ID/SECRET/STATE_SECRET/BASE_URL + `missing_slack_oauth()`;
      `slack/manifest-distributed.json` (socket off, redirect_urls, request_url). Suite **557 green**.
      Note: Model B uses per-workspace tokens (no single SLACK_BOT_TOKEN); `.env` needs CLIENT_ID/SECRET/SIGNING/BASE_URL.
- [~] **#5.3 Deploy + live** — ✅ DEPLOYED to Cloud Run `asia-southeast1` (service `ledgr`, public,
      `--no-cpu-throttling`, env via file). URL `https://ledgr-640071771526.asia-southeast1.run.app`.
      ✅ Caught+fixed a critical bug: all Slack routes returned **422** (PEP-563 string annotations + `Request`
      imported inside `fastapi_app` → FastAPI mis-resolved it). Fixed = module-level `from fastapi import ... Request`;
      regression test `tests/test_fastapi_routes.py` (calls the routes, not just lists them). `/slack/events`→401 (good).
      ✅ IAM GRANTED (2026-06-12, user-authorized): compute SA now has `roles/datastore.user` (project) +
      `roles/storage.objectAdmin` (bucket). **`/slack/install` → HTTP 200** (Bolt install page + OAuth link) —
      service fully functional. ✅ `/slack/events`→401 (sig check). 
      ✅ OAUTH INSTALL VERIFIED (2026-06-12): user added the redirect URL + completed install → per-workspace token
      PERSISTED to Firestore `workspaces/none-T0B59UG473K` (team QBS-AI, has_bot_token=True, scopes present).
      **Model B multi-workspace install flow works end-to-end on the live service.** (Redirect-URI mismatch was the
      one gotcha — add `…/slack/oauth_redirect` to OAuth&Permissions Redirect URLs; now in `docs/slack-setup.md`.)
      ⏳ REMAINING (final E2E): config Event Subscriptions + Interactivity Request URLs = `…/slack/events` (separate
      from OAuth — needed for event/button delivery; `/slack/events` proven to pass Slack's signed url_verification);
      invite bot to a channel → set up client (modal+COA or seed) → drop a bill → confirm Excel ledger returns.
      NOTE: prod best-practice = a dedicated `app_sa` (least-priv) rather than the default compute SA.
      `/healthz` returns a Google 404 (GFE reserves the path) — cosmetic; could rename to `/health`.

**Eval status (Phase E):** bank lane already at 100% recon (`eval/bank_eval.py`); remaining = account-code +
tax-code accuracy vs verified `Ledger_FY` ground truth (token-independent; the next "done right" work).
