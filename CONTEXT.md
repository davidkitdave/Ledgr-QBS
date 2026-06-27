# Ledgr — Context Glossary

The shared language for this project. Definitions only — no implementation detail.
When a term here conflicts with how code or conversation uses a word, this file wins
until the team deliberately changes it.

---

## Firm
The paying customer — an accounting/bookkeeping practice. A firm installs the Ledgr
Slack app once into its own Slack workspace (via OAuth). One firm has many Clients.

## Client
One of the firm's end customers (the business whose books are being kept).
**A Client is identified by exactly one Slack channel.** The channel resolves the
client's chart of accounts, tax rules, financial-year end, and sales-vs-purchase
direction. "Per channel" and "per client" mean the same thing.

## Teammate
The conversational face of Ledgr in a channel. Implemented as the **Coordinator**:
an ADK `LlmAgent` that is the **entry node of a slim `Workflow` graph** (the graph
is the runtime root — a bare `LlmAgent` cannot host the human-input node Review
needs). The Coordinator reads each human message, decides intent, replies in plain
language, and routes to the right branch. "Make the agent a teammate" means: stop
being a silent file-processor and start answering and acting on messages. The
Teammate *talks and routes*; it does **not** itself extract data.

## Accountant agent (the "clean agent")
The rebuilt, lean `ledgr_agent` ADK `LlmAgent`. The phrase "clean agent" names
**two surfaces**, and they must not be conflated:
- the **accountant LlmAgent** — the conversational / `adk web` / agents-cli / eval
  surface, where the agent itself orchestrates; and
- the **document spine + tool** — the production document-processing path, a
  deterministic spine that calls the agent's `process_document_batch` tool
  directly. **In production the LlmAgent does not orchestrate.**

So "is the clean agent in prod?" is answered at the *tool/spine* level, not the
LlmAgent level. Governing principle (ADR-0026): the **LLM reads** (extraction via
the tool) and **deterministic Python applies** tax treatment and COA codes — the
LLM never decides tax codes. The legacy [[Engine]] graph (`accounting_agents`) is
the thing being retired in favour of this.

## Engine (processing pipeline)
The document-processing path: classify → **understand** → categorise → tax →
workbook. It is **intelligent at the document boundary, deterministic after**.

- **Understand** — one multimodal Gemini call per standard invoice/receipt/telco
  bill, returning a Drive-style [[Document Summary]] plus [[Ledger lines]] in a
  single structured schema (`DocumentLedgerExtract`). This replaces the old
  faithful-capture + regex-normalize bridge for those doc types.
- **Policy** — reconcile, tax rules, COA categorisation, and export projection
  stay plain Python (auditable, testable).
- The Engine runs inside a **slim ADK Workflow graph** — never as a chain of
  per-step LLM agents (an earlier rewrite burned tokens and was retired; see
  docs/adr/0001). See [[Understand layer]] and ADR-0011.

**Verified empirically (2026-06-26, issue #28, branch
`feat/minimal-extract-control-experiment`):** one direct `generate_content`
call on the real Starhub bill (18 pages, 4.4 MB) returns 1 doc / 3 lines /
clean SR+ZR GST breakdown in 8.9 s; the chunked factory returns 12 fake docs
/ 216 noisy lines in 209.8 s. The chunked path *causes* the truncation it
was added to prevent. The minimal direct-call path is now wired into
`ledgr_agent` as a `FunctionTool` alongside the full pipeline
(ADR-0030). "Smart factory" was the wrong instinct for a per-bill read;
Drive-style direct extraction is the correct shape.

**Light path (ADR-0031):** after the read, add **policy rounds only when
proven necessary** — R1 one LLM read alone → R2 deterministic tax → R3 COA
from the client's list (LLM batched match) → R4 route + export. The spike
`scripts/spike_light_vs_factory.py` climbs that ladder per fixture before
any production cutover.

