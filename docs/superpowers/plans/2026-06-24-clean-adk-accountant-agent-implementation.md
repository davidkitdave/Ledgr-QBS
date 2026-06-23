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

This document fully details **Plan 1**. Plans 2-6 should be written after Plan 1 lands, because Plan 1 defines the contracts those later plans depend on.

---

## Current Repo Facts

- `app/main.py` is the current Cloud Run entrypoint and imports `accounting_agents.slack_runner.build_fastapi_app`.
- `agents-cli-manifest.yaml` currently uses `agent_directory: "accounting_agents"`.
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

## Plan 2 Preview: Document Tool Wrapper

Plan 2 should be written after Plan 1 passes. It should:

- Add `ledgr_agent/tools/document_tools.py`.
- Wrap existing production-safe `invoice_processing` calls behind `process_document_batch`.
- Convert existing engine output into `BatchResult`.
- Record `llm_call_count`, `models_used`, `strong_model_used`, `documents_skipped_before_llm`, and credit placeholders.
- Add `tests/ledgr_agent/test_document_tool_contract.py`.
- Add agents-cli eval cases that assert `process_document_batch` appears in tool traces.
- Keep real/private PDFs in `tests/eval_invoices/` or `scratch/` only.

---

## Plan 3 Preview: HITL + Review Cleanup

Plan 3 should:

- Add hard-stop vs soft-warning classifiers.
- Group repeated low-confidence COA warnings.
- Add `hitl_noise_score`.
- Keep dangerous chat writes behind ADK confirmation or the current Slack/RequestInput bridge after verification.
- Add regression tests proving one invoice does not create many repeated Slack review bullets.

---

## Plan 4 Preview: Multi-ERP + Tax Policy

Plan 4 should:

- Make SG/MY policy YAML executable through Python validators.
- Keep Gemini as evidence extractor only.
- Add `sg-policy.json` and `my-policy.json` eval suites.
- Strengthen `tests/test_erp_golden_format.py` and related ERP tests.
- Confirm final tax behavior with an accountant before live automation.

---

## Plan 5 Preview: Credit Integration

Plan 5 should:

- Implement Firestore credit gate before expensive processing.
- Deduct only after successful delivery.
- Add idempotent credit ledger refs to `BatchResult`.
- Add `credits.json` eval cases.
- Use live QA for zero-credit, dedup, rejected-doc, and re-extract cases.

---

## Plan 6 Preview: Cutover + Retirement

Plan 6 should:

- Move traffic only after tests, eval, and Slack QA pass.
- Split Slack adapter concerns from accountant logic.
- Shrink `accounting_agents/slack_runner.py`.
- Retire `eval/` scripts only after parity exists in `tests/eval/` or pytest.
- Retire old `accounting_agents/agent.py` document graph only after `ledgr_agent/workflows/` owns the flow.

---

## Self-Review Notes

Spec coverage:

- `ledgr_agent/` package: Task 1.
- `BatchResult`, review, warning, credit schemas: Task 2.
- SG/MY policy YAML: Task 3.
- Debug/playground visibility: Task 8.
- Cost/performance metric shell: Task 6.
- Agents-cli eval shell: Task 7 and Task 8.
- No live Slack traffic change: global safety rule and Task 8.
- Private eval docs remain ignored: current repo facts and Task 9.

Known intentional gaps:

- No real `process_document_batch` implementation in Plan 1. This belongs in Plan 2.
- No Slack HITL change in Plan 1. This belongs in Plan 3.
- No Firestore credit implementation in Plan 1. This belongs in Plan 5.
- No production traffic cutover in Plan 1. This belongs in Plan 6.

Child-simple explanation:

Plan 1 builds the new empty clean office, labels the cabinets, and makes sure the door opens. It does not move customers there yet.
