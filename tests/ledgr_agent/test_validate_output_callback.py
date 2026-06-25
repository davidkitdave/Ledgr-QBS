"""Hermetic tests for ledgr_agent.callbacks.validate_output.

Tests cover:
- Hard violation present + status=="success" → callback flips status to
  "needs_review" and sets validation_summary["policy_enforcement"].
- Same input with LEDGR_VALIDATE_STRICT=1 → callback raises ValueError.
- Clean response (no hard violation) → returns None (pass-through).
- Hard violation already reflected (status != "success") → returns None.
- Non-process_document_batch tool → returns None.
- Inline pass (_run_policy_validators): validator that raises → surfaces
  policy_validator_error hard violation instead of swallowing.
- Inline pass: taxable line with blank tax_treatment → invalid_tax_code hard
  violation.
"""

from __future__ import annotations

import pytest

from ledgr_agent.callbacks.validate_output import validate_output_after_tool


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


_DOC_TOOL = _FakeTool("process_document_batch")
_OTHER_TOOL = _FakeTool("inspect_market_policy")


def _make_response(
    *,
    status: str = "success",
    review_requests: list[dict] | None = None,
    validation_summary: dict | None = None,
) -> dict:
    return {
        "status": status,
        "review_requests": review_requests or [],
        "validation_summary": validation_summary or {},
    }


def _hard_req(violation_id: str = "gst_claimed_by_non_registered_client") -> dict:
    return {"id": violation_id, "severity": "hard_review", "file_name": "inv.pdf", "message": violation_id}


# ---------------------------------------------------------------------------
# Callback tests
# ---------------------------------------------------------------------------


def test_hard_violation_with_success_status_flips_to_needs_review() -> None:
    response = _make_response(status="success", review_requests=[_hard_req()])
    result = validate_output_after_tool(
        tool=_DOC_TOOL, args={}, tool_context=None, tool_response=response
    )
    assert result is not None, "callback should return a patched dict"
    assert result["status"] == "needs_review"
    assert result["validation_summary"]["policy_enforcement"] == "failed_open_detected"


def test_hard_violation_strict_mode_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGR_VALIDATE_STRICT", "1")
    response = _make_response(status="success", review_requests=[_hard_req()])
    with pytest.raises(ValueError, match="LEDGR_VALIDATE_STRICT"):
        validate_output_after_tool(
            tool=_DOC_TOOL, args={}, tool_context=None, tool_response=response
        )


def test_clean_response_returns_none() -> None:
    response = _make_response(status="success", review_requests=[])
    result = validate_output_after_tool(
        tool=_DOC_TOOL, args={}, tool_context=None, tool_response=response
    )
    assert result is None


def test_hard_violation_already_reflected_in_status_returns_none() -> None:
    # status is already "needs_review" — callback should not double-report.
    response = _make_response(status="needs_review", review_requests=[_hard_req()])
    result = validate_output_after_tool(
        tool=_DOC_TOOL, args={}, tool_context=None, tool_response=response
    )
    assert result is None


def test_other_tool_name_returns_none() -> None:
    response = _make_response(status="success", review_requests=[_hard_req()])
    result = validate_output_after_tool(
        tool=_OTHER_TOOL, args={}, tool_context=None, tool_response=response
    )
    assert result is None


def test_non_dict_response_returns_none() -> None:
    result = validate_output_after_tool(
        tool=_DOC_TOOL, args={}, tool_context=None, tool_response="not a dict"
    )
    assert result is None


def test_policy_validator_error_id_also_flips_status() -> None:
    response = _make_response(
        status="success", review_requests=[_hard_req("policy_validator_error")]
    )
    result = validate_output_after_tool(
        tool=_DOC_TOOL, args={}, tool_context=None, tool_response=response
    )
    assert result is not None
    assert result["status"] == "needs_review"


def test_invalid_tax_code_id_also_flips_status() -> None:
    response = _make_response(
        status="success", review_requests=[_hard_req("invalid_tax_code")]
    )
    result = validate_output_after_tool(
        tool=_DOC_TOOL, args={}, tool_context=None, tool_response=response
    )
    assert result is not None
    assert result["status"] == "needs_review"


def test_patched_response_is_a_copy_not_mutation() -> None:
    """Callback must return a deep copy — original dict is unmodified."""
    response = _make_response(status="success", review_requests=[_hard_req()])
    result = validate_output_after_tool(
        tool=_DOC_TOOL, args={}, tool_context=None, tool_response=response
    )
    assert result is not None
    assert response["status"] == "success", "original must not be mutated"


def test_strict_mode_env_false_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGR_VALIDATE_STRICT", "0")
    response = _make_response(status="success", review_requests=[_hard_req()])
    result = validate_output_after_tool(
        tool=_DOC_TOOL, args={}, tool_context=None, tool_response=response
    )
    assert result is not None
    assert result["status"] == "needs_review"


# ---------------------------------------------------------------------------
# Inline pass (_run_policy_validators) tests
# ---------------------------------------------------------------------------


def test_inline_pass_validator_raises_surfaces_policy_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """When validate_gst_registration_gate raises, _run_policy_validators must
    return a policy_validator_error violation instead of swallowing the error."""
    from unittest.mock import MagicMock

    import ledgr_agent.tools.document_tools as dt

    # Patch at the call site (the name already imported into document_tools module).
    monkeypatch.setattr(dt, "validate_gst_registration_gate", MagicMock(side_effect=RuntimeError("boom")))

    # Build a minimal engine_result with one doc that has a normalized invoice
    normalized = MagicMock()
    normalized.doc_gst_total = 10.0
    normalized.doc_type = "purchase"
    normalized.lines = []

    doc = MagicMock()
    doc.path = "/tmp/inv.pdf"
    doc.normalized = normalized

    engine_result = MagicMock()
    engine_result.docs = [doc]

    violations = dt._run_policy_validators(engine_result, region="SG", tax_registered=True)

    assert len(violations) == 1, f"expected 1 violation, got {violations}"
    assert violations[0]["id"] == "policy_validator_error"
    assert violations[0]["severity"] == "hard_review"
    assert "boom" in violations[0]["message"]