## Light path
The **experimental minimum spine** for replacing the heavy extraction factory
(ADR-0031). Two related shapes live in `ledgr_agent` today — do not conflate them.

### Commercial bill → ERP (shipped eval gate, 2026-06-27)

For **one** invoice / tax invoice / receipt / credit note when the user wants
**ERP import rows** (QBS, Xero, AutoCount, SQL Account):

1. **Read** — `read_document` `FunctionTool`: one Gemini call with the real PDF
   bytes and structured `ReadDocument` schema (`ledgr_agent/agents/document_reader.py`).
2. **Project** — `project_to_erp`: deterministic YAML-driven row mapping
   (`ledgr_agent/export/erp_projection.py`); **no second LLM call**.

**Not** this path: bank statements, SOA / multi-invoice PDFs, multi-file batches,
COA/tax engine — see bank path below or `process_document_batch`.

Eval: `tests/eval/datasets/light-read-erp-smoke.json` +
`tests/eval/eval_config_light_read.yaml` (Auditair ISO golden: purchase, QBS
Sub Total 2800, Tax 252, Total 3052).

### Bank statement → workbook (shipped eval gate, 2026-06-27)

For **one** bank / account statement PDF when the user wants **workbook tabs**:

1. **Read** — `read_bank_statement`: hybrid pdfplumber digital + vision fallback;
   structured `ReadBankStatement` (`ledgr_agent/models/bank_statement.py`).
2. **Normalize** — running-balance reconcile + deterministic tab titles
   (`ledgr_agent/normalize/bank_statement.py`).
3. **Project** — `project_bank_workbook`: QBS bank column rows per account+currency;
   **no second LLM call** (`ledgr_agent/export/bank_workbook.py`).

Eval: `tests/eval/datasets/light-read-bank-smoke.json` +
`tests/eval/eval_config_light_read_bank.yaml`.

**Not** this path: commercial bills → `read_document`; SOA / FY merge → factory.

### Policy ladder (R1–R4, still experimental)

One direct Gemini read (`LedgerRowBundle`) then a **policy ladder**
added only when A/B evidence requires it:

1. **Read** — LLM fills vendor, reference, `tax_lines[]`, summary charge rows;
   **no account codes** in this step.
2. **Tax** — deterministic `classify_invoice` after bridging printed tax onto
   lines; LLM does not decide tax codes (ADR-0026).
3. **COA** — `categorize_invoice`: entity memory / corrections first, then **one
   LLM call against the client's own COA list** for unresolved lines.
4. **Deliver** — deterministic FY route + exporter projection.

Implemented in `ledgr_agent/tools/light_ledger.py`; measured by
`scripts/spike_light_vs_factory.py`. Production still uses
`process_document_batch` until eval gates the winning round per doc type.

## Document truth (QA)
Independent checks that exported rows cover what the **source PDF text** shows
(e.g. every invoice number and total in a multi-invoice SOA), without trusting
the LLM extraction. Implemented in `ledgr_agent/tools/document_truth.py`; used
in spikes and eval to catch "only one of five invoices exported" failures.

## Understand layer
The **intelligence** step at the document boundary: one Gemini multimodal call
with a structured JSON schema that returns both human-readable facts and
accounting-meaningful lines. Matches Google Drive side-panel behaviour (Category /
Details summary + collapsed ledger lines for telco). **Not** faithful OCR of every
row followed by Python regex to re-summarize. SOA packages and complex multi-doc
splits still use the legacy capture path (ADR-0011).

## Document Summary
The Drive-style **Category / Details** table the Understand layer returns
alongside ledger lines (`DocumentLedgerExtract.summary_table`). Used internally
for eval, debug, and Drive-parity checks — **not** shown in Slack (ADR-0011).
Slack shows the ledger preview data_table and FY workbook instead.

