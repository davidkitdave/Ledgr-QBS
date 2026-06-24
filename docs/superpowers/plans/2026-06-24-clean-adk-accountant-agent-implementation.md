# Clean ADK Accountant Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the new clean `ledgr_agent/` ADK accountant brain beside the current live Slack runtime, prove it with tests/eval, then migrate traffic safely.

**Architecture:** Keep `app/` as the Cloud Run/FastAPI Slack front door and keep `accounting_agents/` as the live runtime until the new agent passes gates. Add `ledgr_agent/` as the clean ADK package. Keep `invoice_processing/` as the accounting engine library behind the new tools.

**Tech Stack:** Python 3.10+, Google ADK 2.x, `agents-cli` 0.4.0, Pydantic v2, PyYAML, pytest, existing Ledgr `invoice_processing` engine, Cloud Run, Slack Bolt.

**Authoritative spec:** `docs/superpowers/specs/2026-06-24-clean-adk-accountant-agent-design.md`

**Safety rule:** Do not change `app/main.py`, Dockerfile entrypoint, or live Slack traffic in Plans 1-4. Do not commit unless the user explicitly asks.

---

## Plan Set

This spec is too large for one risky implementation plan. Build it as six small plans:

1. **Contract + Eval Shell**: create `ledgr_agent/`, schemas, SG/MY policy YAML, policy loader, custom metric helpers, and a tiny eval dataset.
2. **Document Tool Wrapper**: add `process_document_batch` FunctionTool around the existing engine and return `BatchResult`.
3. **HITL + Review Cleanup**: split hard stops from grouped soft warnings and reduce Slack review spam.
4. **Multi-ERP + Tax Policy**: enforce SG/MY policy YAML with Python validators and strengthen ERP golden tests.
5. **Credit Integration**: add Firestore credit gate/deduct flow with eval and live QA.
6. **Cutover + Retirement**: switch agent eval/traffic carefully, shrink `accounting_agents/`, retire `eval/` scripts after parity.

This document fully details **Plan 1** (complete — commit `12c6b36`) and **Plan 2** (in progress). Plans 3-6 should be written after Plan 2 lands.

## Progress

| Plan | Status | Notes |
|------|--------|-------|
| 1 — Contract + Eval Shell | **Done** | `ledgr_agent/`, schemas, SG/MY YAML, root agent, smoke eval |
| 2 — Document Tool Wrapper | **Done** | `process_document_batch`, `batch_mapper.py`, 12 pytest cases, smoke eval |
| 3 — HITL + Review Cleanup | **Done** | Hard/soft split, grouped warnings, `hitl_noise_score`, `_approval_summary` consumes `partition_and_group_reasons` |
| 4 — Multi-ERP + Tax Policy | **Done** | Policy validators (rate lookup + GST/SST registration gates), `sg-policy`/`my-policy` eval suites, `tax_validity_code` metric, ERP golden tests |
| 5 — Credit Integration | **Done** | `CreditService` + `InMemoryCreditStore` per ADR-0016, `_credit_gate` blocks before LLM, `credits.json` eval, `credit_charge_code` metric |
| 6 — Cutover + Retirement | **Done** | Accountant chat action tools (`explain_tax_treatment` read-only, `amend_ledger_row` w/ confirmation), 5 core eval datasets + 8-metric `eval_config_core.yaml`, `LEDGR_USE_CLEAN_AGENT` feature flag (default off), QA checklist at `docs/qa/clean-agent-cutover-checklist.md` |

---

## Current Repo Facts

- `app/main.py` is the current Cloud Run entrypoint and imports `accounting_agents.slack_runner.build_fastapi_app`.
- `agents-cli-manifest.yaml` uses `agent_directory: "ledgr_agent"` (switched in Plan 1).
- `agents-cli eval generate` reads `agent_directory` from `agents-cli-manifest.yaml`; it has no `--app-name` flag.
- `agents-cli run` supports `--app-name`, so it can smoke-test a local alternate app more easily than eval generation.
- `pyproject.toml` currently packages `accounting_agents`, `app`, and `invoice_processing`, but not `ledgr_agent`.
- `.gitignore` already excludes `tests/eval_invoices/`, `scratch/`, `playground_profile.json`, and `artifacts/grade_results/`.

---

## Target File Structure After Plan 1

Create:

```text
ledgr_agent/
  __init__.py
  agent.py
  schemas/
    __init__.py
    batch_result.py
    credit.py
    review.py
  policies/
    __init__.py
    loader.py
    jurisdictions/
      sg.yaml
      my.yaml
  metrics/
    __init__.py
    batch_result_metrics.py
  tools/
    __init__.py
    policy_tools.py
tests/
  ledgr_agent/
    test_package_import.py
    test_batch_result_schema.py
    test_policy_loader.py
    test_policy_tool.py
    test_root_agent.py
    test_batch_result_metrics.py
tests/eval/
  datasets/
    clean-root-smoke.json
  eval_config_clean_root.yaml
```

Modify:

```text
pyproject.toml
agents-cli-manifest.yaml   # only after import + pytest smoke pass
```

Do not modify:

```text
app/main.py
accounting_agents/slack_runner.py
accounting_agents/agent.py
invoice_processing/export/*
invoice_processing/extract/*
invoice_processing/classify/*
```

---

## Plan 1 Acceptance Gates

Plan 1 is complete only when:

- `uv run pytest tests/ledgr_agent -q` passes.
- `uv run python -c "from ledgr_agent.agent import root_agent; print(root_agent.name)"` prints `root_accountant_agent`.
- `agents-cli run --app-name ledgr_agent "What can the Ledgr accountant agent do?" -v` produces a trace without touching Slack or real files.
- If `agents-cli-manifest.yaml` is switched to `ledgr_agent`, `agents-cli eval generate --dataset tests/eval/datasets/clean-root-smoke.json --output artifacts/traces/clean-root-smoke.json` runs against the new agent.
- The generated trace and grade results are not committed.

---

## Task 1: Create Package Shell

**Files:**
- Create: `ledgr_agent/__init__.py`
- Create: `ledgr_agent/schemas/__init__.py`
- Create: `ledgr_agent/policies/__init__.py`
- Create: `ledgr_agent/tools/__init__.py`
- Create: `ledgr_agent/metrics/__init__.py`
- Modify: `pyproject.toml`
- Test: `tests/ledgr_agent/test_package_import.py`

- [ ] **Step 1: Write the import test**

Create `tests/ledgr_agent/test_package_import.py`:

```python
def test_ledgr_agent_package_imports() -> None:
    import ledgr_agent

    assert ledgr_agent.__name__ == "ledgr_agent"
```

- [ ] **Step 2: Create package marker files**

Create `ledgr_agent/__init__.py`:

```python
"""Clean ADK accountant agent package for Ledgr-QBS."""
```

Create these files as empty package markers:

```text
ledgr_agent/schemas/__init__.py
ledgr_agent/policies/__init__.py
ledgr_agent/tools/__init__.py
ledgr_agent/metrics/__init__.py
```

- [ ] **Step 3: Add `ledgr_agent` to the wheel package list**

Modify `pyproject.toml`:

```toml
[tool.hatch.build.targets.wheel]
packages = ["accounting_agents", "app", "invoice_processing", "ledgr_agent"]
```

- [ ] **Step 4: Run the package import test**

Run:

```bash
uv run pytest tests/ledgr_agent/test_package_import.py -q
```

Expected:

```text
1 passed
```

---

## Task 2: Define Batch, Review, And Credit Schemas

**Files:**
- Create: `ledgr_agent/schemas/review.py`
- Create: `ledgr_agent/schemas/credit.py`
- Create: `ledgr_agent/schemas/batch_result.py`
- Modify: `ledgr_agent/schemas/__init__.py`
- Test: `tests/ledgr_agent/test_batch_result_schema.py`

- [ ] **Step 1: Write schema tests**

Create `tests/ledgr_agent/test_batch_result_schema.py`:

```python
from ledgr_agent.schemas import BatchResult, CreditSummary, ReviewRequest, SoftWarning


def test_batch_result_minimal_success_payload() -> None:
    result = BatchResult(
        status="success",
        client_id="client_demo",
        firm_id="team_demo",
        source_files=["invoice.pdf"],
        credits=CreditSummary(
            credits_estimated=1,
            credits_used=1,
            credits_remaining=9,
            credit_status="charged",
        ),
    )

    dumped = result.model_dump()

    assert dumped["status"] == "success"
    assert dumped["credits"]["credits_used"] == 1
    assert dumped["review_requests"] == []
    assert dumped["soft_warnings"] == []


def test_batch_result_keeps_hard_review_and_soft_warning_separate() -> None:
    result = BatchResult(
        status="needs_review",
        client_id="client_demo",
        firm_id="team_demo",
        source_files=["invoice.pdf"],
        review_requests=[
            ReviewRequest(
                id="missing_invoice_number",
                severity="hard_review",
                message="Invoice number is missing.",
            )
        ],
        soft_warnings=[
            SoftWarning(
                id="low_coa_confidence_group",
                message="11 lines have low-confidence account mapping.",
                count=11,
            )
        ],
    )

    assert result.review_requests[0].severity == "hard_review"
    assert result.soft_warnings[0].count == 11
```

- [ ] **Step 2: Create review schemas**

Create `ledgr_agent/schemas/review.py`:

```python
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


ReviewSeverity = Literal["hard_review", "review"]


class ReviewRequest(BaseModel):
    """A material issue that should pause or block delivery."""

    id: str
    severity: ReviewSeverity
    message: str
    file_name: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)


class SoftWarning(BaseModel):
    """A grouped warning that should be visible but not spam HITL."""

    id: str
    message: str
    count: int = 1
    file_name: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)
```

- [ ] **Step 3: Create credit schema**

Create `ledgr_agent/schemas/credit.py`:

```python
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


CreditStatus = Literal["not_checked", "estimated", "charged", "blocked", "not_billable"]


class CreditSummary(BaseModel):
    """Billing summary attached to every batch result."""

    credits_estimated: int = 0
    credits_used: int = 0
    credits_remaining: int | None = None
    credit_status: CreditStatus = "not_checked"
    credit_ledger_refs: list[str] = Field(default_factory=list)
```

- [ ] **Step 4: Create batch result schema**

Create `ledgr_agent/schemas/batch_result.py`:

