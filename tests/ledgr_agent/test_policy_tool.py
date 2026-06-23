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