## Ledger lines
The small set of charge rows the Understand layer returns for posting — e.g. one
line for a simple invoice, two SR/ZR summary lines for a telco bill. Mapped into
[[Canonical Schema]] `InvoiceLine` entries; tax treatment and account codes are
applied in later **Policy** steps, not in Understand.

## Batch (Job)
The unit of work a human creates by dropping one or more documents at once. Even
though each document is handled on its own, the batch is **reported as a single
Job** — one summary of what was posted and what still needs [[Review (HITL)]],
rather than one message per document. "The job" is how a human refers to a drop
and its outcome.

## Review (HITL)
Human-in-the-loop check triggered by **material ambiguity** — a document that
won't reconcile, is missing a required field, comes from a brand-new vendor with
no known mapping, or is illegible. Review is **not** triggered merely because a
document's type label is unfamiliar; a cleanly extracted `other` or `expense_claim`
posts without a pause. Review is realised with **ADK 2.0's native `RequestInput`**
node inside a *slim* approval Workflow (Engine node → approval node → deliver node
— no per-step LLM). A human's approve/edit in Slack resumes the paused node via the
Firestore interrupt bridge. An approve-with-edit becomes a [[Correction]] the Engine
remembers. See ADR-0017 for the full signal taxonomy.

## Correction
A human-supplied fix to how a Client's documents are handled — e.g.
"vendor X belongs to account 61010", "this vendor's GST is in the second column".
A Correction is **structured and per-client**, stored durably, and **applied
deterministically by the Engine on the next document**. It is the unit of
"learning". Distinct from a one-off edit to a single result.

> A Correction is *not* how you fix "the extractor never captured this column at
> all" — that is an Engine schema/prompt fix, not something remembering can solve.

## Financial Year (FY)
The accounting year a document belongs to, derived from its date and the Client's
financial-year-end month. FY routes a document to the correct workbook and archive
location. (e.g. FY2025.)

## Workbook
The consolidated Excel output for a Client, per FY and per kind (ledger vs bank).
It **accumulates** — uploading a new month updates the existing workbook in place
rather than creating a duplicate. The previous workbook is retrieved from the
**system of record** (see below) to append to.

## System of record
**The Slack channel's Files tab.** Documents and workbooks live in Slack, not in
external object storage. (An earlier GCS archive is being retired as the record.)
Ledgr storage is working/ephemeral — the Client's own accounting software remains
the authoritative books.

## FY Filing View (Canvas Index)
How "filing documents into the right financial-year folder" is realised, given a
hard Slack constraint: **Slack channel folders are a manual UI-only feature with
no Web API** — a bot cannot create a folder or move files into one. Instead the
Teammate maintains a channel **Canvas** with one section per FY, each listing the
Client's documents (vendor · date · type · status) with a permalink to the file.
The Canvas is the tidy "folder view"; the raw chat may stay noisy. A human may
optionally drag the Canvas + files into a real Slack folder by hand.

## Canonical Schema
The single, **software-agnostic** model the Engine maps **understood** documents
into (`NormalizedInvoice` / `BankStatement`) — a **superset** of what any accounting
target needs. The Understand layer produces ledger-ready lines; Policy steps fill
tax treatment and COA codes before per-software **exporters** project into each
target's import template (QBS Ledger, Xero) at write time: *one understanding →
many exports*. The canonical schema is never shaped to one software's headers.
See ADR-0005 and ADR-0011.

## Completeness Contract
The set of fields extraction **must** fill = the **union of every target template's
required headers** (Xero's `*` columns + QBS Ledger's required set, sales and
purchase). Extraction quality is judged as **per-required-header fill rate, per
target** — a blank required cell is a measurable, attributable failure.

## Chart of Accounts (COA)
The client's **own** list of account codes (code · description · account type ·
financial-statement section · nature · keywords), uploaded per client and held in
their profile. It is the **single source of truth** for [[Categorisation]]. There is
**no baked-in generic/standard COA** — account numbers differ per client, so a
client must provide (or have us build) their own. Captured via three paths
(upload · guided export · build-from-prior-financials) under a **soft gate**:
documents always ingest and extract, but lines are only written to the import
template against a validated COA — never a generic default. See ADR-0006.

