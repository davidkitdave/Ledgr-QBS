# Ledgr Testing Strategy — Two Surfaces

> Status: living doc. Created 2026-06-19 during an adk-web QA session.
> Supersedes the app-selection guidance in `adk-web-testing.md` (which assumed
> three separately-selectable apps — adk web only exposes one, see §1.2).

We test Ledgr on **two distinct surfaces**, for two distinct purposes. Keep them
separate — a failure on one does not imply a failure on the other, and fixing
one does not verify the other.

| | **Surface 1 — ADK / agents-cli playground** | **Surface 2 — Slack interface** |
|---|---|---|
| **Purpose** | Build & test the *agent system* — routing, extraction reasoning, tax/jurisdiction logic, HITL gating, node order | Test the *integration & delivery layer* — Slack file intake, Block Kit cards, approval buttons, threads, OAuth, artifact save |
| **Entry point** | `adk web` (browser) · `playground_runner.py` (CLI) · `scratch/inspect_state.py` (state dump) · `agents-cli eval` (chat) · `pytest` (doc lane) | Local Socket-Mode bot · live Slack workspace QA |
| **Unit under test** | `coordinator_graph` / `document_workflow` / `assistant_app` and the `invoice_processing` engine | `slack_runner.py` + the agent system behind it |
| **What it CANNOT test** | Real Slack rendering, button payloads, OAuth, multi-workspace install | Whether the agent *reasoning* is correct (too slow/expensive to iterate here) |
| **Iterate here when…** | Changing nodes, prompts, extractors, tax rules, routing | Changing delivery cards, HITL buttons, thread wiring, onboarding |

**Golden rule:** prove agent correctness on Surface 1 first (fast, inspectable),
then verify the Slack wrapper on Surface 2. Never debug agent logic by reading
Slack messages — use the playground.

---

## 1. Surface 1 — ADK / agents-cli playground

### 1.0 The #1 discipline rule: RESTART after any code change

`adk web` (and any long-running runner) **imports your Python modules once at
startup and holds them in memory.** If you edit `nodes.py`, `agent.py`, etc.
*after* the server started, the server keeps running the **old bytecode**. Your
QA results will reflect stale code.

> This bit us on 2026-06-19: an "agent crashes on upload" finding turned out to
> be a stale server — the live code already had the fix. Always:
>
> ```bash
> pkill -f "adk web accounting_agents"      # kill the old server
> # then relaunch (see 1.1)
> ```
>
> This is the adk-web equivalent of the "restart the Slack bot before QA" rule.

### 1.1 Launch

```bash
cd /Users/davidkitdave/Projects/Ledgr-QBS
set -a; source .env; set +a            # GOOGLE_API_KEY (dev = AI Studio, not Vertex)
export LEDGR_ENV=dev
export LEDGR_PLAYGROUND_PROFILE_PATH=playground_profile.json
uv run adk web accounting_agents --port 8080
# open http://127.0.0.1:8080
```

### 1.2 Only ONE app is selectable — `accounting_agents`

adk web discovers apps by the `root_agent` exported from the agent directory, so
it shows **only `accounting_agents`** (the `coordinator_graph`). The
`accounting_agents_document` and `accounting_agents_assistant` apps referenced in
older docs are **not** separately selectable in the UI. Consequences:

- To test the **document lane** in adk web, you go *through* the coordinator
  (upload a PDF → it classifies intent `document` → routes into the pipeline).
- To test the **chat assistant** in isolation, use `playground_runner.py --chat`
  (the chat app is a separate `App` not exposed to adk web).

### 1.3 Uploading a document

The "+" button in the message bar opens a native file chooser. When driving the
UI programmatically, target the hidden `<input type=file>` directly. Files must
live inside the workspace root (copy test docs into `scratch/qa_docs/`).

### 1.4 Match the client profile to the document

`playground_profile.json` seeds the client profile (currently **Malaysia / JBI
Plus Auto / MYR**). The tax/jurisdiction result depends on this profile, so:

> **A Singapore document tested under the Malaysia profile is treated as
> CROSS_BORDER** (Malaysia client buying from an SG supplier → import /
> reverse-charge → flagged for HITL). That is *correct reasoning for a mismatched
> profile*, but it is not what you want when QA-ing an SG document.

Set the region to match the document before testing:

```bash
export LEDGR_PLAYGROUND_REGION=SINGAPORE   # or MALAYSIA
export LEDGR_PLAYGROUND_CURRENCY=SGD       # or MYR
```

…or edit `playground_profile.json`, then **restart adk web** (1.0).

### 1.5 Reading the run — the four tabs

- **Traces** — *ground truth* for execution order. Blue rows are events; click one
  to open Event / Request / Response / Graph / State-Changes panels.
- **Graph** — the static topology. A `⚠️ [NO DEFAULT]` marker on `dynamic_router`
  / `classify_node` is expected (the router has no fall-through edge — see the
  architecture note). The doc pipeline renders as
  `extract → review → categorize → resolve_jurisdiction → tax → approval_gate →
  apply_decision → route → consolidate → deliver`.
