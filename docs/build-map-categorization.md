# Build Map — COA Categorization, Per-Client Context & Learning (on top of the existing ADK agent)

Grounded in the ADK docs (adk.dev: Sessions/State, Memory, Tools, Callbacks). The goal: make the
agent categorize each line to **the client's own chart of accounts** and learn per client, without
hardcoding any client's account numbers — multi-tenant by construction.

## What already exists (Layer 0)
- `root_agent` (`LlmAgent`) + `run_inference` pipeline (Acting → Investigation → ALF) — extraction works.
- `export/` — tax classifier (SG GST SR/ZR/ES/OS) + Ledger exporters (QBS Ledger / Xero Ledger),
  verified. The exporter fills `Account Code / COA` from each line's `account_code` — currently blank.

The missing link is **resolving each line to the client's account code**, fed by per-client context,
with corrections that persist (learning). Below maps each need to an ADK primitive.

## ADK primitive → our use

| Need | ADK primitive | How we use it |
|---|---|---|
| Inject a client's COA / Category_Mapping / Entity_Memory / flags so the agent can categorize | **Session `state`** (`session.state`, dict of serializable values) | At session start, load the client's Client Setup into `state` (e.g. `state["coa"]`, `state["category_mapping"]`, `state["entity_memory"]`, `state["tax_registered"]`, `state["software"]`, `state["base_currency"]`). Tools read it via `tool_context.state`. |
| Load that context automatically when a document arrives | **`before_agent_callback`** (CallbackContext.state) | Callback resolves the client (from the Slack workspace/channel routing), reads the per-client store, and populates `state`. State changes via `callback_context.state[...]` are auto-tracked. |
| Make the prompt client-aware (region, GST-registered, target software) | **Instruction `{key}` templating** | `instruction="...Region: {region}. GST-registered: {tax_registered}. Target ledger: {software}..."` — ADK injects state values before calling the model. |
| The categorization itself (vendor→account, line→account) | **FunctionTool** + `ToolContext.state` | A `resolve_account(line_description, vendor_name, ...)` tool: (1) Entity_Memory hit → return remembered account+tax (deterministic); (2) else LLM → standard Category → client code via `Category_Mapping`; (3) else match against COA "AI Search Keywords"; (4) confidence + flag. All client data read from `tool_context.state`. No hardcoded codes. |
| Persist a correction so it's reused next time (learning) | **Structured per-client store** (Firestore/GCS) loaded into state — **not** Memory Bank | A `remember_entity(vendor, account_code, tax_code, ...)` tool writes to the client's Entity_Memory store; the next session's loader reads it back into `state`. Corrections come from the Slack ✏️ flow. |
| (Optional, later) semantic recall of past free-text decisions | **`MemoryService` / Memory Bank** (`VertexAiMemoryBankService`, `--memory_service_uri`) | Memory Bank extracts *meaning from conversations* — good for "how did we treat X before" recall, NOT for the structured vendor→account table. Keep it as a secondary aid; the source of truth for codes is Entity_Memory. |
| Persist state/sessions across turns (multi-tenant) | **`VertexAiSessionService`** (Agent Platform Sessions, already chosen) | Session/state persists; tenant isolation via `user_id = team_id:slack_user` + per-client store keyed by client id. |

> Multi-tenancy note: ADK's built-in state prefixes are `app:` (all users), `user:` (one user_id),
> `temp:` (one invocation), or none (one session). None of these is exactly "per client", so we do
> **not** rely on prefixes for tenancy — the callback loads the *correct client's* config into session
> state based on the workspace→client mapping. The persistent Entity_Memory lives in a per-client store.

## The mapping model (recap, now ADK-wired)
Two layers keep the AI client-agnostic:
1. **AI → universal Category** (telco_internet, utilities, professional_fees, …) — same for every client.
2. **Category → that client's account code** via the client's `Category_Mapping` (in `state`).
Plus **Entity_Memory** (vendor→account+tax, deterministic) and **COA keyword match** for client-specific
accounts. A brand-new client just supplies their Client Setup; if `Category_Mapping` is unmapped, a
one-time **bootstrap** proposes category→account for accountant confirmation.

## Build steps (incremental, on top of Layer 0)

1. **Client Setup loader** — read a client's COA / Category_Mapping / Entity_Memory / Sys_Config from
   the Client Setup workbook (local dev) or Firestore/GCS (prod) into a typed object. Inject into
   `session.state` via a `before_agent_callback`. *(new: `export/client_context.py`)*
2. **`resolve_account` FunctionTool** — deterministic-first (Entity_Memory) → category→code → COA
   keyword match → confidence/flag. Reads `tool_context.state`. *(new: `export/categorizer.py`)*
3. **Wire into the pipeline** — after extraction, run `resolve_account` per line to fill
   `InvoiceLine.account_code`; tax classifier conditioned on `state["tax_registered"]`; exporter writes
   the Ledger per `state["software"]`.
4. **`remember_entity` tool + correction flow** — accountant correction (Slack ✏️) writes to the
   client's Entity_Memory store; loader picks it up next session. (Reuse the ALF safety-loop idea to
   vet a correction before committing.)
5. **Instruction templating** — add `{region}`, `{tax_registered}`, `{software}` to the agent
   instruction so it stays client-aware.
6. **Bootstrap mapping** (new-client onboarding) — when `Category_Mapping` is empty, propose
   category→account from the COA for one-time accountant confirmation.

## Eval hook
Each step is graded by the `agents-cli eval` loop (task #9): account-code accuracy is a metric, graded
against the verified `Ledger_FY` files; iterate to ≥0.9.

Sources: adk.dev — Sessions/State, Memory (`MemoryService`/Memory Bank), Tools (FunctionTool/ToolContext),
Callbacks. See also [[ledgr-data-model]] in memory and docs/research/sg-gst-tax-codes.md.
