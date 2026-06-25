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


def test_hitl_noise_fails_when_many_ungrouped_review_requests() -> None:
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
