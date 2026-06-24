from ledgr_agent.review.grouping import partition_and_group_reasons


def test_groups_many_account_flags_into_one_soft_warning() -> None:
    reasons = [
        f"INV-1: line 'Part {i}' flagged for account review"
        for i in range(11)
    ]
    hard, soft = partition_and_group_reasons(reasons)

    assert hard == []
    assert len(soft) == 1
    assert soft[0].id == "low_coa_confidence_group"
    assert soft[0].count == 11
    assert "11 lines" in soft[0].message


def test_keeps_hard_stops_separate_from_soft_groups() -> None:
    reasons = [
        "INV-1: not reconciled (totals do not reconcile)",
        "INV-1: line 'Widget' flagged for account review",
        "INV-1: line 'Bolt' flagged for account review",
    ]
    hard, soft = partition_and_group_reasons(reasons)

    assert len(hard) == 1
    assert hard[0].severity == "hard_review"
    assert len(soft) == 1
    assert soft[0].count == 2
