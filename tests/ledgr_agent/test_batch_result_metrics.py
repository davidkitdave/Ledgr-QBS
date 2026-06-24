from ledgr_agent.metrics import (
    accounting_task_success_code,
    cost_efficiency_code,
    doc_type_code,
    erp_export_shape_code,
    no_unneeded_llm_code,
)
 
 
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


def _batch_instance(payload: dict) -> dict:
    return {
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
                                            "response": payload,
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


def test_accounting_task_success_passes_when_status_success() -> None:
    result = accounting_task_success_code(
        _batch_instance({"status": "success", "doc_type": "invoice"})
    )
    assert result["score"] == 1.0


def test_accounting_task_success_fails_when_status_blocked() -> None:
    result = accounting_task_success_code(
        _batch_instance({"status": "blocked", "block_reason": "zero_credit"})
    )
    assert result["score"] == 0.0


def test_doc_type_passes_for_recognised_label() -> None:
    result = doc_type_code(_batch_instance({"doc_type": "invoice"}))
    assert result["score"] == 1.0


def test_doc_type_passes_for_mixed_label() -> None:
    result = doc_type_code(_batch_instance({"doc_type": "mixed"}))
    assert result["score"] == 1.0


def test_doc_type_fails_for_unknown_label() -> None:
    result = doc_type_code(_batch_instance({"doc_type": "parcel"}))
    assert result["score"] == 0.0


def test_erp_export_shape_passes_for_list_rows() -> None:
    result = erp_export_shape_code(_batch_instance({"export_rows": [{"a": 1}]}))
    assert result["score"] == 1.0


def test_erp_export_shape_fails_when_missing() -> None:
    result = erp_export_shape_code(_batch_instance({"status": "success"}))
    assert result["score"] == 0.0
