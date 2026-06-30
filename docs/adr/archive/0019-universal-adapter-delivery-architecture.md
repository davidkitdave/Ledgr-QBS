> **Archived 2026-07-01** — Describes the removed `accounting_agents` / `invoice_processing` graph and factory. **Live runtime:** `ledgr_slack` + `ledgr_agent` ([ADR-0032](../0032-ledgr-agent-and-slack-two-packages.md)). History only; do not implement against this doc.

# 0019 — Universal Adapter delivery architecture (target)

- **Status:** Accepted (target) — **not implemented**
- **Date:** 2026-06-18
- **Deciders:** Ledgr team

## Context

ADR-0005 established a software-agnostic [[Canonical Schema]] projected per target
by per-software exporters. Today two delivery targets are implemented: Excel
workbooks written to Slack (QBS Ledger and Xero templates) and a Slack delivery
card. Firms have stated that the highest-pain gap is getting data into their
accounting software — manual re-entry from the Excel output is the current
workaround.

Three future delivery channels have been identified:

1. **ERP REST API push** — direct Xero / QuickBooks Online API write; removes
   manual import entirely but requires per-firm OAuth token storage and live API
   error handling.
2. **Legacy batch-import file generation** — deterministic projection of the
   canonical schema into `.iif`, `.csv`, or `.txt` formats accepted by legacy
   accounting software batch-import flows; no external auth, pure data
   transformation.
3. **RPA automation** — driving a desktop accounting application via a CLI or
   browser automation layer (e.g. Daytona) for software that offers neither an
   API nor a batch-import format; highest complexity, lowest priority.

The ADK `Artifact Service` is the correct mechanism for transient delivery
artefacts (generated import files); verified against ADK docs. ADK also supports
A2A (Agent-to-Agent) communication, but this must be used selectively — only when
a delivery adapter genuinely runs in a different runtime environment (e.g. an
on-premises RPA agent on the firm's own network). For all adapters that run in the
same Cloud Run process, in-process `FunctionTool` endpoints are sufficient and
cheaper. See ADR-0001 (deterministic engine in a slim graph) and ADR-0005
(canonical schema + per-target projection).

## Decision

Adopt a **Universal Adapter architecture**: the [[Canonical Schema]] is a single
source of truth; per-destination **delivery agents** project it into each target's
format and transport it. The routing graph is deterministic — no LLM decides which
adapter runs; the client's profile (`delivery_target`) drives the route.

### Structure

```
Canonical Schema (NormalizedInvoice / BankStatement)
        │
        ▼
Delivery Router (deterministic, profile-driven)
        │
        ├── Excel/Slack adapter        ← implemented today
        ├── ERP REST API adapter       ← roadmap (Xero/QBO OAuth push)
        ├── Legacy import-file adapter ← roadmap (ADK Artifact Service)
        └── RPA adapter                ← roadmap (Daytona/CLI, on-prem only)
```

Each adapter is a `FunctionTool` registered in-process unless it must run on the
firm's own network (the RPA case), in which case A2A is acceptable. **No A2A for
adapters that run in Cloud Run.**

The ADK `Artifact Service` stores generated import files (`.iif`, `.csv`, `.txt`)
as transient artefacts during a session; the Slack delivery card provides the
download link. Files are not permanently archived (consistent with ADR-0002: Ledgr
storage is working/ephemeral; the client's accounting software is the record).

### Phased implementation order

| Phase | Adapter | Rationale |
|-------|---------|-----------|
| 1 | **Legacy import-file generator** | Highest stated pain; pure deterministic projection of the canonical schema; no external auth needed. Best first endpoint. |
| 2 | **Modern ERP API push** (Xero/QBO) | Removes re-entry entirely; needs per-firm OAuth + token storage in Firestore; live API error handling. |
| 3 | **RAG suggestion layer** | Fuzzy vendor→COA matching whose confirmed result becomes a [[Correction]] (see ADR-0004 roadmap amendment). Feeds all adapters. |
| 4 | **RPA route** | Daytona/CLI for legacy apps with no API and no batch import; highest complexity, lowest frequency. A2A justified only here. |

Each phase is its own plan and ADR when picked up.

## Consequences

- Adding a new delivery target requires only a new adapter `FunctionTool` and a
  profile field — the canonical schema and Engine are unchanged unless the new
  target requires a field no existing target did (per ADR-0005 completeness
  contract).
- Delivery is separated from understanding: the Engine produces one canonical
  result; delivery adapters consume it independently, in any combination.
- In-process `FunctionTool` adapters share the Cloud Run instance's memory and
  do not add network hops — appropriate for the current `max-instances=1`
  deployment.
- A2A is reserved for the RPA case (on-premises network boundary) and introduces
  no new dependencies until Phase 4.
- The `Artifact Service` path for import files keeps generated files out of Slack
  channel storage (consistent with ADR-0002) and scoped to the session lifetime.

## Alternatives considered

- **A2A for all adapters** — unnecessary network overhead for adapters that run
  in-process; adds latency and operational surface. Rejected except for the
  genuine on-prem boundary case.
- **Per-target extraction schemas** — rejected by ADR-0005; duplicated,
  divergent, wasteful.
- **Slack file upload for import files** — permanently stores generated import
  files in the firm's Slack channel, inflating storage and polluting the Files
  tab. The `Artifact Service` + session-scoped download link is the correct
  pattern.
- **ERP push first** — requires OAuth per firm before any firm can benefit;
  legacy import-file generation requires no auth and solves the same re-entry
  pain for the broadest set of clients first.