## Categorisation
Assigning each extracted line to one of the client's own COA codes.
**Deterministic-first**: remembered vendor ([[Correction]] / Entity_Memory, conf
0.95) → category→client-code map (0.9) → COA keyword match (0.8). Whatever remains is
judged by **one LLM call against the client's own COA**; low-confidence lines are
flagged → [[Review (HITL)]] → fix becomes a [[Correction]]. No account numbers are
hardcoded.

On the [[Light path]], round 1 **does not** assign account codes. Round 3 is this
same categorizer — the LLM sees the client's COA JSON and picks the best key per
line; do not patch keyword matching in the spike to fake a match.

## Direction
Which side of a [[Client]]'s books a document belongs to: **purchase** (the Client
pays the vendor) or **sales** (the Client issues / collects). Direction is a
*reading* decision and is **universal** — every accounting target separates purchase
from sales.

**Resolution (deterministic-first, mirroring [[Categorisation]]):** the
[[Understand layer]] reads Direction from the document; for a vendor the Client has
already taught us, the Client's recorded buy/sell **role** ([[Correction]] /
Entity_Memory — Creditor = buy-from, Debtor = sell-to) acts as a deterministic prior.
It **fills in** when the read is *unknown*, **agrees** silently when they match, and
**flags a [[Review (HITL)]]** when it *conflicts* with a confident read — it never
silently overrides the document. A brand-new vendor with no recorded role takes
**one** Review pause, after which the role is remembered and the floor handles it. The
role rides on buy/sell only, so it is **ERP-independent** (it never uses an ERP's
account codes); when no role is on file the floor has no data and degrades to the pure
LLM read.

Direction is a *necessary input to* the [[Accounting Module]] but **not the whole
answer** — where a document lands also depends on whether the counterparty is a tracked
[[Creditor / Debtor code]]. (A document where the Client is both issuer and payer is
*self-referential*.)

## Creditor / Debtor code
A specific software's **identifier for one tracked supplier (Creditor) or customer
(Debtor)** — the code under its AP / AR control account. It is **per-client master data
the Client owns**, never something Ledgr invents. Ledgr **learns** a code (from a
[[Correction]] / Entity_Memory, or by ingesting the Client's own **Creditor / Debtor
balance report**) and **resolves** a document's counterparty to it — but never
fabricates or hardcodes one. Same authority rule as the [[Chart of Accounts (COA)]]:
the **Client's own scheme wins**; there is no generic default and no example client's
codes are baked in. A target that requires the code (e.g. AutoCount / SQL AP/AR
Invoice) cannot post a credit document without it, so a brand-new counterparty takes
**one** [[Review (HITL)]] pause to capture its code, then is remembered. A counterparty
with no code on a **paid** document falls to the [[Accounting Module]] CashBook path
(no code needed).

## Payment (settlement)
Whether a document has been **paid** is a **settlement** fact — **not** a property that
reroutes the document. A bill is booked as a payable / receivable when it is understood;
the matching payment is a **separate event** that arrives through the **bank lane** (a
bank-statement line — realised in some targets as a Payment Voucher / Official Receipt
that knocks off the invoice). Ledgr therefore does **not** stamp a paid/credit status on
an invoice to decide where it lands — see [[Accounting Module]]. *Exception:* a one-off
paid expense with **no** [[Creditor / Debtor code]] (petty cash, a directly-paid utility)
is booked straight to an expense account via a CashBook-style module.