- **Events** — per-step `state_delta`. Each node shows the keys it wrote
  (e.g. `tax_node` → `tax_jurisdiction, tax_system_hint, jurisdiction_rates`).
- **State** — the cumulative session state. Use it to confirm `tax_jurisdiction`,
  `account_code`, per-line `tax_treatment`, currency.

**HITL works in adk web.** A flagged document pauses at `approval_gate` and
renders an approval form (Decision = approve/edit/reject, optional Edits, Submit).
You can drive the human-in-the-loop loop entirely in the browser.

### 1.6 Faster, scriptable variants of Surface 1

| Tool | Use when |
|---|---|
| `uv run python -m accounting_agents.playground_runner --pdf <path> --region … --currency …` | Drive one PDF to local Excel without a browser. Note: pauses at HITL (no resume flag yet). |
| `uv run python scratch/inspect_state.py <pdf> <REGION> <CCY> "<client name>"` | Dump the **full pre-gate state** (jurisdiction, per-line tax_treatment, account_code) for any PDF. Best for tax-logic QA. |
| `agents-cli eval generate && agents-cli eval grade` | Regression-grade the **chat** eval cases (`tests/eval/datasets/ledgr.evalset.json`). |
| `pytest tests/eval/test_f_extract_direction.py tests/eval/test_soa_cover_skip_sample_vendor.py` | Regression-grade the **document lane** (hermetic + real-PDF integration). |

> Eval split: ADK eval cases carry a **text prompt**, not a PDF — they test the
> *chat* assistant. Document-pipeline correctness is tested in **pytest**. Do not
> try to assert extraction correctness from an ADK eval case.

---

## 2. Surface 2 — Slack interface

### 2.0 Discipline rule: restart the bot before QA

Same principle as 1.0 — a stale long-running bot serves old code. Always
kill + relaunch the Socket-Mode bot from HEAD before a live QA pass.

### 2.1 What this surface tests (and only this surface)
- File intake from a Slack `file_share` event → artifact save → workflow trigger
- The delivery card (counts, ledger pointer, Block Kit data table, xlsx upload)
- HITL **buttons** (Approve / Edit / Reject) and their payload round-trip
- Thread context, per-channel client profile, onboarding / COA upload
- OAuth install (Model B, multi-workspace) and per-workspace token handling

### 2.2 Setup
- Dev app = `Ledgr (dev)`, Socket Mode, QBS-AI workspace only (`LEDGR_ENV` unset/dev).
- See `two-slack-apps-dev-prod` memory and `manifest-dev.json`.

### 2.3 What to verify on Slack that Surface 1 cannot
- Card renders correctly (no Block Kit overflow, table formatting intact)
- Approve button produces the **same** rich delivery card as the clean path
- COA `.xlsx` upload is accepted when channel is in `pending_coa` status
- The xlsx artifact downloads and opens

---

## 3. Test document matrix (both surfaces)

| # | Doc | Profile to seed | Expected |
|---|---|---|---|
| 1 | SG purchase invoice (9% GST) | SG / SGD | `tax_jurisdiction: SINGAPORE`, SR, no cross-border flag |
| 2 | SG telco (SR + ZR split) | SG / SGD | two lines SR + ZR, `SINGAPORE` |
| 3 | MY receipt (8% SST) | MY / MYR | `MALAYSIA`, SST, `account_code` populated |
| 4 | MY ← AU/SG supplier | MY / MYR | `CROSS_BORDER`, OS, flagged → HITL |
| 5 | SG bank statement | SG / SGD | bank lane, `tax_node` does NOT run |
| 6 | SOA cover + invoices | either | cover page skipped, no phantom rows |

Sample docs live at `~/Desktop/LocalTest/TestDoc/` (copy into `scratch/qa_docs/`
for browser upload). See the local test-data memory.

---

## 4. Known gaps surfaced during QA (2026-06-19)

1. **`resolve_jurisdiction_node` and `tax_node` both compute jurisdiction and can
   disagree** — verified: resolve wrote `MALAYSIA`, tax_node overwrote to
   `CROSS_BORDER` on the same `tax_jurisdiction` key. Centralise jurisdiction in
   one place (QA-plan item: "replace hardcoded if/else in tax_node with
   `resolve_jurisdiction`").
2. **Coordinator is a 3-way classifier, not a conversational front desk** — it
   forces every turn into `document` / `question` / `unknown` and routes
   `unknown` to a canned help message. See the architecture note for the
   recommended ADK pattern (LLM-driven delegation).
3. **`playground_runner.py` cannot resume past HITL** (`--auto-approve` is in the
   help text but unimplemented), so flagged docs can't be driven to a booked
   result from the CLI. adk web *can* (it renders the approval form).
4. **`account_code` is empty until a COA is ingested** — by design; COA-from-upload
   is the open Step-10 work.
