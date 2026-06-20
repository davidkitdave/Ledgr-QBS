# Plan: Data-driven jurisdiction + MY SST correctness + multi-ERP exporters

Status: Approved (2026-06-20) — ready for execution · **fact-checked 2026-06-20** (4 parallel verify agents; corrections folded in — see inline VERIFIED markers + the Risk-controls footer)
Owner: lead (Opus) orchestrates; `executor` (Sonnet) implements
Branch: `feat/ledgr-my-sst-correctness` off `main` (PR3 merged at `171a6fe`)
Relates to: ADR-0017 (quality-gated HITL), ADR-0024 (cross-border auto-book), ADR-0001 (deterministic engine), ADR-0019 (universal adapter delivery)
Supersedes the scope of the local planning note `~/.claude/plans/look-for-more-details-shimmying-deer.md`.

> **De-hardcode the "assume Singapore" codebase.** One theme runs through every workstream:
> *stop assuming, source from the jurisdiction YAML, fail loud (HITL) when unknown — never silent-default.*

---

## Context

The bot raised a HITL "review the taxes" approval on *every* document (false-positive storm). Investigating that surfaced a deeper, systemic issue: the codebase silently assumes **Singapore / SGD / GST / tax-registered** in ~10 places. A Malaysian client is therefore taxed and exported as a Singapore client from day one. The user asked to (a) fix the MY SST tax data "based on control," (b) verify a NotebookLM flat-file-export note, and (c) zoom out and de-hardcode the codebase with an accountant's eye. A three-agent research pass (authoritative MY tax sources + ADK docs + a codebase audit) produced the findings below.

**Intended outcome:**
- HITL fires only when genuinely useful (real per-line doubt, unreconciled totals, partial-exempt reverse-charge) — not as a config-gap proxy.
- Region/currency/registration are **captured per client**, never assumed; unknown → HITL, not a silent SG default.
- Adding a country = a YAML drop + one registry row (no Python surgery).
- MY tax data is factually correct (the current file is wrong — see WS2).
- Malaysian ERPs (SQL Account, AutoCount) are supported as code-keyed, file-import targets.

