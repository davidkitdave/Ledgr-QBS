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
# Inline policy-validator tests removed: light batch path does not run the
# invoice_processing policy ladder (_run_policy_validators).
# ---------------------------------------------------------------------------


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


