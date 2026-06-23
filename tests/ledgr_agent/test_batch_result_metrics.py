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
