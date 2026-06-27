"""Tests for light-read eval metrics."""

from __future__ import annotations

from types import SimpleNamespace

from ledgr_agent.metrics.light_read_metrics import light_read_cost_code


def _auditair_read_response(*, total_token_count: int = 5000) -> dict:
    return {
        "doc_type": "purchase",
        "vendor_name": "A Cube TIC Limited",
        "invoice_number": "INV 011861",
        "grand_total": 3052.0,
        "extraction_meta": {
            "gemini_call_count": 1,
            "model": "gemini-2.5-flash-lite",
            "usage": {"total_token_count": total_token_count},
        },
    }


def _instance(read_response: dict, calls: list[str]) -> dict:
    events = []
    for name in calls:
        if name == "read_document":
            events.append(
                {
                    "content": {
                        "parts": [
                            {"function_call": {"name": "read_document"}},
                            {"function_response": {"name": "read_document", "response": read_response}},
                        ]
                    }
                }
            )
        elif name == "project_to_erp":
            events.append(
                {
                    "content": {
                        "parts": [
                            {"function_call": {"name": "project_to_erp"}},
                            {"function_response": {"name": "project_to_erp", "response": {"status": "success"}}},
                        ]
                    }
                }
            )
    return {
        "eval_case_id": "light_read_auditair_iso",
        "agent_data": {"turns": [{"events": events}]},
    }


def test_light_read_cost_passes_under_baseline_ceiling() -> None:
    out = light_read_cost_code(_instance(_auditair_read_response(total_token_count=1000), ["read_document", "project_to_erp"]))
    assert out["score"] == 1.0
    assert "cost ok" in out["explanation"]


def test_light_read_cost_skips_other_cases() -> None:
    inst = _instance(_auditair_read_response(), ["read_document", "project_to_erp"])
    inst["eval_case_id"] = "other_case"
    out = light_read_cost_code(inst)
    assert out["score"] == 1.0


def test_light_read_cost_fails_factory_path() -> None:
    out = light_read_cost_code(
        _instance(_auditair_read_response(), ["process_document_batch"])
    )
    assert out["score"] == 0.0


def test_light_read_cost_fails_wrong_call_count() -> None:
    read = _auditair_read_response()
    read["extraction_meta"]["gemini_call_count"] = 2
    out = light_read_cost_code(_instance(read, ["read_document", "project_to_erp"]))
    assert out["score"] == 0.0


def test_sequential_bill_pipeline_builds() -> None:
    from ledgr_agent.agents.bill_pipeline import build_bill_pipeline_agent

    agent = build_bill_pipeline_agent()
    assert agent.name == "bill_pipeline"
    assert len(agent.sub_agents) == 2
    assert agent.sub_agents[0].name == "bill_read_node"
    assert agent.sub_agents[1].name == "bill_project_node"


def test_sequential_bank_pipeline_builds() -> None:
    from ledgr_agent.agents.bank_pipeline import build_bank_pipeline_agent

    agent = build_bank_pipeline_agent()
    assert agent.name == "bank_pipeline"
    assert len(agent.sub_agents) == 2


def test_usage_from_response_extracts_counts() -> None:
    from ledgr_agent.shared.gemini_usage import usage_from_response

    resp = SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=100,
            candidates_token_count=50,
            total_token_count=150,
        )
    )
    assert usage_from_response(resp) == {
        "prompt_token_count": 100,
        "candidates_token_count": 50,
        "total_token_count": 150,
    }
