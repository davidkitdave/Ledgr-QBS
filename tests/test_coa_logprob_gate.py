"""Unit tests for COA logprob confidence gate (WS-3.3).

Pure-function tests — no Gemini, no network.
"""

from __future__ import annotations

from types import SimpleNamespace

from invoice_processing.export.categorizer import (
    COA_MIN_AVG_LOGPROBS,
    COA_MIN_LOGPROB_MARGIN,
    evaluate_coa_logprob_gate,
    extract_logprob_metrics,
)


class TestEvaluateCoaLogprobGate:
    def test_high_avg_and_wide_margin_not_flagged(self):
        flagged, reason = evaluate_coa_logprob_gate(-0.1, 1.5)
        assert flagged is False
        assert reason == ""

    def test_low_avg_logprobs_flagged(self):
        flagged, reason = evaluate_coa_logprob_gate(-2.0, 1.5)
        assert flagged is True
        assert "low_avg_logprobs" in reason

    def test_narrow_margin_flagged(self):
        flagged, reason = evaluate_coa_logprob_gate(-0.1, 0.05)
        assert flagged is True
        assert "narrow_margin" in reason

    def test_missing_avg_logprobs_flagged(self):
        flagged, reason = evaluate_coa_logprob_gate(None, 1.5)
        assert flagged is True
        assert "missing_logprobs" in reason

    def test_missing_margin_flagged(self):
        flagged, reason = evaluate_coa_logprob_gate(-0.1, None)
        assert flagged is True
        assert "missing_logprobs" in reason

    def test_threshold_boundary_avg_not_flagged(self):
        flagged, _ = evaluate_coa_logprob_gate(COA_MIN_AVG_LOGPROBS, 1.0)
        assert flagged is False

    def test_threshold_boundary_margin_not_flagged(self):
        flagged, _ = evaluate_coa_logprob_gate(-0.1, COA_MIN_LOGPROB_MARGIN)
        assert flagged is False


class TestExtractLogprobMetrics:
    def test_extracts_from_candidate_namespace(self):
        top_step = SimpleNamespace(
            candidates=[
                SimpleNamespace(log_probability=-0.2, token="6001"),
                SimpleNamespace(log_probability=-1.5, token="6200"),
            ]
        )
        candidate = SimpleNamespace(
            avg_logprobs=-0.3,
            logprobs_result=SimpleNamespace(top_candidates=[top_step]),
        )
        resp = SimpleNamespace(candidates=[candidate])

        avg, margin = extract_logprob_metrics(resp)
        assert avg == -0.3
        assert margin == 1.3  # -0.2 - (-1.5)

    def test_missing_candidates_returns_none(self):
        resp = SimpleNamespace(candidates=[])
        avg, margin = extract_logprob_metrics(resp)
        assert avg is None
        assert margin is None

    def test_single_top_candidate_no_margin(self):
        top_step = SimpleNamespace(
            candidates=[SimpleNamespace(log_probability=-0.2, token="6001")]
        )
        candidate = SimpleNamespace(
            avg_logprobs=-0.3,
            logprobs_result=SimpleNamespace(top_candidates=[top_step]),
        )
        resp = SimpleNamespace(candidates=[candidate])

        avg, margin = extract_logprob_metrics(resp)
        assert avg == -0.3
        assert margin is None