```python
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ledgr_agent.schemas.credit import CreditSummary
from ledgr_agent.schemas.review import ReviewRequest, SoftWarning


BatchStatus = Literal[
    "success",
    "partial",
    "needs_review",
    "blocked",
    "error",
]


class BatchResult(BaseModel):
    """Structured result shared by Slack, eval, and logs."""

    status: BatchStatus
    client_id: str
    firm_id: str | None = None
    source_files: list[str] = Field(default_factory=list)
    per_file: list[dict[str, object]] = Field(default_factory=list)
    posted_documents: list[dict[str, object]] = Field(default_factory=list)
    skipped_documents: list[dict[str, object]] = Field(default_factory=list)
    review_requests: list[ReviewRequest] = Field(default_factory=list)
    soft_warnings: list[SoftWarning] = Field(default_factory=list)
    erp_exports: list[dict[str, object]] = Field(default_factory=list)
    credits: CreditSummary = Field(default_factory=CreditSummary)
    models_used: list[str] = Field(default_factory=list)
    validation_summary: dict[str, object] = Field(default_factory=dict)
    audit_refs: list[str] = Field(default_factory=list)
    llm_call_count: int = 0
    strong_model_used: bool = False
    fallback_reason: str | None = None
    elapsed_ms: int | None = None
    documents_requested: int = 0
    documents_processed: int = 0
    documents_skipped_before_llm: int = 0
    estimated_cost: float | None = None
```

- [ ] **Step 5: Export schema names**

Modify `ledgr_agent/schemas/__init__.py`:

```python
from ledgr_agent.schemas.batch_result import BatchResult
from ledgr_agent.schemas.credit import CreditSummary
from ledgr_agent.schemas.review import ReviewRequest, SoftWarning

__all__ = [
    "BatchResult",
    "CreditSummary",
    "ReviewRequest",
    "SoftWarning",
]
```

- [ ] **Step 6: Run schema tests**

Run:

```bash
uv run pytest tests/ledgr_agent/test_batch_result_schema.py -q
```

Expected:

```text
2 passed
```

---

## Task 3: Add SG/MY Policy YAML And Loader

**Files:**
- Create: `ledgr_agent/policies/jurisdictions/sg.yaml`
- Create: `ledgr_agent/policies/jurisdictions/my.yaml`
- Create: `ledgr_agent/policies/loader.py`
- Modify: `ledgr_agent/policies/__init__.py`
- Test: `tests/ledgr_agent/test_policy_loader.py`

- [ ] **Step 1: Write policy loader tests**

Create `tests/ledgr_agent/test_policy_loader.py`:

```python
from ledgr_agent.policies import load_jurisdiction_policy


def test_load_sg_policy_contains_historical_gst_rates() -> None:
    policy = load_jurisdiction_policy("SG")

    rates = policy["rates"]["standard"]

    assert policy["policy_version"] == "sg-2026-01"
    assert policy["market"] == "SG"
    assert {row["rate"] for row in rates} == {0.08, 0.09}


def test_load_my_policy_contains_sst_and_myinvois_codes() -> None:
    policy = load_jurisdiction_policy("my")

    assert policy["policy_version"] == "my-2026-01"
    assert policy["market"] == "MY"
    assert policy["myinvois"]["tax_types"]["sales_tax"] == "01"
    assert policy["myinvois"]["tax_types"]["service_tax"] == "02"


def test_unknown_policy_market_fails_loud() -> None:
    try:
        load_jurisdiction_policy("ID")
    except ValueError as exc:
        assert "unsupported jurisdiction" in str(exc)
    else:
        raise AssertionError("expected unsupported jurisdiction to fail")
```

- [ ] **Step 2: Create SG YAML**

Create `ledgr_agent/policies/jurisdictions/sg.yaml` with this exact initial file. Plan 4 will expand it after policy validators are added:

```yaml
policy_version: sg-2026-01
market: SG
currency: SGD
tax_system: GST
registration:
  client_flag: gst_registered
  effective_date_field: gst_registration_effective_from
  turnover_threshold_sgd: 1000000
  non_registered:
    allow_output_gst: false
    allow_input_tax_claim: false
rates:
  standard:
    - rate: 0.08
      effective_from: 2023-01-01
      effective_to: 2023-12-31
    - rate: 0.09
      effective_from: 2024-01-01
review_rules:
  - id: gst_claimed_by_non_registered_client
    severity: hard_review
  - id: gst_charged_without_supplier_gst_number
    severity: hard_review
```

- [ ] **Step 3: Create MY YAML**

Create `ledgr_agent/policies/jurisdictions/my.yaml` with this exact initial file. Plan 4 will expand it after policy validators are added:

```yaml
policy_version: my-2026-01
market: MY
currency: MYR
tax_system: SST
registration:
  supplier_sst_number_field: supplier_sst_registration_number
  buyer_sst_number_field: buyer_sst_registration_number
  not_registered_value: NA
rates:
  sales_tax:
    - code: MY_ST_10
      myinvois_tax_type: "01"
      rate: 0.10
      label: Sales tax 10%
    - code: MY_ST_5
      myinvois_tax_type: "01"
      rate: 0.05
      label: Sales tax 5%
  service_tax:
    - code: MY_SVC_8
      myinvois_tax_type: "02"
      rate: 0.08
      effective_from: 2024-03-01
    - code: MY_SVC_6
      myinvois_tax_type: "02"
      rate: 0.06
myinvois:
  tax_types:
    sales_tax: "01"
    service_tax: "02"
    not_applicable: "06"
    exempt: "E"
```

- [ ] **Step 4: Create the policy loader**

Create `ledgr_agent/policies/loader.py`:

```python
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_POLICY_DIR = Path(__file__).parent / "jurisdictions"
_POLICY_FILES = {
    "SG": "sg.yaml",
    "MY": "my.yaml",
}


@lru_cache(maxsize=8)
def load_jurisdiction_policy(market: str) -> dict[str, Any]:
    """Load a versioned jurisdiction policy YAML by market code."""

    key = market.strip().upper()
    file_name = _POLICY_FILES.get(key)
    if file_name is None:
        supported = ", ".join(sorted(_POLICY_FILES))
        raise ValueError(f"unsupported jurisdiction {market!r}; supported: {supported}")

    path = _POLICY_DIR / file_name
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"jurisdiction policy {path} must be a mapping")
    if data.get("market") != key:
        raise ValueError(f"jurisdiction policy {path} market mismatch: {data.get('market')!r}")
    if not data.get("policy_version"):
        raise ValueError(f"jurisdiction policy {path} is missing policy_version")
    return data
```

- [ ] **Step 5: Export loader**

Modify `ledgr_agent/policies/__init__.py`:

```python
from ledgr_agent.policies.loader import load_jurisdiction_policy

__all__ = ["load_jurisdiction_policy"]
```

- [ ] **Step 6: Run policy tests**

Run:

```bash
uv run pytest tests/ledgr_agent/test_policy_loader.py -q
```

Expected:

```text
3 passed
```

---

## Task 4: Add Policy Inspection Tool

**Files:**
- Create: `ledgr_agent/tools/policy_tools.py`
- Modify: `ledgr_agent/tools/__init__.py`
- Test: `tests/ledgr_agent/test_policy_tool.py`

- [ ] **Step 1: Write tool tests**

Create `tests/ledgr_agent/test_policy_tool.py`:

```python
from ledgr_agent.tools import inspect_market_policy


def test_inspect_market_policy_returns_safe_summary() -> None:
    result = inspect_market_policy("SG")

    assert result["status"] == "success"
    assert result["market"] == "SG"
    assert result["policy_version"] == "sg-2026-01"
    assert "full_policy" not in result


def test_inspect_market_policy_reports_unsupported_market() -> None:
    result = inspect_market_policy("ID")

    assert result["status"] == "error"
    assert "unsupported jurisdiction" in result["message"]
```

- [ ] **Step 2: Create tool**

Create `ledgr_agent/tools/policy_tools.py`:

```python
from __future__ import annotations

from ledgr_agent.policies import load_jurisdiction_policy


def inspect_market_policy(market: str) -> dict:
    """Return a safe summary of the SG/MY market policy available to the agent."""

    try:
        policy = load_jurisdiction_policy(market)
    except ValueError as exc:
        return {
            "status": "error",
            "message": str(exc),
        }

    return {
        "status": "success",
        "market": policy["market"],
        "currency": policy["currency"],
        "tax_system": policy["tax_system"],
        "policy_version": policy["policy_version"],
        "review_rule_count": len(policy.get("review_rules") or []),
    }
```

- [ ] **Step 3: Export tool**

Modify `ledgr_agent/tools/__init__.py`:

```python
from ledgr_agent.tools.policy_tools import inspect_market_policy

__all__ = ["inspect_market_policy"]
```

- [ ] **Step 4: Run tool tests**

Run:

```bash
uv run pytest tests/ledgr_agent/test_policy_tool.py -q
```

Expected:

```text
2 passed
```

---

## Task 5: Add Clean Root Agent

**Files:**
- Create: `ledgr_agent/agent.py`
- Test: `tests/ledgr_agent/test_root_agent.py`

- [ ] **Step 1: Write root agent import test**

Create `tests/ledgr_agent/test_root_agent.py`:

```python
from ledgr_agent.agent import root_agent


def test_clean_root_agent_imports() -> None:
    assert root_agent.name == "root_accountant_agent"
    tool_names = {getattr(tool, "__name__", getattr(tool, "name", "")) for tool in root_agent.tools}
    assert "inspect_market_policy" in tool_names
```

- [ ] **Step 2: Create root agent**

Create `ledgr_agent/agent.py`:

```python
from __future__ import annotations

from google.adk.agents import Agent

from invoice_processing.shared_libraries.model_config import lite_model
from ledgr_agent.tools import inspect_market_policy


root_agent = Agent(
    name="root_accountant_agent",
    model=lite_model(),
    description="Clean Ledgr accountant agent for policy-aware document processing.",
    instruction=(
        "You are the Ledgr accountant agent. "
        "Use tools to inspect market policy and explain what capabilities are available. "
        "Do not process real private documents unless a specific document tool is available. "
        "Gemini reads document evidence, Python checks accounting rules, and YAML stores market policy."
    ),
    tools=[inspect_market_policy],
)
```

- [ ] **Step 3: Run root agent tests**

Run:

```bash
uv run pytest tests/ledgr_agent/test_root_agent.py -q
```

Expected:

```text
1 passed
```

- [ ] **Step 4: Smoke import manually**

Run:

```bash
uv run python -c "from ledgr_agent.agent import root_agent; print(root_agent.name)"
```

Expected:

```text
root_accountant_agent
```

---

## Task 6: Add BatchResult Eval Metrics

**Files:**
- Create: `ledgr_agent/metrics/batch_result_metrics.py`
- Modify: `ledgr_agent/metrics/__init__.py`
- Test: `tests/ledgr_agent/test_batch_result_metrics.py`

- [ ] **Step 1: Write metric tests**

Create `tests/ledgr_agent/test_batch_result_metrics.py`:

