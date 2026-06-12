# Ledgr — Forward Design (Slack-native)

Supersedes the legacy Google-Sheets/Drive workflow. Slack is the interface; our own datastore holds
client config; the agent classifies a document, extracts it, categorises to the client's accounts, and
returns Excel in Slack. See also docs/build-map-categorization.md and docs/research/sg-gst-tax-codes.md.

## Decisions locked
- **Client ↔ Slack:** one **channel per client** (firm installs the app once; `#envistore`, `#acme`, …).
  The channel identifies the client → resolves COA, tax rules, and **sales-vs-purchase direction**.
- **Throughput:** **batch** — users drop many docs; agent returns **one consolidated workbook**
  (Purchase + Sales sheets; bank statements get their own sheet/workbook).
- **COA onboarding:** client **uploads their COA once at setup** (stored in our datastore); no Google Sheet.
- **v1 document types:** **purchase invoices/bills, sales invoices, receipts, bank statements.**
- (Earlier) Singapore first, deploy `asia-southeast1`, Gemini Flash only, QBS Ledger + Xero output,
  SR/ZR/ES/OS tax logic, Agent Platform Sessions.

## Agent pipeline — classify first
```
document(s) dropped in a client channel
   ▼
CLASSIFY (Gemini multimodal): purchase | sales | receipt | bank_statement | other
   │   (sales vs purchase = direction; resolved with the client's identity from the channel)
   ▼
ROUTE → specialist
   ├─ purchase/receipt  → purchase extraction (vendor, bill no, date, lines, GST SR/ZR)
   ├─ sales             → sales extraction (customer, inv no, date, lines, GST)
   └─ bank_statement    → transaction extraction (date, desc, debit/credit, balance + math-check)
   ▼
CATEGORISE each line → client's account code (Entity_Memory → category map → COA-keyword match)
   ▼
TAX code per line (SR/ZR/ES/OS), only if client is GST-registered
   ▼
APPEND to the consolidated workbook (QBS Ledger or Xero, per client) → post in Slack
```

## ADK mapping (on top of the existing agent)
- **Router/coordinator** = an `LlmAgent` whose first job is doc-type classification, delegating to
  specialist sub-agents (or internal pipelines) via `transfer_to_agent` / tool calls. The existing
  `run_inference` (Acting→Investigation→ALF) becomes the **purchase/sales** specialist path.
- **Per-client context** = loaded into `session.state` by a `before_agent_callback` from our datastore
  (keyed by the channel→client map): COA, category map, Entity_Memory, tax_registered, software, currency.
- **Categorisation** = `resolve_account` FunctionTool reading `tool_context.state` (deterministic-first).
- **Learning** = `remember_entity` tool → per-client Entity_Memory store (not ADK Memory Bank).
- **Sessions** = `VertexAiSessionService`; isolation via channel/client id + `user_id`.
- **Export** = existing `export/` module (QBS Ledger / Xero workbooks), now fed real account codes.

## Slack UX
- **Setup (once per client):** `/ledgr setup` modal (name, UEN, region, GST-registered, target ledger)
  + **upload COA** (or start from a standard SG SME COA, replaceable later). Creates the channel↔client
  binding in our datastore.
- **Daily use:** drop docs in the client channel → 👀 *"Processing 14 documents…"* → per-doc result
  cards in a thread (`🧾 Purchase · Starhub · SGD 1,269.22 · SR+ZR · Telco 61010 · ✅/✏️`; low-confidence
  → `⚠️ review`) → **one consolidated Excel** attached when the batch finishes (or `/ledgr export`).
- **Corrections:** ✏️ on a card → fix account/tax → re-posted + **remembered** (Entity_Memory) for next time.
- **Bank statements:** transaction-table Excel with running-balance reconciliation.

## Infra
- **Cloud Run** service (FastAPI + Slack routes + worker) in `asia-southeast1`.
- **Firestore:** per-workspace bot tokens; channel→client map; per-client config + COA + Entity_Memory.
- **GCS:** uploaded source docs + generated workbooks (per client/tenant prefix).
- **Vertex AI (Flash):** classification + extraction (multimodal).

## Build sequence (revised)
1. **Doc-type classifier/router** — multimodal classify {purchase, sales, receipt, bank_statement, other};
   test against real Cast Unity / MYDoc files (known types). *(the piece the agent most needs)*
2. **Per-type extraction** — purchase/sales (adapt existing pipeline) + receipt (photo) + bank statement (new).
3. **Per-client datastore + COA onboarding** — Firestore schema; `/ledgr setup` + COA upload; channel↔client.
4. **Categorisation + tax** — `resolve_account` + tax classifier (built) → fill account codes + tax codes.
5. **Batch + consolidated workbook** — accumulate a batch → one QBS/Xero workbook → Slack.
6. **Slack glue** — channel-per-client install, events, files_upload_v2, result cards, ✏️ corrections.
7. **Learning** — corrections → Entity_Memory.
8. **Eval loop** — `agents-cli eval` to ≥0.9 across the doc types (task #9).

## Verification
Per stage: classifier accuracy on labelled real docs; extraction vs the verified `Ledger_FY` rows;
account-code + tax-code accuracy; end-to-end batch → workbook in a Slack test channel.
