from ledgr_agent.metrics import credit_charge_code


def _batch_result(payload: dict) -> dict:
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


def test_credit_charge_passes_when_zero_credit_blocked_before_llm() -> None:
    instance = _batch_result(
        {
            "status": "blocked",
            "validation_summary": {"block_reason": "zero_credit"},
            "llm_call_count": 0,
            "credits": {"credit_status": "blocked", "balance": 0},
        }
    )

    result = credit_charge_code(instance)

    assert result["score"] == 1.0
    assert "zero credit" in result["explanation"].lower()


def test_credit_charge_passes_when_status_charged() -> None:
    instance = _batch_result(
        {
            "status": "success",
            "llm_call_count": 2,
            "credits": {"credit_status": "charged", "pages": 1, "balance": 9},
        }
    )

    result = credit_charge_code(instance)

    assert result["score"] == 1.0
    assert "charged" in result["explanation"]


def test_credit_charge_passes_when_status_not_billable() -> None:
    instance = _batch_result(
        {
            "status": "success",
            "llm_call_count": 1,
            "credits": {"credit_status": "not_billable", "reason": "reextract"},
        }
    )

    result = credit_charge_code(instance)

    assert result["score"] == 1.0
    assert "not_billable" in result["explanation"]


def test_credit_charge_passes_when_status_estimated() -> None:
    instance = _batch_result(
        {
            "status": "needs_review",
            "llm_call_count": 2,
            "credits": {"credit_status": "estimated", "pages": 1, "balance": 10},
        }
    )

    result = credit_charge_code(instance)

    assert result["score"] == 1.0
    assert "estimated" in result["explanation"]


def test_credit_charge_fails_on_unexpected_credit_state() -> None:
    instance = _batch_result(
        {
            "status": "success",
            "llm_call_count": 2,
            "credits": {"credit_status": "unknown"},
        }
    )

    result = credit_charge_code(instance)

    assert result["score"] == 0.0
    assert "unexpected" in result["explanation"].lower()


def test_credit_charge_fails_when_no_batch_in_trace() -> None:
    # A trace with no process_document_batch call must FAIL — the tool never ran.
    instance = {"agent_data": {"turns": []}}

    result = credit_charge_code(instance)

    assert result["score"] == 0.0
    assert "no process_document_batch" in result["explanation"].lower()