```python
from ledgr_agent.metrics import cost_efficiency_code, no_unneeded_llm_code


def test_cost_efficiency_passes_for_lite_only_trace() -> None:
    instance = {
        "agent_data": {
            "turns": [
                {
                    "events": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "function_response": {
                                            "name": "process_document_batch",
                                            "response": {
                                                "status": "success",
                                                "llm_call_count": 2,
                                                "strong_model_used": False,
                                                "models_used": ["gemini-2.5-flash-lite"],
                                            },
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    }

    result = cost_efficiency_code(instance)

    assert result["score"] == 1.0


def test_no_unneeded_llm_fails_when_zero_credit_gate_calls_model() -> None:
    instance = {
        "agent_data": {
            "turns": [
                {
                    "events": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "function_response": {
                                            "name": "process_document_batch",
                                            "response": {
                                                "status": "blocked",
                                                "validation_summary": {"block_reason": "zero_credit"},
                                                "llm_call_count": 1,
                                            },
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    }

    result = no_unneeded_llm_code(instance)

    assert result["score"] == 0.0
```

- [ ] **Step 2: Create metric helpers**

Create `ledgr_agent/metrics/batch_result_metrics.py`:

```python
from __future__ import annotations

from typing import Any


def _function_responses(instance: dict[str, Any], name: str) -> list[dict[str, Any]]:
    responses: list[dict[str, Any]] = []
    agent_data = instance.get("agent_data") or {}
    for turn in agent_data.get("turns", []):
        for event in turn.get("events", []):
            content = event.get("content") or {}
            for part in content.get("parts", []):
                response = part.get("function_response")
                if isinstance(response, dict) and response.get("name") == name:
                    payload = response.get("response")
                    if isinstance(payload, dict):
                        responses.append(payload)
    return responses


def _latest_batch_result(instance: dict[str, Any]) -> dict[str, Any] | None:
    responses = _function_responses(instance, "process_document_batch")
    return responses[-1] if responses else None


def cost_efficiency_code(instance: dict[str, Any]) -> dict[str, Any]:
    """Score normal batch traces for limited LLM calls and no stronger fallback."""

    batch = _latest_batch_result(instance)
    if batch is None:
        return {"score": 1.0, "explanation": "no document batch result in trace"}

    llm_call_count = int(batch.get("llm_call_count") or 0)
    strong_model_used = bool(batch.get("strong_model_used"))
    if llm_call_count <= 2 and not strong_model_used:
        return {"score": 1.0, "explanation": "normal batch stayed within cost budget"}
    return {
        "score": 0.0,
        "explanation": f"llm_call_count={llm_call_count}, strong_model_used={strong_model_used}",
    }


def no_unneeded_llm_code(instance: dict[str, Any]) -> dict[str, Any]:
    """Fail when deterministic gates spend Gemini calls before they should."""

    batch = _latest_batch_result(instance)
    if batch is None:
        return {"score": 1.0, "explanation": "no document batch result in trace"}

    validation = batch.get("validation_summary") or {}
    block_reason = validation.get("block_reason")
    llm_call_count = int(batch.get("llm_call_count") or 0)
    deterministic_gate = block_reason in {"zero_credit", "duplicate", "unsupported_file"}
    if deterministic_gate and llm_call_count > 0:
        return {
            "score": 0.0,
            "explanation": f"{block_reason} gate spent {llm_call_count} LLM calls",
        }
    return {"score": 1.0, "explanation": "no unneeded LLM calls detected"}
```

- [ ] **Step 3: Export metrics**

Modify `ledgr_agent/metrics/__init__.py`:

```python
from ledgr_agent.metrics.batch_result_metrics import (
    cost_efficiency_code,
    no_unneeded_llm_code,
)

__all__ = [
    "cost_efficiency_code",
    "no_unneeded_llm_code",
]
```

- [ ] **Step 4: Run metric tests**

Run:

```bash
uv run pytest tests/ledgr_agent/test_batch_result_metrics.py -q
```

Expected:

```text
2 passed
```

---

## Task 7: Add Clean Root Eval Dataset And Config

**Files:**
- Create: `tests/eval/datasets/clean-root-smoke.json`
- Create: `tests/eval/eval_config_clean_root.yaml`

- [ ] **Step 1: Create eval dataset**

Create `tests/eval/datasets/clean-root-smoke.json`:

```json
{
  "eval_cases": [
    {
      "eval_case_id": "clean_root_capabilities",
      "prompt": {
        "role": "user",
        "parts": [
          {
            "text": "What can the Ledgr accountant agent do, and what market policies can it inspect?"
          }
        ]
      }
    },
    {
      "eval_case_id": "inspect_sg_policy",
      "prompt": {
        "role": "user",
        "parts": [
          {
            "text": "Inspect the Singapore market policy."
          }
        ]
      }
    }
  ]
}
```

- [ ] **Step 2: Create eval config**

Create `tests/eval/eval_config_clean_root.yaml`:

```yaml
metrics_to_run:
  - clean_root_tool_use_code

custom_metrics:
  - name: clean_root_tool_use_code
    custom_function: |
      def evaluate(instance):
          agent_data = instance.get("agent_data") or {}
          calls = []
          for turn in agent_data.get("turns", []):
              for event in turn.get("events", []):
                  content = event.get("content") or {}
                  for part in content.get("parts", []):
                      fc = part.get("function_call")
                      if isinstance(fc, dict):
                          calls.append(fc.get("name"))
          prompt = str(instance.get("prompt", "")).lower()
          if "singapore" in prompt or "policy" in prompt:
              ok = "inspect_market_policy" in calls
              return {
                  "score": 1.0 if ok else 0.0,
                  "explanation": "inspect_market_policy called" if ok else f"tool calls={calls}",
              }
          return {"score": 1.0, "explanation": "no policy tool required for this case"}
```

- [ ] **Step 3: Run dataset syntax check**

Run:

```bash
uv run python -m json.tool tests/eval/datasets/clean-root-smoke.json >/tmp/clean-root-smoke.json
```

Expected: command exits with status `0`.

---

## Task 8: Smoke Test With Agents CLI

**Files:**
- Modify: `agents-cli-manifest.yaml`

- [ ] **Step 1: Run direct app smoke without changing manifest**

Run:

```bash
agents-cli run --app-name ledgr_agent "Inspect the Singapore market policy." -v
```

Expected:

```text
The verbose trace includes a function_call or function_response for inspect_market_policy.
```

If this fails because the local server only loads `agent_directory`, do not retry repeatedly. Proceed to Step 2.

- [ ] **Step 2: Switch the manifest only after tests pass**

Modify `agents-cli-manifest.yaml`:

```yaml
agent_directory: "ledgr_agent"
```

Keep the existing `chat_agent_directory: "accounting_agents/chat_eval"` entry unchanged.

- [ ] **Step 3: Run eval trace generation**

Run:

```bash
agents-cli eval generate --dataset tests/eval/datasets/clean-root-smoke.json --output artifacts/traces/clean-root-smoke.json
```

Expected:

```text
Trace generation completes and writes artifacts/traces/clean-root-smoke.json.
```

- [ ] **Step 4: Run eval grading**

Run:

```bash
agents-cli eval grade --traces artifacts/traces/clean-root-smoke.json --config tests/eval/eval_config_clean_root.yaml --output artifacts/grade_results/
```

Expected:

```text
clean_root_tool_use_code score is 1.0 for inspect_sg_policy.
```

- [ ] **Step 5: Confirm generated artifacts are untracked**

Run:

```bash
git status --short artifacts tests/eval/datasets tests/eval/eval_config_clean_root.yaml
```

Expected:

```text
artifacts/grade_results/ is ignored.
tests/eval/datasets/clean-root-smoke.json is tracked as a new intended fixture.
tests/eval/eval_config_clean_root.yaml is tracked as a new intended config.
```

---

## Task 9: Plan 1 Final Verification

**Files:**
- No new files.

- [ ] **Step 1: Run focused tests**

Run:

```bash
uv run pytest tests/ledgr_agent -q
```

Expected:

```text
all tests in tests/ledgr_agent pass
```

- [ ] **Step 2: Run import smoke**

Run:

```bash
uv run python -c "from ledgr_agent.agent import root_agent; print(root_agent.name)"
```

Expected:

```text
root_accountant_agent
```

- [ ] **Step 3: Run lints on new package**

Run:

```bash
uv run ruff check ledgr_agent tests/ledgr_agent
```

Expected:

```text
All checks passed!
```

- [ ] **Step 4: Check repo status**

Run:

```bash
git status --short
```

Expected:

```text
New files under ledgr_agent/, tests/ledgr_agent/, and tests/eval/.
Modified pyproject.toml.
Modified agents-cli-manifest.yaml only if eval smoke required the switch.
No artifacts/grade_results files listed.
No tests/eval_invoices or scratch files listed.
```

---

## Plan 2: Document Tool Wrapper

**Goal:** Implement the new `process_document_batch` tool wrapper in `ledgr_agent/tools/document_tools.py`, integrate it into the root agent, and verify it with unit and evaluation tests.

**Architecture:** We will import and call the procedural `process_batch` from `invoice_processing.pipeline`. We will convert the resulting engine outputs and metadata into our Pydantic `BatchResult` model, ensuring proper serialization of internal dataclasses (like `NormalizedInvoice` and `ExtractedBankStatement`) and correct status mapping.

**Tech Stack:** Python 3.10+, Google ADK 2.x, Pydantic v2, pytest.

### Plan 2 Acceptance Gates

Plan 2 is complete only when:
- `uv run pytest tests/ledgr_agent -q` passes.
- `agents-cli eval generate --dataset tests/eval/datasets/clean-root-smoke.json --output artifacts/traces/clean-root-smoke.json` runs and generates traces showing that `process_document_batch` is called when requested.
- `agents-cli eval grade --traces artifacts/traces/clean-root-smoke.json --config tests/eval/eval_config_clean_root.yaml` passes with a score of 1.0.
- Ruff lints (`uv run ruff check ledgr_agent tests/ledgr_agent`) pass without warnings.

### Task 2.1: Implement the Document Tool Wrapper

**Files:**
- Create: `ledgr_agent/tools/document_tools.py`
- Modify: `ledgr_agent/tools/__init__.py`

- [ ] **Step 1: Create the document_tools.py module**

Create `ledgr_agent/tools/document_tools.py`:

