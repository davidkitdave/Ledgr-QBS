from accounting_agents.nodes import _approval_summary


def test_approval_summary_groups_account_flags() -> None:
    reasons = [
        f"INV-1: line 'Part {i}' flagged for account review"
        for i in range(11)
    ]
    summary = _approval_summary(reasons)

    assert summary.count("flagged for account review") == 0
    assert "11 lines have low-confidence account mapping" in summary
