# 0009 — Chat write tools (amend/remove ledger row) via ADK Tool Confirmation

- **Status:** Accepted
- **Date:** 2026-06-15
- **Deciders:** Ledgr team
- **Supersedes/extends:** ADR-0007 (HITL surface), ADR-0008 (chat lane standalone root agent)

## Context

Through Step 3 the chat assistant has read + explain tools only. The felt gap (noted
within minutes of Step 1 going live): a user can point at a wrong row in chat
("the AWS line should be 6010, not 6090") but the assistant has no hands — it can
explain, not fix. Master-plan Step 4 (C-2) gives it hands: `amend_ledger_row` and
`remove_ledger_row`, **gated so no write happens without an explicit human OK**.

Three forces constrain the design:

1. **The chat lane is a non-resumable standalone `LlmAgent`** (ADR-0008), not a
   graph. The document lane's `adk_request_input` HITL is graph/resumability
   machinery — wrong shape for chat.
2. **Every write must pause for a human yes/no**, and the "yes" arrives on a
   *separate* Slack turn (a fresh `runner.run_async` on the same per-thread
   session). The pending write must survive between turns.
3. **Singapore GST master gate (§0.5-C / ADR-pending memory `sg-gst-tax-rule-and-xero-codes`)**:
   a non-GST-registered client gets `NT` on every line regardless of what the human
   typed. A write tool must re-run the classifier, not trust free text.

## Decision

**Use the ADK Tool Confirmation primitive** (`FunctionTool(require_confirmation=True)`
+ `tool_context.request_confirmation(hint, payload)`) as the write-gate transport,
expressed as a **two-turn in-chat confirm**.

Evidence the primitive works on our stack (architect spike, 2026-06-15):
- The confirmation pause is a plain *long-running* `adk_request_confirmation`
  function call (`functions.py:generate_request_confirmation_event`), yielded in
  normal tool postprocessing — **not** gated on resumability.
- The pending request lives in `event.actions.requested_tool_confirmations`, a typed
  `EventActions` field. Our `FirestoreSessionService` persists events via
  `model_dump_json()` and rehydrates via `Event.model_validate_json()`
  (`sessions.py:206,242`) — a byte-faithful round-trip, the *same* fidelity that
  already makes `adk_request_input` resume work here.
- The documented "unsupported on DatabaseSessionService / VertexAiSessionService"
  limitation is a **serialization-fidelity** problem (legacy pickle / lossy managed
  schema), **not** a resumability or session-class problem. It does not apply to our
  JSON-Pydantic-faithful custom service. A smoke test asserts the round-trip.

### Control flow (two Slack turns)

**Turn 1 — propose.** User: "change the AWS line to 6010." Model calls
`amend_ledger_row(...)`. The tool body (no `tool_context.tool_confirmation` yet):
locates the target row by its `(_sheet, _row)` coordinate, computes the **post-write
preview including re-classified tax** (§0.5-C), then calls
`tool_context.request_confirmation(hint=<human-readable diff>, payload=<canonical
write spec>)` and returns a "needs confirmation" status. The Slack runner surfaces
the `hint` as the assistant's reply.

**Turn 2 — commit.** User: "yes." The Slack runner detects a pending
`adk_request_confirmation` for this session and synthesises a `FunctionResponse`
(`name="adk_request_confirmation"`, matching `id`, `response=ToolConfirmation(
confirmed=…, payload=…)`, `by_alias=True`). ADK re-executes the tool with
`tool_confirmation.confirmed=True`; the commit branch writes to the workbook via the
new `SlackLedgerStore` mutation method. A non-affirmative reply ("no"/"cancel") →
`confirmed=False` → the tool returns "cancelled", nothing is written.

### Row addressing

`SlackLedgerStore.read_rows` is extended to stamp each row dict with `_row` (the
1-based worksheet row number) alongside the existing `_sheet`. This is additive
(existing read/explain tools iterate known headers and ignore underscore keys), and
gives chat write tools a **deterministic cell coordinate** instead of fragile
content-matching. `lookup_row` surfaces `(sheet, row)` so the model can target a row.

### Workbook mutation (new `SlackLedgerStore` methods)

- `amend_row(client_id, fy, slack_client, channel_id, sheet, row, updates)` and
  `remove_row(client_id, fy, slack_client, channel_id, sheet, row)`: download the
  pointed-to workbook, mutate by exact `(sheet, row)`, upload a **new file version**,
  update the Firestore pointer's `slack_file_id` — the same accumulate-the-record
  contract as `append_rows` (memory `ledger-accumulates-keep-record`).
- **Scope:** invoice-ledger rows only (Purchase/Sales). Bank-statement rows carry a
  derived running balance (memory `bank-ledger-continuous-sorted`); amending/removing
  one would desync balances. The tools detect a bank sheet and refuse with a clear
  message rather than corrupt the balance chain. Not a shim — a deliberate boundary.
- `seen_doc_keys` is left intact on amend/remove: editing one line does not "un-see"
  the source document.

### Tax re-classification on commit (§0.5-C)

The commit branch reconstructs the affected line as an `InvoiceLine` on a
`NormalizedInvoice` whose `our_gst_registered` comes from the **client profile**
(not the doc, not the chat), and calls `TaxClassifier().classify_line(line, inv)`.
The classifier's master gate is therefore re-applied: a non-registered client's
amended line is forced to `NT` even if the user (or the model) asked for `SR`. The
preview shown in Turn 1 is computed the same way, so preview == commit.

### Audit

Every committed write appends an audit record (who, when, session, sheet, row,
before→after, re-classified tax) — reuse the correction/op-id logging shape already
in `slack_runner`.

### Silence guardrail (§0.5-B)

Carry the Step-1 pattern: the assistant instruction explicitly requires a final text
reply after a tool/confirmation call, and the runner's `extract_tool_response_text`
safety net surfaces the tool result if the model goes silent.

## Consequences

- The assistant can fix the book from chat, safely, with a one-word confirm — no
  modal, no graph, no new resumability machinery.
- Writes are auditable and tax-correct by construction (the gate cannot be bypassed
  via chat free text).
- Bank rows stay read-only from chat until a balance-aware editor exists (future).
- We take an explicit dependency on an **experimental** ADK feature
  (`@experimental(FeatureName.TOOL_CONFIRMATION)`); the smoke test + unit tests pin
  the behavior we rely on so an ADK bump that regresses it fails CI.

## Alternatives considered

- **`adk_request_input` HITL (doc-lane mechanism)** — battle-proven, but built for the
  resumable graph; adopting it forces graph-ification or heavy interrupt-doc/resume
  machinery into the deliberately-simple chat lane. Kept only as the fallback if the
  smoke test fails (evidence says it will pass).
- **Pure custom two-turn pending-write in `state` (no Tool Confirmation)** — reinvents
  request/original-call matching and stale-confirm guards that the framework already
  provides; more bespoke test surface. Rejected.
- **Direct write, no confirm** — violates the §11 anti-goal "every write is gated."
  Rejected outright.