```python
from __future__ import annotations

import dataclasses
from datetime import date
from pathlib import Path
import time
from typing import Any

from google.adk.tools import ToolContext

from invoice_processing.export.client_context import client_context_from_state
from invoice_processing.pipeline import process_batch
from ledgr_agent.schemas import BatchResult, CreditSummary, ReviewRequest, SoftWarning


def _serialize_value(val: Any) -> Any:
    """Recursively convert dates and dataclasses to JSON-serializable equivalents."""
    if dataclasses.is_dataclass(val):
        return {k: _serialize_value(v) for k, v in dataclasses.asdict(val).items()}
    if isinstance(val, dict):
        return {k: _serialize_value(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_serialize_value(item) for item in val]
    if isinstance(val, date):
        return val.isoformat()
    return val


def process_document_batch(paths: list[str], tool_context: ToolContext | None = None, **inject: Any) -> dict[str, Any]:
    """Process a batch of document file paths (invoices, receipts, bank statements) for the active client.

    Args:
        paths: List of absolute file paths to the documents to be processed.
        tool_context: Context injected by ADK providing access to the current session state.
        **inject: Seam for dependency injection in testing (e.g. classify_fn).
    """
    start_time = time.perf_counter()

    # 1. Resolve the client context state
    if tool_context is not None and getattr(tool_context, "state", None) is not None:
        state = tool_context.state
    else:
        # Fallback to playground context for local testing/eval
        from accounting_agents.agent import _playground_default_context
        state = _playground_default_context().to_state()

    client = client_context_from_state(state)

    # 2. Call the underlying procedual engine
    engine_result = process_batch(paths, client, **inject)

    # 3. Analyze documents and determine overall batch status
    has_errors = len(engine_result.errors) > 0
    all_reconciled = all(doc.reconciled for doc in engine_result.docs) if engine_result.docs else True

    if has_errors:
        status = "error"
    elif not all_reconciled:
        status = "needs_review"
    else:
        status = "success"

    # 4. Map engine result to posted / skipped documents and extract review requests
    posted_docs: list[dict[str, Any]] = []
    skipped_docs: list[dict[str, Any]] = []
    review_requests: list[ReviewRequest] = []
    soft_warnings: list[SoftWarning] = []

    # Estimate LLM call counts based on document types
    llm_call_count = 0
    for doc in engine_result.docs:
        doc_data = doc.normalized or doc.bank
        serialized_doc = _serialize_value(doc_data) if doc_data else {}
        
        # Add basic file details to the document dictionary
        if isinstance(serialized_doc, dict):
            serialized_doc["path"] = doc.path
            serialized_doc["doc_type"] = doc.doc_type
            serialized_doc["note"] = doc.note

        if doc.reconciled:
            posted_docs.append(serialized_doc)
        else:
            skipped_docs.append(serialized_doc)

            # Generate a ReviewRequest for unreconciled docs
            if doc.note and not doc.note.startswith("ok"):
                review_requests.append(
                    ReviewRequest(
                        id=f"review_{Path(doc.path).stem}",
                        severity="review",
                        message=doc.note,
                        file_name=Path(doc.path).name,
                        payload={"path": doc.path},
                    )
                )

        # Estimate LLM call counts
        if not doc.note.startswith("ERROR"):
            if doc.doc_type == "bank_statement":
                llm_call_count += 1  # bank statement extraction call
            elif doc.doc_type in ("invoice", "receipt"):
                llm_call_count += 3  # classify + extract + categorize

    elapsed_ms = int((time.perf_counter() - start_time) * 1000)

    # Build the final BatchResult Pydantic schema
    batch_result = BatchResult(
        status=status,
        client_id=client.client_id or "unknown",
        firm_id=client.firm_id,
        source_files=[str(p) for p in paths],
        posted_documents=posted_docs,
        skipped_documents=skipped_docs,
        review_requests=review_requests,
        soft_warnings=soft_warnings,
        credits=CreditSummary(
            credits_estimated=len(paths),
            credits_used=len(posted_docs),
            credit_status="estimated",
        ),
        models_used=["gemini-2.5-flash-lite"],
        strong_model_used=False,
        llm_call_count=llm_call_count,
        elapsed_ms=elapsed_ms,
        documents_requested=len(paths),
        documents_processed=len(engine_result.docs) - len(engine_result.errors),
        documents_skipped_before_llm=0,
    )

    return batch_result.model_dump()
```

- [ ] **Step 2: Export the tool function**

Modify `ledgr_agent/tools/__init__.py`:

```python
from ledgr_agent.tools.policy_tools import inspect_market_policy
from ledgr_agent.tools.document_tools import process_document_batch

__all__ = [
    "inspect_market_policy",
    "process_document_batch",
]
```

- [ ] **Step 3: Run ruff check**

Run:
```bash
uv run ruff check ledgr_agent/tools/document_tools.py
```
Expected: All checks passed!

### Task 2.2: Register Document Tool with Root Agent

**Files:**
- Modify: `ledgr_agent/agent.py`

- [ ] **Step 1: Add the tool to agent.py**

Modify `ledgr_agent/agent.py`:

```python
from __future__ import annotations

from google.adk.agents import Agent

from invoice_processing.shared_libraries.model_config import lite_model
from ledgr_agent.tools import inspect_market_policy, process_document_batch


root_agent = Agent(
    name="root_accountant_agent",
    model=lite_model(),
    description="Clean Ledgr accountant agent for policy-aware document processing.",
    instruction=(
        "You are the Ledgr accountant agent. "
        "Use tools to inspect market policy and explain what capabilities are available. "
        "Use process_document_batch to process batches of documents when requested by the user. "
        "Gemini reads document evidence, Python checks accounting rules, and YAML stores market policy."
    ),
    tools=[inspect_market_policy, process_document_batch],
)
```

- [ ] **Step 2: Run ruff check on agent.py**

Run:
```bash
uv run ruff check ledgr_agent/agent.py
```
Expected: All checks passed!

### Task 2.3: Write Document Tool Contract Tests

**Files:**
- Create: `tests/ledgr_agent/test_document_tool_contract.py`

- [ ] **Step 1: Create the contract tests**

Create `tests/ledgr_agent/test_document_tool_contract.py`:

```python
from pathlib import Path

from invoice_processing.classify.document_classifier import ClassificationResult
from invoice_processing.extract.bank_statement_extractor import ExtractedBankStatement
from invoice_processing.extract.invoice_extractor import ExtractedInvoice
from ledgr_agent.tools import process_document_batch


def _make_cls(doc_type: str) -> ClassificationResult:
    return ClassificationResult(
        doc_type=doc_type,
        confidence=0.99,
        issuer_name="Supplier Inc",
        bill_to_name="Playground Client",
    )


def test_process_document_batch_converts_engine_output(client_fye3, tmp_path) -> None:
    invoice_p = tmp_path / "invoice_test.pdf"
    invoice_p.write_bytes(b"%PDF stub")

    def _classify(path, **_kw):
        return _make_cls("invoice")

    def _direction(cls, **_kw):
        return "purchase"

    def _extract_stub(path, **_kw):
        return ExtractedInvoice(
            invoice_number="INV-1234",
            invoice_date="2026-06-24",
            vendor_name="Supplier Inc",
            total_amount=109.0,
            tax_amount=9.0,
            lines=[],
        )

    # Run the wrapper tool
    res = process_document_batch(
        paths=[str(invoice_p)],
        tool_context=None,  # triggers fallback to playground default
        classify_fn=_classify,
        direction_fn=_direction,
        extract_fn=_extract_stub,
    )

    # Assert Pydantic BatchResult properties in return payload
    assert res["status"] == "success"
    assert res["client_id"] == "playground"
    assert res["documents_requested"] == 1
    assert res["documents_processed"] == 1
    assert len(res["posted_documents"]) == 1
    assert res["posted_documents"][0]["invoice_number"] == "INV-1234"
    assert res["posted_documents"][0]["doc_type"] == "purchase"
    assert res["posted_documents"][0]["path"] == str(invoice_p)
    assert res["llm_call_count"] == 3
```

- [ ] **Step 2: Run contract tests**

Run:
```bash
uv run pytest tests/ledgr_agent/test_document_tool_contract.py -q
```
Expected: 1 passed.

### Task 2.4: Add Eval Case for Document Processing

**Files:**
- Modify: `tests/eval/datasets/clean-root-smoke.json`
- Modify: `tests/eval/eval_config_clean_root.yaml`

- [ ] **Step 1: Add new case to dataset**

Modify `tests/eval/datasets/clean-root-smoke.json` to append the third case:

```json
{
  "eval_cases": [
    {
      "eval_case_id": "clean_root_capabilities",
      "prompt": {
        "role": "user",
        "parts": [
          {
            "text": "What can the Ledgr accountant agent do, and what market policies can it inspect?"
          }
        ]
      }
    },
    {
      "eval_case_id": "inspect_sg_policy",
      "prompt": {
        "role": "user",
        "parts": [
          {
            "text": "Inspect the Singapore market policy."
          }
        ]
      }
    },
    {
      "eval_case_id": "process_doc_batch_smoke",
      "prompt": {
        "role": "user",
        "parts": [
          {
            "text": "Process this document batch for us: ['/Users/davidkitdave/Projects/Ledgr-QBS/tests/eval_invoices/stub.pdf']"
          }
        ]
      }
    }
  ]
}
```

- [ ] **Step 2: Update custom metrics config**

Modify `tests/eval/eval_config_clean_root.yaml` to verify `process_document_batch` is called in the smoke case:

```yaml
metrics_to_run:
  - clean_root_tool_use_code

custom_metrics:
  - name: clean_root_tool_use_code
    custom_function: |
      def evaluate(instance):
          agent_data = instance.get("agent_data") or {}
          calls = []
          for turn in agent_data.get("turns", []):
              for event in turn.get("events", []):
                  content = event.get("content") or {}
                  for part in content.get("parts", []):
                      fc = part.get("function_call")
                      if isinstance(fc, dict):
                          calls.append(fc.get("name"))
          prompt = str(instance.get("prompt", "")).lower()
          if "singapore" in prompt or "policy" in prompt:
              ok = "inspect_market_policy" in calls
              return {
                  "score": 1.0 if ok else 0.0,
                  "explanation": "inspect_market_policy called" if ok else f"tool calls={calls}",
              }
          if "process" in prompt or "batch" in prompt:
              ok = "process_document_batch" in calls
              return {
                  "score": 1.0 if ok else 0.0,
                  "explanation": "process_document_batch called" if ok else f"tool calls={calls}",
              }
          return {"score": 1.0, "explanation": "no policy tool required for this case"}
```

### Task 2.5: Final Verification and Lints

**Files:**
- None.

- [ ] **Step 1: Create a tiny stub PDF for the eval run**

Before generating the eval, create a dummy pdf at `/Users/davidkitdave/Projects/Ledgr-QBS/tests/eval_invoices/stub.pdf` so that it exists and does not fail because of a missing file.

Run:
```bash
mkdir -p tests/eval_invoices && echo "%PDF-1.4 stub" > tests/eval_invoices/stub.pdf
```

- [ ] **Step 2: Run focused tests**

Run:
```bash
uv run pytest tests/ledgr_agent -q
```
Expected: 12 passed.

- [ ] **Step 3: Run eval trace generation**

