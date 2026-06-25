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
