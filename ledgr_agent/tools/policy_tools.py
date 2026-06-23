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