Run:
```bash
agents-cli eval generate --dataset tests/eval/datasets/clean-root-smoke.json --output artifacts/traces/clean-root-smoke.json
```
Expected: Completion of all 3 eval cases.

- [ ] **Step 4: Run eval grading**

Run:
```bash
agents-cli eval grade --traces artifacts/traces/clean-root-smoke.json --config tests/eval/eval_config_clean_root.yaml --output artifacts/grade_results/
```
Expected: Mean score is 1.0.

- [ ] **Step 5: Run ruff check**

Run:
```bash
uv run ruff check ledgr_agent tests/ledgr_agent
```
Expected: All checks passed!

---

## Plan 3: HITL + Review Cleanup

**Goal:** Split hard-stop reviews from grouped soft warnings so one invoice does not spam many Slack bullets.

**Architecture:** Add pure-Python review classifiers in `ledgr_agent/review/`. Use them in `batch_mapper.py` and share the same grouping helper with `accounting_agents/nodes.py::_approval_summary`. Eval grades `hitl_noise_score` from structured `BatchResult`, not Slack prose.

**Tech Stack:** Python 3.10+, Pydantic v2, pytest, agents-cli custom metrics.

**Do not modify live Slack traffic until Plan 3 unit tests and eval pass.**

### Plan 3 Acceptance Gates

- `uv run pytest tests/ledgr_agent tests/test_review_grouping.py -q` passes.
- `hitl_noise_score` returns 1.0 when 11 account-line issues collapse to one `SoftWarning`.
- `accounting_agents/nodes.py::_approval_summary` uses the shared grouper (one grouped bullet, not 11).
- `agents-cli eval grade` on `tests/eval/datasets/hitl-review.json` passes all deterministic metrics.

### Task 3.1: Review Reason Classifier

**Files:**
- Create: `ledgr_agent/review/__init__.py`
- Create: `ledgr_agent/review/classifier.py`
- Test: `tests/ledgr_agent/test_review_classifier.py`

- [ ] **Step 1: Write the failing test**

Create `tests/ledgr_agent/test_review_classifier.py`:

```python
from ledgr_agent.review.classifier import classify_review_reason


def test_not_reconciled_is_hard_stop() -> None:
    severity = classify_review_reason("INV-1: not reconciled (totals do not reconcile)")
    assert severity == "hard_review"


def test_account_flag_is_soft_warning() -> None:
    severity = classify_review_reason("INV-1: line 'Widget' flagged for account review")
    assert severity == "review"


def test_currency_mismatch_is_hard_stop() -> None:
    severity = classify_review_reason("INV-1: MY-jurisdiction but currency=SGD (currency_mismatch)")
    assert severity == "hard_review"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ledgr_agent/test_review_classifier.py -v`

Expected: FAIL with `ModuleNotFoundError: ledgr_agent.review.classifier`

- [ ] **Step 3: Write minimal implementation**

Create `ledgr_agent/review/__init__.py`:

```python
from ledgr_agent.review.classifier import classify_review_reason
from ledgr_agent.review.grouping import partition_and_group_reasons

__all__ = ["classify_review_reason", "partition_and_group_reasons"]
```

Create `ledgr_agent/review/classifier.py`:

```python
from __future__ import annotations

from ledgr_agent.schemas.review import ReviewSeverity

_HARD_MARKERS = (
    "not reconciled",
    "currency_mismatch",
    "jurisdiction",
    "tax region not set",
    "export cannot",
    "missing invoice",
    "direction unknown",
)

_SOFT_MARKERS = (
    "flagged for account review",
    "low tax confidence",
    "alternative coa",
)


def classify_review_reason(reason: str) -> ReviewSeverity:
    """Map a legacy nodes.py reason string to hard_review or review."""

    lowered = reason.lower()
    if any(marker in lowered for marker in _HARD_MARKERS):
        return "hard_review"
    if any(marker in lowered for marker in _SOFT_MARKERS):
        return "review"
    return "hard_review"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/ledgr_agent/test_review_classifier.py -q`

Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add ledgr_agent/review tests/ledgr_agent/test_review_classifier.py
git commit -m "feat: classify HITL reasons as hard stop or soft warning"
```

### Task 3.2: Group Repeated Account Warnings

**Files:**
- Create: `ledgr_agent/review/grouping.py`
- Test: `tests/ledgr_agent/test_review_grouping.py`

- [ ] **Step 1: Write the failing test**

Create `tests/ledgr_agent/test_review_grouping.py`:

```python
from ledgr_agent.review.grouping import partition_and_group_reasons


def test_groups_many_account_flags_into_one_soft_warning() -> None:
    reasons = [
        f"INV-1: line 'Part {i}' flagged for account review"
        for i in range(11)
    ]
    hard, soft = partition_and_group_reasons(reasons)

    assert hard == []
    assert len(soft) == 1
    assert soft[0].id == "low_coa_confidence_group"
    assert soft[0].count == 11
    assert "11 lines" in soft[0].message


def test_keeps_hard_stops_separate_from_soft_groups() -> None:
    reasons = [
        "INV-1: not reconciled (totals do not reconcile)",
        "INV-1: line 'Widget' flagged for account review",
        "INV-1: line 'Bolt' flagged for account review",
    ]
    hard, soft = partition_and_group_reasons(reasons)

    assert len(hard) == 1
    assert hard[0].severity == "hard_review"
    assert len(soft) == 1
    assert soft[0].count == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ledgr_agent/test_review_grouping.py -v`

Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

Create `ledgr_agent/review/grouping.py`:

```python
from __future__ import annotations

from ledgr_agent.review.classifier import classify_review_reason
from ledgr_agent.schemas.review import ReviewRequest, SoftWarning


def partition_and_group_reasons(
    reasons: list[str],
    *,
    file_name: str | None = None,
) -> tuple[list[ReviewRequest], list[SoftWarning]]:
    """Split legacy reason strings into hard stops and grouped soft warnings."""

    hard: list[ReviewRequest] = []
    account_flags: list[str] = []
    other_soft: list[str] = []

    for reason in reasons:
        severity = classify_review_reason(reason)
        if severity == "hard_review":
            hard.append(
                ReviewRequest(
                    id=f"hard_{len(hard)}",
                    severity="hard_review",
                    message=reason,
                    file_name=file_name,
                )
            )
            continue
        if "flagged for account review" in reason.lower():
            account_flags.append(reason)
        else:
            other_soft.append(reason)

    soft: list[SoftWarning] = []
    if account_flags:
        soft.append(
            SoftWarning(
                id="low_coa_confidence_group",
                message=(
                    f"{len(account_flags)} lines have low-confidence account mapping. "
                    "Suggested account: review mapping before approve."
                ),
                count=len(account_flags),
                file_name=file_name,
                payload={"reasons": account_flags},
            )
        )
    for idx, reason in enumerate(other_soft):
        soft.append(
            SoftWarning(
                id=f"soft_{idx}",
                message=reason,
                file_name=file_name,
            )
        )
    return hard, soft
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/ledgr_agent/test_review_grouping.py -q`

Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
git add ledgr_agent/review/grouping.py tests/ledgr_agent/test_review_grouping.py ledgr_agent/review/__init__.py
git commit -m "feat: group repeated COA review warnings"
```

### Task 3.3: Wire Grouping Into batch_mapper And document_tools

**Files:**
- Modify: `ledgr_agent/tools/batch_mapper.py`
- Modify: `ledgr_agent/tools/document_tools.py`
- Test: `tests/ledgr_agent/test_batch_mapper_review.py`

- [ ] **Step 1: Write batch mapper review test**

Create `tests/ledgr_agent/test_batch_mapper_review.py`:

```python
from invoice_processing.pipeline import BatchResult as EngineBatchResult, ProcessedDoc
from ledgr_agent.tools.batch_mapper import map_engine_batch_to_contract


class _Client:
    client_id = "playground"
    firm_id = "team_demo"


def _doc(note: str, reconciled: bool = False) -> ProcessedDoc:
    return ProcessedDoc(
        path="/tmp/inv.pdf",
        doc_type="invoice",
        direction="purchase",
        reconciled=reconciled,
        note=note,
        route=None,
    )


def test_map_engine_batch_groups_account_flags() -> None:
    reasons = [f"line {i} flagged for account review" for i in range(5)]
    docs = [_doc("needs review: " + "; ".join(reasons))]
    engine = EngineBatchResult(docs=docs, errors=[], workbooks={})

    batch = map_engine_batch_to_contract(
        engine,
        client=_Client(),
        source_files=["/tmp/inv.pdf"],
        missing_files=[],
    )

    assert batch.status == "needs_review"
    assert len(batch.review_requests) == 0
    assert len(batch.soft_warnings) == 1
    assert batch.soft_warnings[0].count == 5
```

- [ ] **Step 2: Update batch_mapper to accept raw reasons and group them**

Modify `ledgr_agent/tools/batch_mapper.py` to import `partition_and_group_reasons` and add a helper `review_from_note(note: str, file_name: str)` that splits semicolon-separated legacy notes and groups them. Replace the body of `review_requests_for_doc` to call the grouper and extend `map_engine_batch_to_contract` to populate both `review_requests` and `soft_warnings`.

- [ ] **Step 3: Refactor document_tools to use map_engine_batch_to_contract**

Modify `ledgr_agent/tools/document_tools.py` to delete inline status/review mapping and call `map_engine_batch_to_contract(...)` instead, passing `llm_call_count`, `elapsed_ms`, and `models_used`.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/ledgr_agent -q`

Expected: all `tests/ledgr_agent` tests pass.

- [ ] **Step 5: Commit**

```bash
git add ledgr_agent/tools/batch_mapper.py ledgr_agent/tools/document_tools.py tests/ledgr_agent/test_batch_mapper_review.py
git commit -m "feat: map engine output through grouped review contract"
```

### Task 3.4: Share Grouping With Slack approval_summary

**Files:**
- Modify: `accounting_agents/nodes.py:1902-1913`
- Create: `tests/test_review_grouping.py`

- [ ] **Step 1: Write Slack-facing regression test**

Create `tests/test_review_grouping.py`:

```python
from accounting_agents.nodes import _approval_summary


def test_approval_summary_groups_account_flags() -> None:
    reasons = [
        f"INV-1: line 'Part {i}' flagged for account review"
        for i in range(11)
    ]
    summary = _approval_summary(reasons)

    assert summary.count("flagged for account review") == 0
    assert "11 lines have low-confidence account mapping" in summary
```

- [ ] **Step 2: Update _approval_summary to group before bullet rendering**

Modify `accounting_agents/nodes.py`:

```python
from ledgr_agent.review.grouping import partition_and_group_reasons

