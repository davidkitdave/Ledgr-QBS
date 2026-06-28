from accounting_agents.review.grouping import merge_soft_warnings, partition_and_group_reasons
from accounting_agents.batch_schemas import SoftWarning


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


def test_groups_reconcile_mismatches_into_one_soft_warning() -> None:
    reasons = [
        "subtotal: lines=65.00 vs doc=60.19 (diff=+4.81, tol=2c)",
        "gst: lines=0.00 vs doc=4.81 (diff=-4.81, tol=2c)",
    ]
    hard, soft = partition_and_group_reasons(reasons)

    assert hard == []
    assert len(soft) == 1
    assert soft[0].id == "reconcile_mismatch_group"
    assert soft[0].count == 2
    assert "line/total mismatches" in soft[0].message


def test_groups_missing_fields_into_one_soft_warning() -> None:
    reasons = ["needs review: missing CreditorCode, AccNo"]
    hard, soft = partition_and_group_reasons(reasons)

    assert hard == []
    assert len(soft) == 1
    assert soft[0].id == "missing_fields_group"
    assert "CreditorCode" in soft[0].message


def test_reconcile_note_with_all_three_reason_classes() -> None:
    """Typical unreconciled sub-invoice note — one grouped card per class, not 3 bullets."""
    note = (
        "subtotal: lines=65.00 vs doc=60.19 (diff=+4.81, tol=2c); "
        "gst: lines=0.00 vs doc=4.81 (diff=-4.81, tol=2c); "
        "needs review: missing CreditorCode, AccNo"
    )
    reasons = [r.strip() for r in note.split(";") if r.strip()]
    hard, soft = partition_and_group_reasons(reasons)

    assert hard == []
    assert len(soft) == 2
    assert {w.id for w in soft} == {"reconcile_mismatch_group", "missing_fields_group"}


def test_merge_soft_warnings_dedupes_identical_reasons_across_documents() -> None:
    reason_a = "subtotal: lines=65.00 vs doc=60.19 (diff=+4.81, tol=2c)"
    reason_b = "gst: lines=0.00 vs doc=4.81 (diff=-4.81, tol=2c)"
    per_doc = [
        SoftWarning(
            id="reconcile_mismatch_group",
            message="2 line/total mismatches — review before posting.",
            count=2,
            payload={"reasons": [reason_a, reason_b]},
        )
        for _ in range(3)
    ]

    merged = merge_soft_warnings(per_doc)

    assert len(merged) == 1
    assert merged[0].count == 3
    assert merged[0].payload["reasons"] == [reason_a, reason_b]


def test_hard_blockers_stay_as_review_requests_not_grouped() -> None:
    reasons = [
        "INV-1: MY-jurisdiction but currency=SGD (currency_mismatch)",
        "subtotal: lines=65.00 vs doc=60.19 (diff=+4.81, tol=2c)",
    ]
    hard, soft = partition_and_group_reasons(reasons)

    assert len(hard) == 1
    assert hard[0].severity == "hard_review"
    assert "currency_mismatch" in hard[0].message
    assert len(soft) == 1
    assert soft[0].id == "reconcile_mismatch_group"