## Accounting Module
**Where a document finally lands in a specific software's books** — a **per-target
projection** described entirely by **that target's own profile** (rule-data), never
hardcoded to any one client or ERP. The driver is the **counterparty, not payment**: a
bill from a tracked supplier / customer posts to the target's **AP / AR Invoice** module
against that party's [[Creditor / Debtor code]]; a one-off cash expense with **no** such
account posts to a **CashBook** module (Payment Voucher / Official Receipt) straight to
an expense / income account. Targets that split a CashBook (e.g. AutoCount, SQL Account)
carry both; targets with no split (e.g. QBS Ledger, Xero) **collapse the Module to
[[Direction]]** — one sheet per side, payment reconciled separately. Introducing or
changing a module is a **profile edit** the export, the Excel [[Workbook]], the Slack
preview table and the [[Batch (Job)]] summary all **follow** — the
[[Completeness Contract]] gains that module's required headers for the targets that
declare it.

## Credit
The prepaid unit a [[Firm]] spends to use Ledgr. A firm buys credits up front (a
[[Top-up]]); processing a document consumes them. The balance is held **per Firm** —
one balance shared across all of that firm's Client channels, not one per Client.
**1 credit = 1 [[Billable unit]].**

## Billable unit
What one credit pays for, which differs by document kind:
- **Bank statement:** one **source-PDF page** = 1 credit (the uploaded
  document's page count, not the number of extracted transaction rows).
- **Invoice / receipt:** one **unique document written to the ledger** = 1 credit —
  *not* per page. One PDF may hold several invoices, or one scanned page several
  receipts (each counts); one invoice spanning several pages counts once. A skipped
  SOA cover page is not a billable unit.

A document is a billable unit only when it is **written to the ledger** (delivered).
Documents rejected as unreadable, and documents detected as duplicates of one already
in the ledger, are **not** billable.

## Top-up
The act of adding credits to a Firm's balance. Payment for the credits is handled
out-of-band (the firm pays the developer); the top-up is the resulting credit grant
recorded against the firm.

## Expense claim
A recognized billable document kind: an employee or staff reimbursement with
itemised expense lines, booked like a purchase (expense lines + tax treatment +
COA categorisation). Expense claims are a **first-class doc type**, not `other`.
The Engine understands and posts them without a [[Review (HITL)]] pause when the
extraction reconciles cleanly. See ADR-0017.

## other (doc type)
The label assigned when a document does not match any named doc type. `other` means
**processable-but-unclassified** — the Engine still runs the Understand layer and
attempts a booking; it is **not** an error. An `other` document that reconciles
cleanly posts without a [[Review (HITL)]] pause. The truly unbookable case —
a document the Engine cannot meaningfully post — is signalled by `processable=False`
(a hard escalation signal), **not** by the `other` label alone. See ADR-0017.

## Familiarity
A per-client learned signal meaning "stop asking about this document shape or vendor."
Stored as a Firestore subcollection `clients/{client_id}/familiarity/{key}` (keyed
by `doc_type` or `doc_type:vendor`) holding `{seen_count, last_seen_at,
last_direction}`. When `seen_count` reaches the threshold, soft [[Review (HITL)]]
signals for that key are suppressed — escalation decays per client as it learns.

**Distinct from [[Correction]]:** a Familiarity record means *"seen this, trust it"*
and lowers the escalation rate; a Correction means *"this mapping was wrong — fix
it."* A Correction changes the output; Familiarity changes whether to pause.
Both live per-client in Firestore; both extend the learning system (ADR-0004).

**Not called "confirmation":** the codebase already uses `committed_confirmations`
(`accounting_agents/slack_runner.py`) as the ADK Tool-Confirmation idempotency
marker — an entirely unrelated mechanism. Using the same word would create
ambiguity in search and review.

## Delivery endpoint *(roadmap)*
A per-destination projection of the [[Canonical Schema]] — one understanding of a
document rendered into the format a specific target needs. Excel/Slack delivery is
the current implementation. Future endpoints include ERP REST API push (Xero/QBO),
legacy batch-import file generation (`.iif`/`.csv`), and optional RPA automation.
The principle: **one understanding → many deliveries**. See ADR-0005 and
ADR-0019 (target architecture).