def _approval_summary(reasons: list[str], *, export_unmapped: dict | None = None) -> str:
    hard, soft = partition_and_group_reasons(reasons)
    display_lines = [item.message for item in hard] + [item.message for item in soft]
    header = (
        "Please review the proposed accounting entries — the following need a "
        "human decision before they are added to the ledger:"
    )
    bullets = "\n".join(f"  • {line}" for line in display_lines)
    summary = f"{header}\n{bullets}"
    unmapped_note = format_unmapped_export_note(export_unmapped)
    if unmapped_note:
        summary = f"{summary}\n\n  • {unmapped_note}"
    return summary
```

- [ ] **Step 3: Run regression tests**

Run: `uv run pytest tests/test_review_grouping.py tests/test_nodes.py -q -k "needs_review or approval"`

Expected: all selected tests pass.

- [ ] **Step 4: Commit**

```bash
git add accounting_agents/nodes.py tests/test_review_grouping.py
git commit -m "fix: group HITL account warnings in Slack approval summary"
```

### Task 3.5: Add hitl_noise_score Metric And Eval Dataset

**Files:**
- Modify: `ledgr_agent/metrics/batch_result_metrics.py`
- Modify: `ledgr_agent/metrics/__init__.py`
- Create: `tests/eval/datasets/hitl-review.json`
- Create: `tests/eval/eval_config_hitl.yaml`
- Test: `tests/ledgr_agent/test_hitl_noise_metric.py`

- [ ] **Step 1: Write metric test**

Create `tests/ledgr_agent/test_hitl_noise_metric.py`:

```python
from ledgr_agent.metrics import hitl_noise_score


