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


def test_sg_policy_has_invoice_evidence_and_guards() -> None:
    policy = load_jurisdiction_policy("SG")
    assert policy["invoice_evidence"]["full_tax_invoice_threshold_inclusive_sgd"] == 1000
    assert policy["guards"]["no_tax_when_not_registered"] is True


def test_my_policy_has_myinvois_document_types() -> None:
    policy = load_jurisdiction_policy("MY")
    assert policy["myinvois"]["document_types"]["invoice"] == "01"
