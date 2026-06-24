from ledgr_agent.metrics import tax_validity_code


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


def test_tax_validity_passes_when_specific_rule_flagged() -> None:
    instance = _batch_result(
        {
            "status": "needs_review",
            "validation_summary": {"tax_policy_version": "sg-2026-01"},
            "review_requests": [
                {"id": "gst_claimed_by_non_registered_client", "severity": "block"}
            ],
        }
    )

    result = tax_validity_code(instance)

    assert result["score"] == 1.0
    assert "flagged" in result["explanation"].lower()


def test_tax_validity_passes_when_policy_version_present_regardless_of_rule() -> None:
    instance = _batch_result(
        {
            "status": "success",
            "validation_summary": {"tax_policy_version": "my-2026-01"},
            "review_requests": [],
        }
    )

    result = tax_validity_code(instance)

    assert result["score"] == 1.0
    assert "my-2026-01" in result["explanation"]


def test_tax_validity_fails_when_no_policy_version_stamped() -> None:
    instance = _batch_result(
        {
            "status": "success",
            "validation_summary": {},
            "review_requests": [],
        }
    )

    result = tax_validity_code(instance)

    assert result["score"] == 0.0
