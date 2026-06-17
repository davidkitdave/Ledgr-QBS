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
Human-in-the-loop check before a low-confidence extraction is committed to a
Workbook. A document needs Review when the Engine flags it: not reconciled, tax
confidence below threshold, or otherwise flagged. Review is realised with **ADK
2.0's native `RequestInput`** node inside a *slim* approval Workflow
(Engine node → approval node → deliver node — no per-step LLM). A human's
approve/edit in Slack resumes the paused node via the Firestore interrupt bridge.
An approve-with-edit becomes a [[Correction]] the Engine remembers.

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
