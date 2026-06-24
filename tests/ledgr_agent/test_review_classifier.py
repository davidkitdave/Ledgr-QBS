from ledgr_agent.review.classifier import classify_review_reason


def test_not_reconciled_is_hard_stop() -> None:
    severity = classify_review_reason("INV-1: not reconciled (totals do not reconcile)")
    assert severity == "hard_review"


def test_account_flag_is_soft_warning() -> None:
    severity = classify_review_reason("INV-1: line 'Widget' flagged for account review")
    assert severity == "review"


def test_currency_mismatch_is_hard_stop() -> None:
    severity = classify_review_reason("INV-1: MY-jurisdiction but currency=SGD (currency_mismatch)")
    assert severity == "hard_review"
