> **Archived 2026-07-01** — Describes the removed `accounting_agents` / `invoice_processing` graph and factory. **Live runtime:** `ledgr_slack` + `ledgr_agent` ([ADR-0032](../0032-ledgr-agent-and-slack-two-packages.md)). History only; do not implement against this doc.

# ADR-0023: Two-lane eval architecture — chat via agents-cli, document pipeline via pytest

> **Superseded by [ADR-0033](0033-reference-free-ledgr-agent-eval.md)** (2026-06-29).
> Single-lane `ledgr_agent` eval; chat lane archived per ADR-0032.

Status: Superseded (2026-06-29; was Accepted 2026-06-19)
Builds on ADR-0015 (eval-driven prompt loop), ADR-0008 / ADR-0021 (chat lane vs document
Workflow are separate ADK surfaces).

## Context

A QA review asked whether Ledgr's evaluation setup is "built right or wrong." The system
has **two distinct runtime surfaces** (ADR-0008/0021): a conversational chat agent
(`accounting_agents/chat_eval`, an `LlmAgent`) and a document pipeline
(`accounting_agents.agent`, a graph `Workflow` driven by `slack_runner.process_file_event`
with a PDF — not a chat turn). agents-cli / ADK eval tooling
(`eval generate` → `eval grade` → `compare`/`analyze`/`optimize`) is designed for
**single-agent conversational inference**: each eval case carries a text prompt, not a file.

### What we verified (evidence, not assumptions)

1. **The evalset schema is the correct current ADK `EvalSet` format**, not a legacy or
   homegrown one. `tests/eval/datasets/ledgr.evalset.json` uses top-level `eval_cases` with
   `eval_id` + `session_input` (`app_name`/`user_id`/`state`) + `conversation` of
   `Invocation`s (`user_content`, `final_response`, `intermediate_data.tool_uses`),
   including multi-turn. This matches the ADK `EvalCase` Pydantic model.
2. **The chat lane already runs through real agents-cli tooling and performs well.**
   `scripts/ledgr_eval_chat.sh` → `eval/ledgr_eval_generate.py` (ADK inference with seeded
   session state) → `agents-cli eval grade`. Live run 2026-06-19: 6/6 chat cases (B3–B8)
   scored `chat_tool_trajectory_match = 1.0` and `custom_response_quality = 5.0`, 0 errors.
3. **The document lane is, and must be, pytest** — it cannot go through `agents-cli eval
   generate` because the pipeline is a graph driven by a PDF, not a single agent answering a
   prompt. `_select_case_ids(lane="doc")` already raises with that explanation. Doc behaviour
   is covered by `tests/eval/test_eval_golden.py` (`AgentEvaluator`), the offline F-cluster
   gate `tests/eval/test_f_extract_direction.py` (deterministic extraction/direction/tax
   scoring, no LLM), and `process_file_event` integration tests
   (`test_ws2_ws3.py`, `test_pipeline.py`, `test_concurrency.py`).
4. **One real defect:** chat-case selection was a hardcoded 4-tuple (`CHAT_CASE_IDS`
   = B3–B6), so newer chat cases **B7/B8 were silently never generated or graded** — and any
   future B-case would drift the same way. Fixed: selection now derives every B-prefixed id
   from the evalset (commit `ab24499`).

So the answer to "are we doing it wrong" is: **the schema and the lane split are correct;
the confusion came from one stale case list and from the architecture being undocumented.**

## Decision

Adopt and document an explicit **two-lane eval architecture**:

- **Chat lane (cluster B) → agents-cli.** `EvalSet` cases with a text prompt + seeded
  `session_input.state`, run through `ledgr_eval_generate.py` (ADK inference) and graded by
  `agents-cli eval grade` using `tests/eval/eval_config_chat.yaml`. This lane is where
  `eval compare` / `eval analyze` / `eval optimize` apply. Chat cases are selected by
  deriving B-prefixed ids from the evalset — never a hardcoded list.

- **Document lane (clusters A, C–F) → pytest.** Graded by `AgentEvaluator`
  (`tests/eval/test_eval_golden.py`, config `eval_config.yaml`) and the offline F-cluster
  scorers (`eval_config_adr0015.yaml` + `tests/eval/custom_metrics.py`), plus
  `process_file_event` integration tests. This lane does **not** use `agents-cli eval
  generate`; that is by design, not a gap.

The canonical, operational description of both lanes lives in `tests/eval/README.md`.

## Consequences

- New chat scenarios are added as `B*` cases in the evalset and are picked up automatically.
- New document scenarios are added as evalset `A/C–F` cases (graded via pytest) and/or
  `process_file_event` integration tests — not as agents-cli eval cases.
- Known follow-ups (not blocking): the chat lane has two configs (`eval_config.yaml` for the
  pytest/`AgentEvaluator` path, `eval_config_chat.yaml` for the agents-cli path) that serve
  two different runners; `eval_config.yaml` carries a couple of unused entries
  (`response_match_score` criterion, `agent_turn_count` metric). These are documented in the
  README rather than merged, to avoid breaking the pytest gate. Coverage can grow with more
  MY-jurisdiction, SOA, and credit-note chat cases (the document lane already has
  `F11_credit_note_sales` and `F12_MY_receipt`).