def test_inline_pass_taxable_line_blank_treatment_surfaces_invalid_tax_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A taxable line with blank tax_treatment must produce an invalid_tax_code
    hard violation."""
    from unittest.mock import MagicMock

    import ledgr_agent.tools.document_tools as dt

    # Patch at the call site — the name bound in document_tools at import time.
    monkeypatch.setattr(dt, "validate_gst_registration_gate", MagicMock(return_value=[]))

    # Taxable line with blank tax_treatment
    line = MagicMock()
    line.gst_amount = 9.0   # non-zero → taxable
    line.tax_treatment = ""  # blank → should flag
    line.description = "Consulting services"

    normalized = MagicMock()
    normalized.doc_gst_total = 9.0
    normalized.doc_type = "purchase"
    normalized.lines = [line]

    doc = MagicMock()
    doc.path = "/tmp/inv2.pdf"
    doc.normalized = normalized

    engine_result = MagicMock()
    engine_result.docs = [doc]

    violations = dt._run_policy_validators(engine_result, region="SG", tax_registered=True)

    invalid_tc = [v for v in violations if v["id"] == "invalid_tax_code"]
    assert len(invalid_tc) == 1, f"expected 1 invalid_tax_code violation, got {violations}"
    assert invalid_tc[0]["severity"] == "hard_review"
    assert "Consulting services" in invalid_tc[0]["message"]


def test_inline_pass_nt_line_no_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A line with gst_amount=0 (NT/ZR) must NOT produce an invalid_tax_code
    violation even when tax_treatment is blank."""
    from unittest.mock import MagicMock

    import ledgr_agent.tools.document_tools as dt

    monkeypatch.setattr(dt, "validate_gst_registration_gate", MagicMock(return_value=[]))

    line = MagicMock()
    line.gst_amount = 0.0   # zero → not taxable
    line.tax_treatment = ""
    line.description = "Zero-rated item"

    normalized = MagicMock()
    normalized.doc_gst_total = 0.0
    normalized.doc_type = "purchase"
    normalized.lines = [line]

    doc = MagicMock()
    doc.path = "/tmp/inv3.pdf"
    doc.normalized = normalized

    engine_result = MagicMock()
    engine_result.docs = [doc]

    violations = dt._run_policy_validators(engine_result, region="SG", tax_registered=True)

    assert violations == [], f"expected no violations for zero-gst line, got {violations}"


# ---------------------------------------------------------------------------
# Fix 1 regression tests: partial / error / blocked branches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["partial", "error"])
def test_hard_violation_with_partial_or_error_status_annotates_but_keeps_status(status: str) -> None:
    """Normal mode: hard violation present in partial/error batch → keep that
    status but annotate policy_enforcement='failed_open_detected'."""
    response = _make_response(status=status, review_requests=[_hard_req()])
    result = validate_output_after_tool(
        tool=_DOC_TOOL, args={}, tool_context=None, tool_response=response
    )
    assert result is not None, f"callback must return a patched dict for status={status!r}"
    assert result["status"] == status, "status must NOT be downgraded to needs_review"
    assert result["validation_summary"]["policy_enforcement"] == "failed_open_detected"


@pytest.mark.parametrize("status", ["partial", "error"])
def test_hard_violation_partial_or_error_strict_raises(
    status: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """STRICT mode: hard violation in partial/error batch → raises ValueError."""
    monkeypatch.setenv("LEDGR_VALIDATE_STRICT", "1")
    response = _make_response(status=status, review_requests=[_hard_req()])
    with pytest.raises(ValueError, match="LEDGR_VALIDATE_STRICT"):
        validate_output_after_tool(
            tool=_DOC_TOOL, args={}, tool_context=None, tool_response=response
        )


def test_blocked_status_no_hard_violation_passthrough() -> None:
    """status='blocked' with no hard violation → pass-through (returns None)."""
    response = _make_response(status="blocked", review_requests=[])
    result = validate_output_after_tool(
        tool=_DOC_TOOL, args={}, tool_context=None, tool_response=response
    )
    assert result is None


def test_needs_review_with_hard_violation_passthrough() -> None:
    """status='needs_review' + hard violation → already reflected, returns None."""
    response = _make_response(status="needs_review", review_requests=[_hard_req()])
    result = validate_output_after_tool(
        tool=_DOC_TOOL, args={}, tool_context=None, tool_response=response
    )
    assert result is None


def test_inline_pass_policy_load_error_surfaces_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """When load_jurisdiction_policy raises, a policy_validator_error must be
    returned (not swallowed as an empty list)."""
    from unittest.mock import MagicMock

    import ledgr_agent.tools.document_tools as dt

    # Patch at the call site — the name bound in document_tools at import time.
    monkeypatch.setattr(dt, "load_jurisdiction_policy", MagicMock(side_effect=ValueError("bad yaml")))

    engine_result = MagicMock()
    engine_result.docs = []

    violations = dt._run_policy_validators(engine_result, region="SG", tax_registered=True)

    assert len(violations) == 1
    assert violations[0]["id"] == "policy_validator_error"
    assert "bad yaml" in violations[0]["message"]