**Research verdicts feeding this plan:**
- **MY SST data is factually wrong today** (WS2). Service Tax was **6% 2018-09-01→2024-02-29**, then **8% from 2024-03-01** with a **6% carve-out** track (F&B, telecom, parking, logistics) — **plus a 2025-07-01 scope expansion** (PU(A) 172/2025: new taxable services, same 6%/8% bands — see WS2). The file encodes flat 8% since 2018. Sales Tax 5%/10% bands are missing entirely.
- **Flat-file export (NotebookLM note):** keep writing export files **deterministically in Python** (`openpyxl` + `LedgerExporter`). Do **NOT** adopt ADK `EnvironmentToolset`/`LocalEnvironment`/`WriteFile`/`Execute` (real but experimental; routes audit-grade output through an LLM running shell commands). Reject its ISO-date advice (our importers want `DD/MM/YYYY`) and 50–100-row batch-splitting (not a real limit). Keep: `GcsArtifactService` ingestion (already our pattern) and the COA→Taxes→Contacts→Invoices import order (the accountant's job).
- **Jurisdiction must stay DETERMINISTIC** (a legal fact from the profile, a hard signal per ADR-0017). The data-driven registry is the right de-hardcoding — NOT an LLM choosing jurisdiction. The only LLM call is per-line tax (already in `tax_reasoning.py`).

---

## Audit — the 10 correctness bugs (all trace to "assume Singapore")

| # | File:line | Bug | Risk | Lands in |
|---|-----------|-----|------|----------|
| C1/C2 | `app/onboarding.py:105`, `invoice_processing/export/client_context.py:76` | region/currency silently default to `SINGAPORE`/`SGD` | MY firm taxed+exported as SG from day 1 | WS4 |
| C3 | `accounting_agents/nodes.py:637` | `tax_registered` defaults **True** | non-registered client gets SR on every line | WS4 |
| C5/C6 | `accounting_agents/tax_reasoning.py:155`, `my_sst.yaml` | MY 8% hardcoded in prompt + no Sales Tax bands | wrong rate, rate-guard rejects 5%/10% | WS2 |
| C4 | `invoice_processing/export/models.py:31` | `is_overseas` = "not Singapore" | MY client's SG suppliers read as domestic | WS1 |
| C8 | `accounting_agents/nodes.py:579` | direction defaults `purchase` | a sale mis-booked as a purchase (wrong P&L side) | WS1 |
| C9 | `invoice_processing/export/tax_classifier.py:210` | rate `%` hardcoded as keywords | future SG 10% breaks auto-classify, needs deploy | WS2 |
| C7 | `accounting_agents/assistant.py:176` | GST/SST thresholds as Python constants | law change = code deploy; move to YAML | WS1 |
| C10 | `accounting_agents/jurisdiction.py:258` | currency fallback ends `or "SGD"` | unknown-region client exports SGD | WS1 |

---

## Workstreams (ordered execution — each is its own commit)

> Characterization tests FIRST where refactoring existing behavior (SG/MY must resolve identically before/after). Suite baseline ~1849 passed / 6 known pre-existing failures — do not regress beyond those 6.

### WS0 — Live verify the over-flag cause (NO code)
Restart the Slack socket bot **and** `adk web` from HEAD (a stale instance runs old code and makes fixes look broken). Drop a clean **domestic** SG invoice and an **overseas-supplier** invoice. Read the approval-card reason strings — they already name the trigger (`not reconciled` / `flagged for tax review` / `low tax confidence (x < 0.7)`, `accounting_agents/nodes.py:1709-1725`). Decision gate: if clean domestic invoices no longer flag → the storm was stale code, and WS4's confidence-tuning shrinks; if they still flag → capture the reason and tune in WS4. Cross-check with `inspect_state` CLI **and** `adk web` (CLI masks profile-seed regressions).

### WS1 — Data-driven jurisdiction registry (was Phase 2)
Replace the hardcoded SG/MY `if/elif` in `resolve_jurisdiction` (`accounting_agents/jurisdiction.py:233-392`, YAML filenames inline in 4 places) with a `REGION_REGISTRY` (region → `{currency, yaml, tax_system, cross_border_flag_policy}`) sourced from the YAMLs in `invoice_processing/shared_libraries/`. `resolve_jurisdiction` becomes: normalize region → registry row → build `JurisdictionRule`. **Reuse** `_norm_region`/`_REGION_ALIASES`, `_current_standard_rate`, the YAML loader/cache — this is a consolidation, not a new system. Keep it a pure deterministic `@node`.
- Fold in: **C4** (`is_overseas` takes the client's home-country as a parameter, not a hardcoded SG list), **C7** (GST/SST thresholds → a `registration_threshold` key in the YAML), **C8** (missing direction → HITL flag, never assume `purchase`), **C10** (no terminal `or "SGD"` — `None` + explicit flag).
- Characterization test first: SG and MY must resolve identically to the pre-refactor branches.
- Payoff: adding Indonesia = drop `id_ppn.yaml` + one registry row; it then appears in the WS4 onboarding dropdown automatically. ADR-0024's cross-border policy becomes a per-region field.
- **Leave the two lanes (bank/commercial) as-is** — they already share the 5-node terminal spine via `lane_config.py`; only the extract step genuinely differs. Collapsing adds branching and breaks the clean `adk web` graph.

### WS2 — Malaysia SST correctness (the immediate fix)
Fix `invoice_processing/shared_libraries/my_sst.yaml` and de-hardcode the rate.

**Rate truth (VERIFIED 2026-06-20 against RMCD/MOF + EY/PwC/KPMG/BDO/Crowe/Grant Thornton — the 2018→2024 model AND the carve-out group letters all confirmed; the one real gap was the 2025-07-01 scope expansion (PU(A) 172/2025), now folded in as a third regime):**
```
rate_by_date / service_tax:
  # Regime 1 — flat 6%
  - { from: 2018-09-01, to: 2024-02-29, rate: 0.06, scope: all prescribed services }
  # Regime 2 — 8% standard + 6% carve-out (Rate of Tax (Amendment) Order 2024, gazetted 2024-02-26; rate keyed to date service PROVIDED; 6-mo grandfather to 2024-08-31)
  - { from: 2024-03-01, to: 2025-06-30, rate: 0.08, scope: standard services }
  - { from: 2024-03-01, to: 2025-06-30, rate: 0.06, scope: carve-out — F&B core prep (Grp B), telecom (Grp I), parking (Grp I), logistics (Grp J) }
  # Regime 3 — 2025-07-01 SST expansion (PU(A) 172/2025): SAME 6%/8% bands, EXPANDED taxable-service scope
  - { from: 2025-07-01, to: null, rate: 0.08, scope: standard + NEW: rental/leasing, fee/commission financial services }
  - { from: 2025-07-01, to: null, rate: 0.06, scope: carve-out + NEW: construction, private healthcare (non-citizens only), private education (>RM60k/student/yr) }
  # beauty services were proposed then WITHDRAWN 2025-06-27 — do NOT code as taxable
  # Grp B nuance: only core F&B prep is 6%; ancillary (alcohol, facility rental, entertainment) is 8%
sales_tax:            # NEW — currently missing entirely; bands UNCHANGED by 2025 expansion (only the taxable-goods scope broadened)
  - { rate: 0.10, scope: standard taxable goods — DEFAULT (anything not 0%/5%) }
  - { rate: 0.05, scope: essential/reduced goods }
  # petroleum specific-rate (per-unit, Second Schedule) out-of-scope for now
registration_thresholds:   # per-category — relevant to "should this vendor even charge tax"
  - { default: 500000, rental_or_leasing: 1000000, financial_services: 1000000, construction: 1500000, private_healthcare: 1500000 }
```
From 2024-03-01 there are **two concurrent service-tax rates** (8% standard + a 6% carve-out track); from 2025-07-01 the **same two bands** apply to an **expanded set of taxable services** — so `rate_by_date` must encode the band AND the document's service-category, and the Python rate-guard picks the rate from (date × category). Sales Tax is single-stage, non-recoverable (no input credit) — a distinct track; the 2025 expansion broadened which goods are taxable but did NOT change the 5%/10% bands.

**Imported taxable services (reverse charge)** = a distinct treatment/code (`IM`): 6% pre-2024-03, 8% after; reported SST-02 (registered) / SST-02A (non-registered). (VERIFIED 2026-06-20: Section 26A Service Tax Act 2018, in force since 2019-01-01.)

**De-hardcode (C5/C6/C9):**
- Delete the hardcoded `"The standard rate for MY is 8% SST"` prompt string (`accounting_agents/tax_reasoning.py:155`) — build the rate narrative from the YAML.
- Build the SR rate-keyword recognizers (`"8%"`, `"6%"`, `"9%"`…) from the YAML rate bands, not hardcoded (`invoice_processing/export/tax_classifier.py:210`; applies to SG too).

**Code maps — KEY CLARIFICATION:** today's `qbs` map is the user's OWN "QBS Ledger" app, **NOT** AutoCount/SQL Account (the inline comment conflates three systems). Add **real, separate** ERP code maps as DATA (consumed by WS6 exporters):
- **SQL Account = date-driven, one code per treatment** (system derives 6% vs 8% from doc date — structural model VERIFIED): service `SV` (std), `SVA` (**adjustment / credit-note, NOT a rate variant**), exempt `SVE`, imported `IMSV` (+`IMSVE` exempt); sales `ST5` (5%, confirmed). → simple `treatment → code` map. ⚠️ **Corrected 2026-06-20:** `SVZ` is **not a real SQL code** (likely a typo of `SVE` — dropped); and `ST`/`STE`/`SE` are **UNVERIFIED** — SQL's actual sales-exemption family is `SEA`/`SEB`/`SEC1-5` (+`SR0`/`ZRE`/`NTR`). Treat every bare-string guess as seed-only until the client file proves it.
- **AutoCount = rate-suffixed codes**: `SV-6`/`SV-8`, `IMSV-6`/`IMSV-8`, `S-5`/`S-10`, `ESV-6`/`ESV-8`. → code depends on the resolved RATE → `treatment → {rate → code}` map.
  - ⚠️ `PS-8`/`SVU-8`/`ESV-8` are **real AutoCount codes** (the 2024 8%-set, VERIFIED to exist), but a 2026-06-20 correction: **`SVU-8` = service tax on own-use/free (deemed supply), NOT imported** (imported = `IMSV-8`); `PS-8` = purchase-of-service (B2B-exempt input leg); `ESV-8` = exempted service. Exact description strings + SST-02 field mappings still need the AutoCount table / client file before that exporter ships to prod.
- **Structural consequence:** the `code_map` value type must support BOTH a flat string (qbs/xero/sql_acc) and a rate-keyed sub-map (autocount). A deterministic resolver computes the rate (from `rate_by_date` + carve-out) then looks up the code. NOT LLM.

### WS3 — Region capture + collapse jurisdiction HITL (was Phase 1)
**Region is the single anchor (resolved with user).** Capture region ONCE; everything else is *derived* from the WS1 registry, never re-asked: region `MALAYSIA` → currency `MYR` + tax system `SST` + ruleset `my_sst.yaml` + registration threshold + cross-border policy. Caveat: region sets the **home/reporting currency + which rules apply**; the *document's own* currency is still recorded as-shown (a MY client can receive a USD invoice — per the record-as-shown rule). So region → rules + home currency; document → its own currency.
- Add a region selector to the onboarding modal (`app/blocks.py onboarding_modal()`), parse into `ProfileInput` (`app/onboarding.py:9`), **dropdown sourced from the WS1 registry** so supported regions live in one place. Base currency is *derived* from region (not a separate manual field), with document currency recorded as-shown.
- Stop hardcoding `"region": "SINGAPORE"` / `"SGD"` (C1/C2 — `app/onboarding.py:105`, `client_context.py:76` and the repeated `or "SINGAPORE"`/`or "SGD"` fallbacks). Default SG only for explicit backward-compat; absent region on a NEW profile → fail loud.
- `tax_registered` defaults **`None` (unknown) → HITL**, never `True` (C3).
- **Collapse jurisdiction-level HITL to ONE doc-level ask**: when jurisdiction is genuinely AMBIGUOUS, raise a single "set this client's tax region" reason instead of force-flagging every line at `tax_confidence=0.5` (`tax_reasoning.py:298-312` + the per-line loop in `approval_gate` `nodes.py:1712`).
- (Evidence-driven, only if WS0 showed it) loosen the `< 0.8` per-line flag cliff (`tax_reasoning.py:362`) / `0.7` approval threshold (`nodes.py:213`); make `APPROVAL_CONFIDENCE_THRESHOLD` profile-readable, not a module constant.

### WS4 — Self-healing reconcile re-read (was Phase 3)
When `unreconciled` is the *only* tripped signal, allow ONE totals-targeted re-extract (existing `_reextract_with_hint`, `nodes.py:1402`) before escalating; re-run `detect_struggle`; proceed silently if it now reconciles, else escalate as today. Reuses `_run_reviewer_loop` (`REVIEW_MAX_REEXTRACTS=1`, `nodes.py:1356`) — no new node. Bank-statement running-balance mismatches stay silent for now (out of scope).

### WS5 — Multi-ERP exporters (was Phase 6) — code-keyed, installed-app (file-import) — **BUILD NOW (real AutoCount/SQL demand, 2026-06-20)**
Malaysian ERPs are **code-keyed (not name-keyed)** and **installed-app** — we integrate by **file-import templates** (both vendors also ship a local SDK/REST "Bridge", but it requires the app on-prem and imposes the *same* code-keying constraint, so it's out of scope — we generate the file). The import **Verify step blocks unresolved rows** (no silent create).

**Governing principle (resolved with user): the client's ERP is the authority.** We do NOT invent codes. Per AutoCount/SQL client, onboarding captures *their* actual master data; we mirror it and resolve against it. Our `my_sst.yaml` code maps are **seed defaults only** — a client-provided code list always wins.

- **Master-data intake at onboarding (new).** Reuse the existing COA-upload mechanism to also capture, per AutoCount/SQL client: (1) their **tax-code list**, (2) their **chart of accounts**, (3) their **creditor/supplier master** (name → creditor code). Stored per client in Firestore. These are configurable per company — capturing them is the only way the export can ever import cleanly (and it makes the UNVERIFIED AutoCount research codes moot).
- **Resolution engine — DETERMINISTIC, not vector/RAG (ADK-grounded, 2026-06-20).** ADK guidance: mapping text → an exact code that must already exist = a **deterministic `FunctionTool` lookup against the source of truth**, NOT RAG (RAG approximates → invalid imports; ADK also documents a single-tool-per-agent limit for retrieval tools — version-gated to Python ≤1.15.0 with 1.16.0+ workarounds, but it still argues against RAG here). Extend the EXISTING deterministic-first resolver (`categorizer.py:94`, already validates LLM output back to the COA) to ALL three code types: tax code, GL code, creditor code. The lone semantic step stays where it already is — the fuzzy account-category guess — always validated to an existing code. **Vector COA drops from a requirement to an optional COA-only future enhancement.**
- **Unmapped policy = Option C, hybrid (resolved with user).** Deliver the file with all resolved rows ready; **flag unmapped rows in the Slack card** ("3 rows need creditor codes before import"); offer to capture the missing code via chat → `learn_mapping` remembers it so the gap shrinks per client. Mapped rows import immediately; stragglers fixed in Slack (remembered) or in the ERP. Blank when unknown — **never guess a code.**
- **Data model:** add `vendor_code`/`creditor_code` to `EntityMemoryEntry` (`client_context.py:59` — today has `mapping_code` = GL account + `role`, but no vendor code; `canonical_party_name` yields a *name*, not a code). Carry through `PartyInfo`.
- **Exporters:** promote the per-class column layout (`LedgerExporter` subclasses, `exporters.py`) to a **declarative ERP profile (YAML) + one generic renderer**; build `AutoCountExporter` + `SqlAccountExporter` matching each ERP's **exact import template** (columns + file type), with the rate-aware tax-code resolver (AutoCount `SV-8` vs `SV-6` by date+category; SQL date-driven single code). File-export only — no MCP (no hosted cloud import API for these on-prem ERPs; their local SDK/REST needs the app installed on the integration host — out of scope; MCP is the Xero/QBO-Online subset). Deterministic Python writer (flat-file verdict).
- **Golden-file acceptance (critical) — ship-gate IS the file, not the vendor docs.** Obtain ONE real import file the client has **already successfully imported** into their AutoCount/SQL test company. Reverse-engineer the exact format from it and use it as the golden output fixture. Without a known-good file we are shipping blind into a Verify step that will reject it. The AutoCount strings now verify as *existing* codes (though research caught `SVU-8` mis-described — it's own-use/free, not imported) and the SQL Account map had a real defect (`SVZ` doesn't exist) — so the code set stays UNVERIFIED *as a complete, correctly-described, correctly-account-linked whole* until the client file proves it. Vendor docs and Big-4 advisories are *risk-reduction* inputs, not acceptance. The true acceptance test = importing our generated file into the client's ERP test company with **zero rejected rows** in Verify (lives outside our system — see Verification).

### WS6 — Chat-agent hygiene + de-rigidify (runs AFTER WS0–WS5) — **resolved with user 2026-06-20**
The user's read: the chat agent *feels* rigid and over-coded (one 2,621-LOC `accounting_agents/assistant.py` = a single `LlmAgent` "assistant" + **24 tools** (19 read-only + 5 mutating, of which only **4** are ADK-confirmation-gated) + a long MUST/MUST NOT `_BASE_INSTRUCTION`, ~46 inline defs; the `assistant_tools/` split was started and stalled). Verified **independently against the ADK docs** (not the ADR) — `adk.dev/agents/` + google-dev-knowledge corpus, 2026-06-20:

- **"Few lines" is the START shape, not production.** ADK: *"Building an agent with just a model, instructions, and tools is a great place to **start**... As your agent grows in capability and complexity, you are likely to want to break up the capabilities... and modularize your code."* The Google-demo "a few lines" agents are simple, high-freedom, and don't have to be *correct*. Two of ADK's four named reasons to grow apply here verbatim: **agent code modularity** and **mixing deterministic + non-deterministic tasks**.
- **Do NOT agentify the accounting math (guardrail).** ADK: *"interweave the non-deterministic functionality of AI models with deterministic code, **rather than relying on non-deterministic AI models to manage the full execution of a task**."* So the graph pipeline stays a graph pipeline, and the 23 numeric/data tools stay deterministic Python — they are the agent's *hands* (without them it would hallucinate financial numbers), NOT bloat. The rigidity to fix is **file layout + prompt**, not determinism.
- **Keep the safety prompt rules.** Match instruction freedom to fragility — *"Low freedom (specific scripts)... when operations are fragile... or a specific sequence MUST be followed"* (this exact phrasing is ADK's **Agent Skills best-practices** *Degrees of freedom* guidance, not the core agent-instruction page) + ADK's own human-oversight-for-high-risk guidance (`adk.dev/safety/`, `tools-custom/confirmation/`). The MUST/MUST NOT guards around the 5 state-mutating tools are correct-by-the-book — do not loosen them. **NB (verified 2026-06-20):** only **4** use `require_confirmation=True` (`amend_ledger_row`/`remove_ledger_row`/`replace_recorded_month`/`re_extract_document`); the 5th, `learn_mapping`, gates differently — it queues to `PENDING_LEARN_KEY` state, NOT ADK confirmation.

**Three cumulative directions (1 → 3 → 2), characterization-guarded by the `chat_eval/` suite — behavior preserved at each step:**
- **6a — File-hygiene refactor (Direction 1, biggest "feels clean" win, lowest risk).** Finish the stalled split: `assistant.py` → a thin `agent.py` (~150-LOC agent definition) + a `tools/` package grouped by concern (`read_tools.py`, `explain_tools.py`, `mutate_tools.py`) + the instruction in its own module. Pure reorganization, no behavior change. After this the agent definition itself *is* "a few lines" — the tools live in modules (ADK "agent code modularity").
- **6b — Prompt slim via tool design (Direction 3, "smarter, not longer").** Move **routing** rules out of `_BASE_INSTRUCTION` into precise per-tool descriptions/docstrings (ADK "guide tool use"); delete the routing MUST/MUST NOTs that exist only because two tools look alike. **Keep** the genuine **safety** constraints in instruction prose + deterministic code guards — ADK guidance is supplement, not replace (`adk.dev/agents/llm-agents/`: *"you should explain the purpose of each tool and the circumstances under which it should be called, **supplementing any descriptions within the tool itself**"*). The 5 mutating tools retain their MUST/MUST NOT guards and `require_confirmation=True` — this matches the existing `chat-readonly-until-step-4` design. Net: the agent talks less rigidly on routing; safety stays explicit.
- **6c — Read/write agent split (Direction 2 = the user's "B").** Split into two specialized agents under a thin root: a **read-only ledger-analyst** (the 19 query/explain tools) + a **ledger-corrections** agent (the 5 mutating tools), via `AgentTool`/LLM-transfer (ADK "grow from single agent → workflow" for modularity + instruction-following). Each agent gets a shorter, more cohesive instruction (fewer rules → less rigid), and the dangerous write surface is isolated behind its own agent. **The idiomatic ADK mechanism for this isolation is per-agent toolset scoping** (different `tools=` list per `LlmAgent` under the root), documented at `adk.dev/agents/custom-agents/#delegation` + the multi-agent `workflows/`/agent-team docs (**corrected 2026-06-20: NOT `adk.dev/agents/routing/`, which is the explicit-function `RoutedAgent` ADK explicitly contrasts with the LLM-driven delegation we want**) — the corrections-agent receives *only* the 5 mutating tools + 1-2 read helpers; the read-agent receives the 19 read-only tools. This also makes the Deferred-Phase-5 in-band trigger a smaller add later.

Sequencing decision (user, 2026-06-20): **WS6 runs after the SST/ERP correctness program (WS0–WS5)** — refactor on a stable base, not on top of in-flight correctness changes. No behavior change is the bar: any chat-eval regression blocks the step.

### Deferred — Phase 5 agentic chat (NOT in this push)
~80% already shipped (`accounting_agents/assistant.py`: **24 tools**, 19 read-only + 5 mutating — **4 behind `require_confirmation=True`**, `learn_mapping` via a `PENDING_LEARN_KEY` state queue; `FirestoreSessionService` makes ADK Tool Confirmation work). The lone net-new piece is an optional `AgentTool(document_workflow)` in-band conversational trigger. Orthogonal to this jurisdiction/ERP push — track separately; **WS6c (read/write split) shrinks this further** since the corrections agent is the natural place to host it.

---

## Verification

1. **pytest** — stay ≤6 known failures. Add cases: AMBIGUOUS → single doc-level ask (not N line flags); MY client → SST (not GST); MY service invoice dated 2024-03+ → 8% (carve-out category → 6%); MY goods at 5%/10% → rate-guard passes; unreconciled-only → one auto-retry then proceed/escalate; WS1 registry resolves SG/MY identically to the old branches (characterization); AutoCount resolver picks `SV-8` vs `SV-6` by date+category; unmapped vendor → HITL not silent.
2. **Live Slack QA on a fresh HEAD bot** (WS0 protocol): clean domestic SG invoice → no flag; overseas-supplier → auto-book (ADR-0024); a deliberately-MY client → SST codes + MYR; a deliberately-unbalanced invoice → one silent retry then a single clean escalation.
3. **adk web** cross-check for profile-seed/jurisdiction regressions the CLI masks (inspect the *resolved* tax/GL/creditor codes mid-pipeline before export).
4. **ERP-import acceptance (WS5, outside our system — the only test that proves importability):** import the generated AutoCount/SQL file into the **client's ERP test company**; pass = zero rows rejected by Verify. Anchor unit tests to a **golden known-good import file** from that client. Test pyramid: unit (synthetic client master-data fixtures) → adk web (resolution) → Slack (file generates + flags blanks) → real ERP import (Verify passes).
5. **Chat-agent refactor (WS6) — behavior-preserving bar:** run the `chat_eval/` suite as a characterization gate *before* 6a and after every sub-step; any regression blocks. The refactor changes structure, not answers — same questions → same tool calls → same answers. Live `adk web` + Slack chat spot-check after 6c (read/write split) to confirm tool selection still routes correctly (read vs mutate) and confirmation prompts still fire on the 5 mutating tools.

## Risk controls
Characterization tests before each refactor; tax-code correctness double-checked against the sourced research; **UNVERIFIED AutoCount strings must be confirmed before that exporter ships to prod**; keep authoring and the verifier pass in separate lanes (writer ≠ verifier context); each WS independently shippable as its own commit. Consider a short ADR per major decision (jurisdiction registry; MY SST rate truth; declarative ERP profiles) following the ADR-0024 pattern.

**Fact-check pass (2026-06-20 — 4 parallel verify agents: ADK docs · codebase audit · MY SST rates · ERP codes).** No fabrications across ~40 checkable claims (every cited file:line + ADK quote real). Corrections folded in: (1) **WS2** now models the **2025-07-01 SST expansion** (PU(A) 172/2025) as a third date regime — the 2018→2024 rates and carve-out group letters all verified against RMCD/MOF + Big-4; (2) **ERP codes** — SQL `SVZ` dropped (not a real code → `SVE`), AutoCount `SVU-8` re-described (own-use/free, *not* imported), `ST`/`STE`/`SE` flagged unverified, "no API" softened to installed-app; (3) **WS6** counts fixed to **24 tools / 4 confirmation-gated** (`learn_mapping` gates via a state queue, not ADK confirmation); (4) **WS6c** ADK citation corrected to `custom-agents/#delegation` + `workflows/` (not `routing/`); (5) `tax_reasoning.py` flag-cliff ref `:426`→`:362`. Still genuinely open (needs the client's real import file, per WS5 ship-gate): exact AutoCount/SQL code descriptions + account links.