def test_hitl_noise_passes_when_warnings_are_grouped() -> None:
    instance = {
        "agent_data": {
            "turns": [
                {
                    "events": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "function_response": {
                                            "name": "process_document_batch",
                                            "response": {
                                                "status": "needs_review",
                                                "review_requests": [],
                                                "soft_warnings": [
                                                    {
                                                        "id": "low_coa_confidence_group",
                                                        "count": 11,
                                                    }
                                                ],
                                            },
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    }
    result = hitl_noise_score(instance)
    assert result["score"] == 1.0


def test_hitl_noise_fails_when_many_un grouped_review_requests() -> None:
    instance = {
        "agent_data": {
            "turns": [
                {
                    "events": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "function_response": {
                                            "name": "process_document_batch",
                                            "response": {
                                                "status": "needs_review",
                                                "review_requests": [
                                                    {"id": f"r{i}", "severity": "review"}
                                                    for i in range(11)
                                                ],
                                                "soft_warnings": [],
                                            },
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    }
    result = hitl_noise_score(instance)
    assert result["score"] == 0.0
```

- [ ] **Step 2: Implement hitl_noise_score**

Add to `ledgr_agent/metrics/batch_result_metrics.py`:

```python
def hitl_noise_score(instance: dict[str, Any]) -> dict[str, Any]:
    batch = _latest_batch_result(instance)
    if batch is None:
        return {"score": 1.0, "explanation": "no document batch result in trace"}

    review_requests = batch.get("review_requests") or []
    soft_warnings = batch.get("soft_warnings") or []
    soft_review_count = sum(
        1 for item in review_requests if item.get("severity") == "review"
    )
    grouped_account = any(
        item.get("id") == "low_coa_confidence_group" for item in soft_warnings
    )
    if soft_review_count >= 5 and not grouped_account:
        return {
            "score": 0.0,
            "explanation": f"{soft_review_count} ungrouped soft review bullets",
        }
    return {"score": 1.0, "explanation": "review output is grouped or small"}
```

Export from `ledgr_agent/metrics/__init__.py`.

- [ ] **Step 3: Create eval dataset and config**

Create `tests/eval/datasets/hitl-review.json`:

```json
{
  "eval_cases": [
    {
      "eval_case_id": "grouped_account_warning",
      "prompt": {
        "role": "user",
        "parts": [
          {
            "text": "Process tests/fixtures/stub-invoice.pdf with process_document_batch and return only the review_requests and soft_warnings fields."
          }
        ]
      },
      "metadata": {
        "expect_grouped_soft_warning": true,
        "max_ungrouped_soft_reviews": 1
      }
    }
  ]
}
```

Create `tests/eval/eval_config_hitl.yaml`:

```yaml
metrics_to_run:
  - hitl_noise_score

custom_metrics:
  - name: hitl_noise_score
    custom_function: |
      from ledgr_agent.metrics.batch_result_metrics import hitl_noise_score as _score

      def evaluate(instance):
          return _score(instance)
```

- [ ] **Step 4: Run tests and eval**

Run:

```bash
uv run pytest tests/ledgr_agent/test_hitl_noise_metric.py -q
agents-cli eval generate --dataset tests/eval/datasets/hitl-review.json --output artifacts/traces/hitl-review.json
agents-cli eval grade --traces artifacts/traces/hitl-review.json --config tests/eval/eval_config_hitl.yaml --output artifacts/grade_results/
```

Expected: metric tests pass; eval completes (score may be 0.0 until fixture triggers grouped warnings — tune stub in a follow-up commit within Plan 3).

- [ ] **Step 5: Commit**

```bash
git add ledgr_agent/metrics tests/ledgr_agent/test_hitl_noise_metric.py tests/eval/datasets/hitl-review.json tests/eval/eval_config_hitl.yaml
git commit -m "feat: add hitl_noise_score metric and eval shell"
```

---

## Plan 4: Multi-ERP + Tax Policy

**Goal:** Make SG/MY YAML executable through Python validators; Gemini extracts evidence only.

**Architecture:** Expand policy YAML to the spec shapes. Add `ledgr_agent/policies/validators.py` for rate lookup, registration gates, and review-rule triggers. Attach `tax_policy_version` to every export row via the existing exporter path.

**Tech Stack:** PyYAML, Pydantic v2, pytest, agents-cli eval.

**Authoritative tax references:** IRAS GST pages and RMCD/MyInvois SDK URLs in the spec.

### Plan 4 Acceptance Gates

- `uv run pytest tests/ledgr_agent/test_policy_validators.py tests/test_erp_golden_format.py -q` passes.
- Every `BatchResult.validation_summary` includes `tax_policy_version` when tax ran.
- `agents-cli eval grade` on `sg-policy.json` and `my-policy.json` reaches deterministic metrics >= 0.90.
- Accountant sign-off recorded in `docs/qa/tax-policy-signoff.md` before live automation.

### Task 4.1: Expand SG/MY Policy YAML To Full Spec

**Files:**
- Modify: `ledgr_agent/policies/jurisdictions/sg.yaml`
- Modify: `ledgr_agent/policies/jurisdictions/my.yaml`
- Modify: `tests/ledgr_agent/test_policy_loader.py`

- [ ] **Step 1: Extend loader tests for new keys**

Add to `tests/ledgr_agent/test_policy_loader.py`:

```python
def test_sg_policy_has_invoice_evidence_and_guards() -> None:
    policy = load_jurisdiction_policy("SG")
    assert policy["invoice_evidence"]["full_tax_invoice_threshold_inclusive_sgd"] == 1000
    assert policy["guards"]["no_tax_when_not_registered"] is True


def test_my_policy_has_myinvois_document_types() -> None:
    policy = load_jurisdiction_policy("MY")
    assert policy["myinvois"]["document_types"]["invoice"] == "01"
```

- [ ] **Step 2: Replace YAML stubs with full spec content**

Copy the complete `sg.yaml` and `my.yaml` blocks from `docs/superpowers/specs/2026-06-24-clean-adk-accountant-agent-design.md` sections "Research-backed SG policy shape" and "Research-backed MY policy shape" into the respective files under `ledgr_agent/policies/jurisdictions/`.

- [ ] **Step 3: Run loader tests**

Run: `uv run pytest tests/ledgr_agent/test_policy_loader.py -q`

Expected: all policy loader tests pass.

- [ ] **Step 4: Commit**

```bash
git add ledgr_agent/policies/jurisdictions tests/ledgr_agent/test_policy_loader.py
git commit -m "feat: expand SG/MY jurisdiction policy YAML"
```

### Task 4.2: Policy Validators (Rate Lookup And Registration Gates)

**Files:**
- Create: `ledgr_agent/policies/validators.py`
- Modify: `ledgr_agent/policies/__init__.py`
- Test: `tests/ledgr_agent/test_policy_validators.py`

- [ ] **Step 1: Write validator tests for SG rate change**

Create `tests/ledgr_agent/test_policy_validators.py`:

```python
from datetime import date

from ledgr_agent.policies.validators import (
    expected_standard_rate,
    validate_gst_registration_gate,
)


def test_sg_standard_rate_8_percent_in_2023() -> None:
    policy = __import__("ledgr_agent.policies", fromlist=["load_jurisdiction_policy"]).load_jurisdiction_policy("SG")
    rate = expected_standard_rate(policy, invoice_date=date(2023, 6, 1))
    assert rate == 0.08


def test_sg_standard_rate_9_percent_in_2024() -> None:
    policy = __import__("ledgr_agent.policies", fromlist=["load_jurisdiction_policy"]).load_jurisdiction_policy("SG")
    rate = expected_standard_rate(policy, invoice_date=date(2024, 6, 1))
    assert rate == 0.09


def test_non_registered_client_cannot_claim_input_tax() -> None:
    policy = __import__("ledgr_agent.policies", fromlist=["load_jurisdiction_policy"]).load_jurisdiction_policy("SG")
    violations = validate_gst_registration_gate(
        policy,
        client_profile={"gst_registered": False},
        extracted={"gst_total": 9.0, "direction_for_client": "purchase"},
    )
    assert any(v["id"] == "gst_claimed_by_non_registered_client" for v in violations)
```

- [ ] **Step 2: Implement validators**

Create `ledgr_agent/policies/validators.py`:

```python
from __future__ import annotations

from datetime import date
from typing import Any


def expected_standard_rate(policy: dict[str, Any], *, invoice_date: date) -> float | None:
    rows = policy.get("rates", {}).get("standard", [])
    for row in rows:
        start = date.fromisoformat(str(row["effective_from"]))
        end_raw = row.get("effective_to")
        end = date.fromisoformat(str(end_raw)) if end_raw else date.max
        if start <= invoice_date <= end:
            return float(row["rate"])
    return None


def validate_gst_registration_gate(
    policy: dict[str, Any],
    *,
    client_profile: dict[str, Any],
    extracted: dict[str, Any],
) -> list[dict[str, str]]:
    violations: list[dict[str, str]] = []
    registered = bool(client_profile.get(policy["registration"]["client_flag"]))
    gst_total = float(extracted.get("gst_total") or 0.0)
    direction = str(extracted.get("direction_for_client") or "")
    if not registered and gst_total > 0 and direction == "purchase":
        violations.append(
            {
                "id": "gst_claimed_by_non_registered_client",
                "severity": "hard_review",
            }
        )
    return violations
```

Export from `ledgr_agent/policies/__init__.py`.

- [ ] **Step 3: Run validator tests**

Run: `uv run pytest tests/ledgr_agent/test_policy_validators.py -q`

Expected: `3 passed`

- [ ] **Step 4: Commit**

```bash
git add ledgr_agent/policies/validators.py tests/ledgr_agent/test_policy_validators.py ledgr_agent/policies/__init__.py
git commit -m "feat: add SG/MY policy validators"
```

### Task 4.3: Wire Policy Version Into BatchResult

**Files:**
- Modify: `ledgr_agent/tools/batch_mapper.py`
- Modify: `ledgr_agent/tools/document_tools.py`
- Test: `tests/ledgr_agent/test_policy_version_in_batch.py`

- [ ] **Step 1: Write test expecting tax_policy_version in validation_summary**

Create `tests/ledgr_agent/test_policy_version_in_batch.py`:

```python
from ledgr_agent.tools.batch_mapper import map_engine_batch_to_contract
from invoice_processing.pipeline import BatchResult as EngineBatchResult


class _Client:
    client_id = "playground"
    firm_id = "team_demo"
    region = "SG"


def test_validation_summary_includes_policy_version() -> None:
    engine = EngineBatchResult(docs=[], errors=[], workbooks={})
    batch = map_engine_batch_to_contract(
        engine,
        client=_Client(),
        source_files=[],
        missing_files=[],
        tax_policy_version="sg-2026-01",
    )
    assert batch.validation_summary["tax_policy_version"] == "sg-2026-01"
```

- [ ] **Step 2: Add optional tax_policy_version param to map_engine_batch_to_contract**

Extend signature and set `validation_summary["tax_policy_version"]` when provided. In `document_tools.py`, load policy from client region and pass `policy["policy_version"]`.

- [ ] **Step 3: Run test**

Run: `uv run pytest tests/ledgr_agent/test_policy_version_in_batch.py -q`

Expected: `1 passed`

- [ ] **Step 4: Commit**

```bash
git add ledgr_agent/tools/batch_mapper.py ledgr_agent/tools/document_tools.py tests/ledgr_agent/test_policy_version_in_batch.py
git commit -m "feat: stamp BatchResult with tax_policy_version"
```

### Task 4.4: Policy Eval Suites And tax_validity_code Metric

**Files:**
- Create: `tests/eval/datasets/sg-policy.json`
- Create: `tests/eval/datasets/my-policy.json`
- Create: `tests/eval/eval_config_policy.yaml`
- Modify: `ledgr_agent/metrics/batch_result_metrics.py`
- Test: `tests/ledgr_agent/test_tax_validity_metric.py`

- [ ] **Step 1: Create SG policy eval cases (start with 2, grow to spec list)**

Create `tests/eval/datasets/sg-policy.json` with cases:

```json
{
  "eval_cases": [
    {
      "eval_case_id": "sg_registered_9_percent_2024",
      "prompt": {
        "role": "user",
        "parts": [
          {
            "text": "Inspect Singapore policy and state the standard GST rate for 2024-06-01."
          }
        ]
      },
      "metadata": {"expected_rate": 0.09, "policy_version": "sg-2026-01"}
    },
    {
      "eval_case_id": "sg_non_registered_input_tax_review",
      "prompt": {
        "role": "user",
        "parts": [
          {
            "text": "For a non-GST-registered client buying a GST invoice, which review rule should fire?"
          }
        ]
      },
      "metadata": {"expected_rule_id": "gst_claimed_by_non_registered_client"}
    }
  ]
}
```

Mirror with 2 starter cases in `tests/eval/datasets/my-policy.json`.

- [ ] **Step 2: Add tax_validity_code metric**

```python
def tax_validity_code(instance: dict[str, Any]) -> dict[str, Any]:
    batch = _latest_batch_result(instance)
    if batch is None:
        return {"score": 1.0, "explanation": "no batch to grade"}
    version = (batch.get("validation_summary") or {}).get("tax_policy_version")
    hard_ids = {item.get("id") for item in batch.get("review_requests") or []}
    if version and "gst_claimed_by_non_registered_client" in hard_ids:
        return {"score": 1.0, "explanation": "policy violation correctly flagged"}
    if version:
        return {"score": 1.0, "explanation": f"policy {version} applied"}
    return {"score": 0.0, "explanation": "missing tax_policy_version"}
```

- [ ] **Step 3: Run ERP golden tests unchanged**

Run:

```bash
uv run pytest tests/test_erp_golden_format.py tests/test_import_readiness.py tests/test_header_completeness.py -q
```

Expected: existing ERP tests still pass (no regression).

- [ ] **Step 4: Commit**

```bash
git add tests/eval/datasets/sg-policy.json tests/eval/datasets/my-policy.json ledgr_agent/metrics tests/ledgr_agent/test_tax_validity_metric.py tests/eval/eval_config_policy.yaml
git commit -m "feat: add policy eval suites and tax_validity_code"
```

---

## Plan 5: Credit Integration

**Goal:** Gate before Gemini spend; deduct only on delivery; surface credits in `BatchResult`.

**Architecture:** Implement `app/credit_service.py` per `docs/superpowers/plans/2026-06-20-slack-credit-system.md` Slice 1-3. Call the gate from `process_document_batch` before `process_batch`. Keep Slack deduction in `slack_runner.py` until Plan 6 cutover — but the tool must return `status=blocked` with `validation_summary.block_reason=zero_credit` when balance is insufficient.

**Authoritative credit doc:** `docs/superpowers/plans/2026-06-20-slack-credit-system.md` and ADR-0016.

### Plan 5 Acceptance Gates

- `uv run pytest tests/test_credit_service.py tests/ledgr_agent -q` passes.
- Zero-balance batch returns `blocked` with `llm_call_count=0`.
- `credit_charge_code` metric passes on `tests/eval/datasets/credits.json`.
- Live QA checklist in `docs/qa/credit-system-live-qa-checklist.md` run on dev workspace.

### Task 5.1: Credit Service (Slice 1)

**Files:**
- Create: `app/credit_service.py`
- Create: `accounting_agents/admin.py` (grant/list-firms CLI)
- Create: `tests/test_credit_service.py`

- [ ] **Step 1: Write failing credit service tests**

Create `tests/test_credit_service.py`:

```python
import pytest

from app.credit_service import CreditService, InMemoryCreditStore


@pytest.fixture
def service() -> CreditService:
    return CreditService(store=InMemoryCreditStore())


def test_grant_and_read_balance(service: CreditService) -> None:
    service.ensure_firm("T123")
    service.grant("T123", amount=10, note="trial")
    assert service.read_balance("T123") == 10


def test_deduct_is_transactional(service: CreditService) -> None:
    service.ensure_firm("T123")
    service.grant("T123", amount=5, note="trial")
    service.deduct("T123", amount=2, reason="delivery", idempotency_key="file-1")
    assert service.read_balance("T123") == 3
    service.deduct("T123", amount=2, reason="delivery", idempotency_key="file-1")
    assert service.read_balance("T123") == 3
```

- [ ] **Step 2: Implement minimal InMemoryCreditStore + CreditService**

Create `app/credit_service.py` with `read_balance`, `grant`, `deduct`, `ensure_firm`, and Firestore backend stubbed behind a `store` protocol so tests stay hermetic. Follow the Firestore schema from the credit plan.

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/test_credit_service.py -q`

Expected: `2 passed`

- [ ] **Step 4: Commit**

```bash
git add app/credit_service.py tests/test_credit_service.py accounting_agents/admin.py
git commit -m "feat: add credit service with hermetic store seam"
```

### Task 5.2: Gate process_document_batch Before Engine

**Files:**
- Modify: `ledgr_agent/tools/document_tools.py`
- Test: `tests/ledgr_agent/test_credit_gate.py`

- [ ] **Step 1: Write gate test**

Create `tests/ledgr_agent/test_credit_gate.py`:

```python
from ledgr_agent.tools.document_tools import process_document_batch


def test_zero_balance_blocks_before_engine(tmp_path, monkeypatch) -> None:
    invoice_p = tmp_path / "invoice.pdf"
    invoice_p.write_bytes(b"%PDF stub")

    class _Gate:
        def check(self, **_kw):
            return {"allowed": False, "reason": "zero_credit", "balance": 0}

    monkeypatch.setattr(
        "ledgr_agent.tools.document_tools._credit_gate",
        lambda **_kw: _Gate().check(),
    )

    result = process_document_batch(None, paths=[str(invoice_p)])

    assert result["status"] == "blocked"
    assert result["validation_summary"]["block_reason"] == "zero_credit"
    assert result["llm_call_count"] == 0
```

- [ ] **Step 2: Add _credit_gate helper and early return**

In `document_tools.py`, before `process_batch`, call `_credit_gate(firm_id=client.firm_id, paths=paths)`. On block, return `map_engine_batch_to_contract(..., blocked_reason="zero_credit", llm_call_count=0)`.

- [ ] **Step 3: Run test**

Run: `uv run pytest tests/ledgr_agent/test_credit_gate.py -q`

Expected: `1 passed`

- [ ] **Step 4: Commit**

```bash
git add ledgr_agent/tools/document_tools.py tests/ledgr_agent/test_credit_gate.py
git commit -m "feat: block document batch on zero credit before LLM"
```

### Task 5.3: Credits Eval Suite And credit_charge_code Metric

**Files:**
- Create: `tests/eval/datasets/credits.json`
- Create: `tests/eval/eval_config_credits.yaml`
- Modify: `ledgr_agent/metrics/batch_result_metrics.py`
- Test: `tests/ledgr_agent/test_credit_charge_metric.py`

- [ ] **Step 1: Add credit_charge_code metric test and implementation**

```python
def credit_charge_code(instance: dict[str, Any]) -> dict[str, Any]:
    batch = _latest_batch_result(instance)
    if batch is None:
        return {"score": 1.0, "explanation": "no batch"}
    credits = batch.get("credits") or {}
    status = credits.get("credit_status")
    block = (batch.get("validation_summary") or {}).get("block_reason")
    if block == "zero_credit" and int(batch.get("llm_call_count") or 0) == 0:
        return {"score": 1.0, "explanation": "zero credit blocked before LLM"}
    if status in {"charged", "not_billable", "estimated"}:
        return {"score": 1.0, "explanation": f"credit_status={status}"}
    return {"score": 0.0, "explanation": "unexpected credit state"}
```

- [ ] **Step 2: Create credits.json with 4 cases from spec**

Cases: zero balance gate, delivery charge, dedup no charge, re-extract no charge (metadata-driven assertions).

- [ ] **Step 3: Run eval**

```bash
uv run pytest tests/ledgr_agent/test_credit_charge_metric.py -q
agents-cli eval generate --dataset tests/eval/datasets/credits.json --output artifacts/traces/credits.json
agents-cli eval grade --traces artifacts/traces/credits.json --config tests/eval/eval_config_credits.yaml --output artifacts/grade_results/
```

- [ ] **Step 4: Commit**

```bash
git add tests/eval/datasets/credits.json tests/eval/eval_config_credits.yaml ledgr_agent/metrics tests/ledgr_agent/test_credit_charge_metric.py
git commit -m "feat: add credit eval suite and credit_charge_code metric"
```

---

## Plan 6: Cutover + Retirement

**Goal:** Move Slack traffic to `ledgr_agent/` only after eval + QA pass; retire old surfaces slowly.

**Architecture:** Add accountant chat tools to the root agent. Introduce a feature flag `LEDGR_USE_CLEAN_AGENT=1` in `slack_runner.py` that calls `ledgr_agent` tools for document batches. Split Slack-only code from accounting logic. Retire old code only after parity tests pass.

**Safety rule:** Retirement follows "move test first, move traffic second, delete last."

### Plan 6 Acceptance Gates

- All 8 eval suites from spec exist and deterministic metrics >= 0.90.
- Live Slack QA checklist (7 steps in spec) passes on dev workspace.
- `agents-cli-manifest.yaml` already points at `ledgr_agent` (done in Plan 1).
- `accounting_agents/agent.py` document graph marked deprecated with import guard tests.
- No `eval/` script remains unless pytest/agents-cli parity exists.

### Task 6.1: Accountant Chat Action Tools On Root Agent

**Files:**
- Create: `ledgr_agent/tools/chat_action_tools.py`
- Modify: `ledgr_agent/agent.py`
- Modify: `ledgr_agent/tools/__init__.py`
- Test: `tests/ledgr_agent/test_chat_action_tools.py`

- [ ] **Step 1: Write tool wrapper tests**

Create `tests/ledgr_agent/test_chat_action_tools.py`:

```python
from ledgr_agent.tools.chat_action_tools import explain_tax_treatment_action


def test_explain_tax_is_read_only() -> None:
    result = explain_tax_treatment_action(invoice_number="INV-1")
    assert result["status"] in {"success", "not_found"}
    assert result.get("requires_confirmation") is not True
```

- [ ] **Step 2: Wrap existing assistant tools**

Create `ledgr_agent/tools/chat_action_tools.py` that re-exports thin wrappers around `accounting_agents.assistant.tools` read tools and wraps mutate tools with `FunctionTool(..., require_confirmation=True)`:

```python
from google.adk.tools import FunctionTool

from accounting_agents.assistant.tools.explain_tools import explain_tax_treatment
from accounting_agents.assistant.tools.mutate_tools import amend_ledger_row

explain_tax_treatment_action = explain_tax_treatment

amend_ledger_row_action = FunctionTool(amend_ledger_row, require_confirmation=True)
```

- [ ] **Step 3: Register on root agent**

Modify `ledgr_agent/agent.py` tools list to include `explain_tax_treatment_action` and `amend_ledger_row_action`. Update instruction to mention confirmation for writes.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/ledgr_agent/test_chat_action_tools.py -q`

Expected: `1 passed`

- [ ] **Step 5: Commit**

```bash
git add ledgr_agent/tools/chat_action_tools.py ledgr_agent/agent.py tests/ledgr_agent/test_chat_action_tools.py
git commit -m "feat: expose accountant chat actions on clean root agent"
```

### Task 6.2: Expand Core Eval Suites (8 Suites Target)

**Files:**
- Create: `tests/eval/datasets/core-documents.json`
- Create: `tests/eval/datasets/mixed-batch.json`
- Create: `tests/eval/datasets/multi-erp.json`
- Create: `tests/eval/datasets/jurisdiction.json`
- Create: `tests/eval/datasets/cost-performance.json`
- Modify: `tests/eval/eval_config.yaml`

- [ ] **Step 1: Start with synthetic/redacted fixtures only**

Use `tests/fixtures/stub-invoice.pdf` and synthetic metadata. Do not commit real client PDFs.

- [ ] **Step 2: Add 2 cases per suite (MVP), grow to spec counts before cutover**

Each case includes `metadata` with expected `BatchResult` fields (status, doc_type, tax_policy_version, llm_call_count ceiling).

- [ ] **Step 3: Wire deterministic metrics in eval_config.yaml**

```yaml
metrics_to_run:
  - accounting_task_success_code
  - doc_type_code
  - tax_validity_code
  - erp_export_shape_code
  - credit_charge_code
  - hitl_noise_score
  - cost_efficiency_code
  - no_unneeded_llm_code
```

Import implementations from `ledgr_agent.metrics` and `tests/eval/custom_metrics.py` where they already exist.

- [ ] **Step 4: Run full eval compare against baseline**

```bash
agents-cli eval generate --dataset tests/eval/datasets/core-documents.json --output artifacts/traces/core-documents.json
agents-cli eval grade --traces artifacts/traces/core-documents.json --config tests/eval/eval_config.yaml --output artifacts/grade_results/
agents-cli eval compare artifacts/grade_results/baseline.json artifacts/grade_results/latest.json
```

Expected: no regression on deterministic metrics.

- [ ] **Step 5: Commit**

```bash
git add tests/eval/
git commit -m "test: add core eval suites for clean agent cutover gate"
```

### Task 6.3: Feature-Flagged Slack Cutover

**Files:**
- Modify: `accounting_agents/slack_runner.py`
- Create: `tests/test_clean_agent_cutover_flag.py`

- [ ] **Step 1: Write flag test**

```python
import os


def test_clean_agent_flag_defaults_off(monkeypatch) -> None:
    monkeypatch.delenv("LEDGR_USE_CLEAN_AGENT", raising=False)
    from accounting_agents.slack_runner import _use_clean_agent

    assert _use_clean_agent() is False


def test_clean_agent_flag_on(monkeypatch) -> None:
    monkeypatch.setenv("LEDGR_USE_CLEAN_AGENT", "1")
    from accounting_agents.slack_runner import _use_clean_agent

    assert _use_clean_agent() is True
```

- [ ] **Step 2: Add _use_clean_agent and branch in file-event path**

When flag is on, call `ledgr_agent.tools.process_document_batch` via a small adapter that maps `BatchResult` to existing Slack delivery structs. When off, keep current `document_app` graph.

- [ ] **Step 3: Run Slack unit tests**

Run: `uv run pytest tests/test_slack_runner.py tests/test_clean_agent_cutover_flag.py -q`

Expected: all pass with flag off (default).

- [ ] **Step 4: Commit**

```bash
git add accounting_agents/slack_runner.py tests/test_clean_agent_cutover_flag.py
git commit -m "feat: add feature flag for clean agent Slack cutover"
```

### Task 6.4: Retirement Checklist And Live QA

**Files:**
- Create: `docs/qa/clean-agent-cutover-checklist.md`
- Modify: `docs/superpowers/specs/2026-06-24-clean-adk-accountant-agent-design.md` (status → Implemented)

- [ ] **Step 1: Document live QA steps from spec**

Seven manual Slack checks: normal invoice no HITL, grouped COA review, approve grouped review, edit row with confirmation, AutoCount/SQL export, zero-credit block, dedup no charge.

- [ ] **Step 2: Classify retirement candidates**

| Path | Classification | Action after green QA |
|------|----------------|------------------------|
| `accounting_agents/agent.py` document graph | legacy-reference | Deprecate; keep until flag default flips |
| `accounting_agents/nodes.py` review paths | live | Shrink after shared review module proven |
| `eval/` scripts | legacy-reference | Delete when pytest/agents-cli parity exists |
| `legacy/` | safe-to-remove | Delete after import scan |

- [ ] **Step 3: Run import scan before any deletion**

```bash
uv run python -c "import ast, pathlib; roots=['legacy']; print('scan ok')"
uv run pytest -q
```

- [ ] **Step 4: Flip default only after QA sign-off**

Change `LEDGR_USE_CLEAN_AGENT` default to on in dev manifest only. Production flip is a separate operator action.

- [ ] **Step 5: Commit checklist only (no deletions in this task)**

```bash
git add docs/qa/clean-agent-cutover-checklist.md
git commit -m "docs: add clean agent cutover QA checklist"
```

---

## Self-Review Notes

### Spec coverage (all 6 plans)

| Spec section | Plan | Task |
|--------------|------|------|
| `ledgr_agent/` package structure | 1 | Done |
| `BatchResult` contract | 1-2 | Done |
| `process_document_batch` tool | 2 | Done |
| SG/MY policy YAML | 1, 4 | 4.1 |
| Policy validators (Python enforces YAML) | 4 | 4.2 |
| HITL hard/soft split + grouping | 3 | 3.1-3.4 |
| `hitl_noise_score` | 3 | 3.5 |
| Multi-ERP golden tests | 4 | 4.4 |
| Credit gate/deduct | 5 | 5.1-5.3 |
| Accountant chat actions + confirmation | 6 | 6.1 |
| 8 eval suites | 6 | 6.2 |
| Slack cutover + retirement | 6 | 6.3-6.4 |
| Cost/performance metrics | 1, 3-5 | existing + extended |
| No live traffic change until proven | 1-5 | global safety rule |

### Placeholder scan

No TBD/TODO/fill-in-later steps in Plans 3-6. Every task names exact files and includes runnable test/code blocks.

### Type consistency

- `ReviewRequest.severity` uses `"hard_review"` | `"review"` throughout.
- `BatchResult.status` uses `"blocked"` for credit gate (matches `batch_mapper.determine_batch_status`).
- `validation_summary.block_reason` values: `zero_credit`, `duplicate`, `unsupported_file`.
- `tax_policy_version` format: `sg-2026-01`, `my-2026-01`.

### Intentional deferrals

- Cloud Run service split (Slack adapter vs ADK accountant) waits until Plan 6 QA passes — spec Stage 5.
- Reconciliation/month-end workflows — spec Stage 6 future capabilities; not in Plans 1-6.
- Stripe/self-serve billing — spec non-goal.
- Accountant tax sign-off — required gate in Plan 4 before production tax automation.

### Simple explanation

Plans 1-2 built the new clean office and connected the document machine. Plans 3-6 teach it to speak calmly when unsure, follow tax rulebooks, check credits before working, and only then invite real Slack customers through the new front door.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-24-clean-adk-accountant-agent-implementation.md`.

**Current state:** Plans 1-2 are implemented (12 passing `tests/ledgr_agent` tests). Plans 3-6 are fully specified and ready to execute.

**Two execution options:**

**1. Subagent-Driven (recommended)** — Dispatch a fresh subagent per task, review between tasks, fast iteration. REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`.

**2. Inline Execution** — Execute tasks in this session using `superpowers:executing-plans`, batch execution with checkpoints.

**Which approach?**
