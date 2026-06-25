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


def test_reconcile_subtotal_mismatch_is_soft_warning() -> None:
    reason = "subtotal: lines=65.00 vs doc=60.19 (diff=+4.81, tol=2c)"
    assert classify_review_reason(reason) == "review"


def test_reconcile_gst_mismatch_is_soft_warning() -> None:
    reason = "gst: lines=0.00 vs doc=4.81 (diff=-4.81, tol=2c)"
    assert classify_review_reason(reason) == "review"


def test_missing_required_fields_is_soft_warning() -> None:
    reason = "needs review: missing CreditorCode, AccNo"
    assert classify_review_reason(reason) == "review"
