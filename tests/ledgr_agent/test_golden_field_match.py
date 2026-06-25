"""Hermetic unit tests for the golden v2 field-match scorer.

All data is 100% synthetic — no real client names, no model calls, no network.
Fixtures build BatchResult payloads with the REAL runtime key shapes:
- top-level ``documents_processed``
- ``credits`` dict with ``credits_used`` / ``credit_status``
- ``posted_documents`` list with ``path`` / ``file_name`` / ``doc_type`` / ``total``
- ``export_rows`` not used here (TODO 0.4 — lines always [] from live projection)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers to build synthetic trace instances and BatchResult payloads
# ---------------------------------------------------------------------------


def _make_instance(batch_payload: dict | None) -> dict:
    """Wrap a BatchResult dict in a minimal agents-cli trace instance."""
    if batch_payload is None:
        return {
            "agent_data": {
                "turns": [
                    {
                        "events": [
                            {
                                "content": {
                                    "parts": [{"text": "plain text, no tool call"}]
                                }
                            }
                        ]
                    }
                ]
            }
        }
    return {
        "agent_data": {
            "turns": [
                {
                    "events": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "function_response": {
                                            "name": "process_document_batch",
                                            "response": batch_payload,
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    }


def _make_batch(
    *,
    filename: str,
    doc_type: str = "invoice",
    total: float | None = 100.00,
    credits_used: int = 1,
    credit_status: str = "charged",
    documents_processed: int = 1,
    extra_posted: list[dict] | None = None,
) -> dict:
    """Build a minimal real-shape BatchResult dict for testing.

    ``posted_documents`` drives the projection; ``per_file`` carries ONLY the
    documented fields (no vendor/currency/lines).
    """
    posted: list[dict] = [
        {
            "path": f"/uploads/{filename}",
            "file_name": filename,
            "doc_type": doc_type,
            "direction": "purchase",
            "reconciled": False,
            "workbook": "Ledger_FY2025",
            "sheet": "Invoices",
            "note": None,
            "invoice_number": "INV-001",
            "invoice_date": "2025-12-01",
            "total": total,
        }
    ]
    if extra_posted:
        posted.extend(extra_posted)

    return {
        "status": "success",
        "client_id": "test-client",
        "firm_id": None,
        "documents_processed": documents_processed,
        "documents_requested": documents_processed,
        "documents_skipped_before_llm": 0,
        "credits": {
            "credits_estimated": credits_used,
            "credits_used": credits_used,
            "credits_remaining": 100,
            "credit_status": credit_status,
        },
        "per_file": [
            {
                "path": f"/uploads/{filename}",
                "file_name": filename,
                "doc_type": doc_type,
                "direction": "purchase",
                "reconciled": False,
                "workbook": "Ledger_FY2025",
                "sheet": "Invoices",
                "note": None,
            }
        ],
        "posted_documents": posted,
        "skipped_documents": [],
        "export_rows": [],
    }


def _write_manifest(tmp_path: Path, data: dict) -> str:
    """Write a synthetic golden v2 manifest JSON and return its path string."""
    p = tmp_path / "golden_v2_sample.json"
    p.write_text(json.dumps(data))
    return str(p)


# ---------------------------------------------------------------------------
# Shared synthetic golden manifest (covers most branches inline)
# ---------------------------------------------------------------------------

MY_FILE = "alpha-invoices.pdf"
SG_FILE = "gamma-telco.pdf"
BANK_FILE = "delta-bank.pdf"

_GOLDEN_MY_DOC = {
    "file": MY_FILE,
    "doc_type": "invoice",
    "vendor": "Vendor Alpha Sdn Bhd",
    "currency": "MYR",
    "total": 1060.00,
    "tax_amount": 60.00,
    "creditor_code": "400-A0001",
    "lines": [
        {
            "tax_code": "SR",
            "coa_code": "610-000",
            "erp_codes": {"autocount": "SV-8", "sql_account": "SV"},
        }
    ],
}

_GOLDEN_SG_DOC = {
    "file": SG_FILE,
    "doc_type": "invoice",
    "vendor": "Telco Gamma Pte Ltd",
    "currency": "SGD",
    "total": 321.00,
    "tax_amount": 21.00,
    "creditor_code": "",
    "lines": [
        {
            "tax_code": "SR",
            "coa_code": "640-000",
            "erp_codes": {
                "autocount": "BLANK(hole B.1)",
                "sql_account": "BLANK(hole B.1)",
            },
        }
    ],
}

_GOLDEN_BANK_DOC = {
    "file": BANK_FILE,
    "doc_type": "bank_statement",
    "vendor": "Delta Bank Bhd",
    "currency": "MYR",
    "total": None,
    "tax_amount": None,
    "creditor_code": "",
    "lines": [],
}

_MINIMAL_GOLDEN = {
    "file_expectations": {
        MY_FILE: {
            "expected_doc_count": 1,
            "expected_billable_credits": 1,
            "jurisdiction": "MY",
            "page_count": 1,
            "note": "",
        },
        SG_FILE: {
            "expected_doc_count": 1,
            "expected_billable_credits": 3,
            "jurisdiction": "SG",
            "page_count": 3,
            "note": "",
        },
        BANK_FILE: {
            "expected_doc_count": 1,
            "expected_billable_credits": 10,
            "jurisdiction": "MY",
            "page_count": 10,
            "note": "",
        },
    },
    "documents": [_GOLDEN_MY_DOC, _GOLDEN_SG_DOC, _GOLDEN_BANK_DOC],
}


# ---------------------------------------------------------------------------
# Autouse fixture: clear LEDGR_GOLDEN_MANIFEST between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_manifest_env(monkeypatch):
    monkeypatch.delenv("LEDGR_GOLDEN_MANIFEST", raising=False)


# ---------------------------------------------------------------------------
# Test: load_golden_manifest
# ---------------------------------------------------------------------------


class TestLoadGoldenManifest:
    def test_returns_full_object(self, tmp_path):
        from ledgr_agent.metrics.golden_field_match import load_golden_manifest

        path = _write_manifest(tmp_path, _MINIMAL_GOLDEN)
        result = load_golden_manifest(path)
        assert "file_expectations" in result
        assert "documents" in result

    def test_accepts_path_object(self, tmp_path):
        from ledgr_agent.metrics.golden_field_match import load_golden_manifest

        p = tmp_path / "golden_v2_sample.json"
        p.write_text(json.dumps(_MINIMAL_GOLDEN))
        result = load_golden_manifest(p)
        assert isinstance(result, dict)
        assert MY_FILE in result["file_expectations"]


# ---------------------------------------------------------------------------
# Test: latest_batch_result
# ---------------------------------------------------------------------------


class TestLatestBatchResult:
    def test_returns_payload_when_present(self):
        from ledgr_agent.metrics.golden_field_match import latest_batch_result

        batch = _make_batch(filename=MY_FILE)
        instance = _make_instance(batch)
        result = latest_batch_result(instance)
        assert result is not None
        assert result["status"] == "success"

    def test_returns_none_when_no_tool_response(self):
        from ledgr_agent.metrics.golden_field_match import latest_batch_result

        instance = _make_instance(None)
        assert latest_batch_result(instance) is None

    def test_returns_last_when_multiple(self):
        from ledgr_agent.metrics.golden_field_match import latest_batch_result

        batch_a = {**_make_batch(filename=MY_FILE), "marker": "first"}
        batch_b = {**_make_batch(filename=MY_FILE), "marker": "second"}
        instance = {
            "agent_data": {
                "turns": [
                    {
                        "events": [
                            {
                                "content": {
                                    "parts": [
                                        {
                                            "function_response": {
                                                "name": "process_document_batch",
                                                "response": batch_a,
                                            }
                                        }
                                    ]
                                }
                            },
                            {
                                "content": {
                                    "parts": [
                                        {
                                            "function_response": {
                                                "name": "process_document_batch",
                                                "response": batch_b,
                                            }
                                        }
                                    ]
                                }
                            },
                        ]
                    }
                ]
            }
        }
        assert latest_batch_result(instance)["marker"] == "second"


# ---------------------------------------------------------------------------
# Test: project_batch
# ---------------------------------------------------------------------------


class TestProjectBatch:
    def test_extracts_documents_processed(self):
        from ledgr_agent.metrics.golden_field_match import project_batch

        batch = _make_batch(filename=MY_FILE, documents_processed=3)
        p = project_batch(batch)
        assert p["documents_processed"] == 3

    def test_extracts_credits_used_and_status(self):
        from ledgr_agent.metrics.golden_field_match import project_batch

        batch = _make_batch(filename=MY_FILE, credits_used=5, credit_status="charged")
        p = project_batch(batch)
        assert p["credits_used"] == 5
        assert p["credit_status"] == "charged"

    def test_per_file_doc_counts_from_posted_documents(self):
        from ledgr_agent.metrics.golden_field_match import project_batch

        # Two posted docs from the same file
        batch = _make_batch(
            filename=MY_FILE,
            extra_posted=[
                {
                    "path": f"/uploads/{MY_FILE}",
                    "file_name": MY_FILE,
                    "doc_type": "invoice",
                    "invoice_number": "INV-002",
                    "invoice_date": "2025-12-02",
                    "total": 200.00,
                }
            ],
        )
        p = project_batch(batch)
        assert p["per_file_doc_counts"][MY_FILE] == 2

    def test_lines_always_empty_live_projection(self):
        from ledgr_agent.metrics.golden_field_match import project_batch

        batch = _make_batch(filename=MY_FILE)
        p = project_batch(batch)
        docs = p["docs_by_file"][MY_FILE]
        assert all(d["lines"] == [] for d in docs)

    def test_missing_credits_block_defaults(self):
        from ledgr_agent.metrics.golden_field_match import project_batch

        batch = _make_batch(filename=MY_FILE)
        batch["credits"] = {}
        p = project_batch(batch)
        assert p["credits_used"] == 0
        assert p["credit_status"] == "not_checked"


# ---------------------------------------------------------------------------
# Test: score_doc_count
# ---------------------------------------------------------------------------


class TestScoreDocCount:
    def _projection(self, filename: str, count: int) -> dict:
        return {
            "per_file_doc_counts": {filename: count},
            "credits_used": 1,
            "credit_status": "charged",
            "docs_by_file": {},
        }

    def test_match_scores_1(self):
        from ledgr_agent.metrics.golden_field_match import score_doc_count

        file_exp = {"expected_doc_count": 2, "expected_billable_credits": 2}
        proj = self._projection(MY_FILE, 2)
        result = score_doc_count(file_exp, proj, MY_FILE)
        assert result["score"] == 1.0
        assert "2/2" in result["explanation"]

    def test_mismatch_scores_0(self):
        from ledgr_agent.metrics.golden_field_match import score_doc_count

        file_exp = {"expected_doc_count": 14, "expected_billable_credits": 14}
        # Engine only produced 2
        proj = self._projection(MY_FILE, 2)
        result = score_doc_count(file_exp, proj, MY_FILE)
        assert result["score"] == 0.0
        assert "got 2" in result["explanation"]
        assert "expected 14" in result["explanation"]

    def test_file_absent_from_projection_scores_0(self):
        from ledgr_agent.metrics.golden_field_match import score_doc_count

        file_exp = {"expected_doc_count": 1, "expected_billable_credits": 1}
        proj = self._projection("other.pdf", 1)
        result = score_doc_count(file_exp, proj, MY_FILE)
        assert result["score"] == 0.0


# ---------------------------------------------------------------------------
# Test: score_credits
# ---------------------------------------------------------------------------


class TestScoreCredits:
    def _projection(self, credits_used: int, credit_status: str = "charged") -> dict:
        return {
            "credits_used": credits_used,
            "credit_status": credit_status,
            "per_file_doc_counts": {},
            "docs_by_file": {},
        }

    def test_match_scores_1(self):
        from ledgr_agent.metrics.golden_field_match import score_credits

        file_exp = {"expected_billable_credits": 3}
        result = score_credits(file_exp, self._projection(3))
        assert result["score"] == 1.0

    def test_mismatch_scores_0(self):
        from ledgr_agent.metrics.golden_field_match import score_credits

        file_exp = {"expected_billable_credits": 10}
        result = score_credits(file_exp, self._projection(5))
        assert result["score"] == 0.0
        assert "used 5" in result["explanation"]

    def test_not_billable_returns_none(self):
        from ledgr_agent.metrics.golden_field_match import score_credits

        file_exp = {"expected_billable_credits": 1}
        result = score_credits(file_exp, self._projection(0, "not_billable"))
        assert result["score"] is None
        assert "not billable" in result["explanation"]


# ---------------------------------------------------------------------------
# Test: score_classification
# ---------------------------------------------------------------------------


class TestScoreClassification:
    def test_match_scores_1(self):
        from ledgr_agent.metrics.golden_field_match import score_classification

        g = {"doc_type": "invoice"}
        a = {"doc_type": "invoice"}
        assert score_classification(g, a)["score"] == 1.0

    def test_mismatch_scores_0(self):
        from ledgr_agent.metrics.golden_field_match import score_classification

        g = {"doc_type": "invoice"}
        a = {"doc_type": "receipt"}
        result = score_classification(g, a)
        assert result["score"] == 0.0
        assert "invoice" in result["explanation"]

    def test_bank_statement_match(self):
        from ledgr_agent.metrics.golden_field_match import score_classification

        g = {"doc_type": "bank_statement"}
        a = {"doc_type": "bank_statement"}
        assert score_classification(g, a)["score"] == 1.0


# ---------------------------------------------------------------------------
# Test: score_fields
# ---------------------------------------------------------------------------


class TestScoreFields:
    def test_all_fields_match(self):
        from ledgr_agent.metrics.golden_field_match import score_fields

        g = {"vendor": "Alpha Sdn Bhd", "currency": "MYR", "total": 100.0, "tax_amount": 8.0}
        a = {"vendor": "Alpha Sdn Bhd", "currency": "MYR", "total": 100.0, "tax_amount": 8.0}
        assert score_fields(g, a)["score"] == 1.0

    def test_vendor_case_insensitive(self):
        from ledgr_agent.metrics.golden_field_match import score_fields

        g = {"vendor": "Alpha Sdn Bhd", "currency": "MYR", "total": 100.0, "tax_amount": 8.0}
        a = {"vendor": "alpha sdn bhd", "currency": "MYR", "total": 100.0, "tax_amount": 8.0}
        assert score_fields(g, a)["score"] == 1.0

    def test_vendor_trimmed(self):
        from ledgr_agent.metrics.golden_field_match import score_fields

        g = {"vendor": "Alpha Sdn Bhd", "currency": "MYR", "total": 100.0, "tax_amount": 8.0}
        a = {"vendor": "  Alpha Sdn Bhd  ", "currency": "MYR", "total": 100.0, "tax_amount": 8.0}
        assert score_fields(g, a)["score"] == 1.0

    def test_wrong_total_cents_scores_three_quarters(self):
        from ledgr_agent.metrics.golden_field_match import score_fields

        g = {"vendor": "Alpha Sdn Bhd", "currency": "MYR", "total": 100.0, "tax_amount": 8.0}
        a = {"vendor": "Alpha Sdn Bhd", "currency": "MYR", "total": 101.0, "tax_amount": 8.0}
        result = score_fields(g, a)
        assert result["score"] == pytest.approx(0.75)
        assert "total=FAIL" in result["explanation"]

    def test_null_golden_total_excluded_from_scoring(self):
        from ledgr_agent.metrics.golden_field_match import score_fields

        # total=None in golden → only vendor/currency/tax_amount scored (3 fields)
        g = {"vendor": "Alpha Sdn Bhd", "currency": "MYR", "total": None, "tax_amount": 8.0}
        a = {"vendor": "Alpha Sdn Bhd", "currency": "MYR", "total": 9999.0, "tax_amount": 8.0}
        result = score_fields(g, a)
        assert result["score"] == pytest.approx(1.0)  # 3/3: vendor+currency+tax_amount

    def test_no_golden_fields_returns_na(self):
        from ledgr_agent.metrics.golden_field_match import score_fields

        g = {"vendor": None, "currency": None, "total": None, "tax_amount": None}
        a = {"vendor": "Whatever", "currency": "MYR", "total": 100.0, "tax_amount": 8.0}
        result = score_fields(g, a)
        assert result["score"] is None
        assert "N/A" in result["explanation"]

    def test_wrong_currency_scores_three_quarters(self):
        from ledgr_agent.metrics.golden_field_match import score_fields

        g = {"vendor": "Alpha Sdn Bhd", "currency": "MYR", "total": 100.0, "tax_amount": 8.0}
        a = {"vendor": "Alpha Sdn Bhd", "currency": "SGD", "total": 100.0, "tax_amount": 8.0}
        result = score_fields(g, a)
        assert result["score"] == pytest.approx(0.75)
        assert "currency=FAIL" in result["explanation"]

    def test_bank_statement_null_total_and_tax(self):
        from ledgr_agent.metrics.golden_field_match import score_fields

        # Bank statement has null total/tax_amount in golden — only vendor+currency scored
        g = {"vendor": "Delta Bank Bhd", "currency": "MYR", "total": None, "tax_amount": None}
        a = {"vendor": "Delta Bank Bhd", "currency": "MYR", "total": None, "tax_amount": None}
        result = score_fields(g, a)
        assert result["score"] == pytest.approx(1.0)

    def test_vendor_currency_absent_from_actual_skipped_not_miss(self):
        """Fix 3(a): golden has vendor+currency+total+tax; actual_doc lacks vendor &
        currency keys (structurally absent from live projection).  Only total+tax
        are scored → 2/2 = 1.0, NOT 2/4.
        """
        from ledgr_agent.metrics.golden_field_match import score_fields

        g = {
            "vendor": "Alpha Sdn Bhd",
            "currency": "MYR",
            "total": 100.0,
            "tax_amount": 8.0,
        }
        # vendor and currency keys are completely absent from actual_doc
        a = {"total": 100.0, "tax_amount": 8.0}
        result = score_fields(g, a)
        assert result["score"] == pytest.approx(1.0), (
            f"Expected 1.0 (only total+tax scored), got {result['score']}. "
            f"Explanation: {result['explanation']}"
        )
        # Only 2 fields evaluated
        assert "2/2" in result["explanation"]

    def test_vendor_key_present_wrong_value_counts_as_miss(self):
        """Fix 3(b): actual_doc HAS the vendor key but with a wrong value → miss."""
        from ledgr_agent.metrics.golden_field_match import score_fields

        g = {
            "vendor": "Alpha Sdn Bhd",
            "currency": "MYR",
            "total": 100.0,
            "tax_amount": 8.0,
        }
        # vendor key IS present but wrong; currency key IS present and correct
        a = {
            "vendor": "Wrong Vendor Ltd",
            "currency": "MYR",
            "total": 100.0,
            "tax_amount": 8.0,
        }
        result = score_fields(g, a)
        assert result["score"] == pytest.approx(0.75), (
            f"Expected 0.75 (vendor=FAIL, rest PASS), got {result['score']}. "
            f"Explanation: {result['explanation']}"
        )
        assert "vendor=FAIL" in result["explanation"]


# ---------------------------------------------------------------------------
# Test: score_tax_coa
# ---------------------------------------------------------------------------


class TestScoreTaxCoa:
    def _make_golden_doc(self, lines: list[dict]) -> dict:
        return {**_GOLDEN_MY_DOC, "lines": lines}

    def _make_actual_doc(self, lines: list[dict]) -> dict:
        return {"doc_type": "invoice", "total": 100.0, "lines": lines}

    def test_full_match_scores_1(self):
        from ledgr_agent.metrics.golden_field_match import score_tax_coa

        g = self._make_golden_doc([
            {
                "tax_code": "SR",
                "coa_code": "610-000",
                "erp_codes": {"autocount": "SV-8", "sql_account": "SV"},
            }
        ])
        a = self._make_actual_doc([
            {
                "tax_code": "SR",
                "coa_code": "610-000",
                "erp_codes": {"autocount": "SV-8", "sql_account": "SV"},
            }
        ])
        assert score_tax_coa(g, a, "autocount")["score"] == 1.0
        assert score_tax_coa(g, a, "sql_account")["score"] == 1.0

    def test_partial_match_scores_half(self):
        from ledgr_agent.metrics.golden_field_match import score_tax_coa

        g = self._make_golden_doc([
            {"tax_code": "SR", "coa_code": "610-000", "erp_codes": {"autocount": "SV-8"}},
            {"tax_code": "NT", "coa_code": "620-000", "erp_codes": {"autocount": "NT"}},
        ])
        a = self._make_actual_doc([
            {"tax_code": "SR", "coa_code": "610-000", "erp_codes": {"autocount": "SV-8"}},  # match
            {"tax_code": "SR", "coa_code": "610-000", "erp_codes": {"autocount": "SV-8"}},  # mismatch
        ])
        result = score_tax_coa(g, a, "autocount")
        assert result["score"] == pytest.approx(0.5)
        assert "1/2" in result["explanation"]

    def test_actual_lines_absent_returns_na(self):
        from ledgr_agent.metrics.golden_field_match import score_tax_coa

        g = self._make_golden_doc([
            {"tax_code": "SR", "coa_code": "610-000", "erp_codes": {"autocount": "SV-8"}},
        ])
        a = {"doc_type": "invoice", "total": 100.0, "lines": []}
        result = score_tax_coa(g, a, "autocount")
        assert result["score"] is None
        assert "no actual line data" in result["explanation"]

    def test_no_golden_lines_for_erp_returns_na(self):
        from ledgr_agent.metrics.golden_field_match import score_tax_coa

        # golden line has erp_codes but not for sql_account
        g = self._make_golden_doc([
            {"tax_code": "SR", "coa_code": "610-000", "erp_codes": {"autocount": "SV-8"}},
        ])
        a = self._make_actual_doc([
            {"tax_code": "SR", "coa_code": "610-000", "erp_codes": {"autocount": "SV-8"}},
        ])
        result = score_tax_coa(g, a, "sql_account")
        assert result["score"] is None
        assert "N/A" in result["explanation"]

    def test_blank_hole_golden_matches_empty_actual(self):
        """'BLANK(hole B.1)' in golden ERP value matches empty/None actual."""
        from ledgr_agent.metrics.golden_field_match import score_tax_coa

        g = self._make_golden_doc([
            {
                "tax_code": "SR",
                "coa_code": "640-000",
                "erp_codes": {"autocount": "BLANK(hole B.1)"},
            }
        ])
        # Actual has empty string for autocount — should match
        a = self._make_actual_doc([
            {"tax_code": "SR", "coa_code": "640-000", "erp_codes": {"autocount": ""}},
        ])
        result = score_tax_coa(g, a, "autocount")
        assert result["score"] == 1.0

    def test_blank_hole_golden_matches_none_actual(self):
        """'BLANK(hole B.1)' in golden ERP value also matches None actual."""
        from ledgr_agent.metrics.golden_field_match import score_tax_coa

        g = self._make_golden_doc([
            {
                "tax_code": "SR",
                "coa_code": "640-000",
                "erp_codes": {"autocount": "BLANK(hole B.1)"},
            }
        ])
        a = self._make_actual_doc([
            {"tax_code": "SR", "coa_code": "640-000", "erp_codes": {"autocount": None}},
        ])
        result = score_tax_coa(g, a, "autocount")
        assert result["score"] == 1.0

    def test_blank_hole_golden_does_not_match_real_value(self):
        """'BLANK(hole B.1)' in golden does NOT match a non-empty actual ERP code."""
        from ledgr_agent.metrics.golden_field_match import score_tax_coa

        g = self._make_golden_doc([
            {
                "tax_code": "SR",
                "coa_code": "640-000",
                "erp_codes": {"autocount": "BLANK(hole B.1)"},
            }
        ])
        a = self._make_actual_doc([
            {"tax_code": "SR", "coa_code": "640-000", "erp_codes": {"autocount": "SV-8"}},
        ])
        result = score_tax_coa(g, a, "autocount")
        assert result["score"] == 0.0

    def test_bank_statement_no_lines_returns_na(self):
        from ledgr_agent.metrics.golden_field_match import score_tax_coa

        # golden bank doc has empty lines
        g = {**_GOLDEN_BANK_DOC}
        a = {"doc_type": "bank_statement", "total": None, "lines": []}
        result = score_tax_coa(g, a, "autocount")
        assert result["score"] is None

    def test_index_alignment_skipped_erp_on_line0(self):
        """Fix 1: golden line 0 has no autocount key; line 1 has autocount.
        actual_lines[1] must be paired with golden line 1, NOT actual_lines[0].
        We make actual_lines[0] a value that would produce a WRONG match if the
        old enumerate(scoreable_golden) skew were still present.
        """
        from ledgr_agent.metrics.golden_field_match import score_tax_coa

        golden_doc = {
            "lines": [
                # line 0: only sql_account, NO autocount key
                {
                    "tax_code": "SR",
                    "coa_code": "610-000",
                    "erp_codes": {"sql_account": "SV"},
                },
                # line 1: has autocount
                {
                    "tax_code": "NT",
                    "coa_code": "620-000",
                    "erp_codes": {"autocount": "NT"},
                },
            ]
        }
        # actual_lines[0]: would wrongly match golden line 1 if skew were present
        # actual_lines[1]: correctly matches golden line 1
        actual_doc = {
            "lines": [
                # index 0 — WRONG values for golden line 1 (trap for old bug)
                {
                    "tax_code": "SR",
                    "coa_code": "610-000",
                    "erp_codes": {"autocount": "SV-8"},
                },
                # index 1 — CORRECT values for golden line 1
                {
                    "tax_code": "NT",
                    "coa_code": "620-000",
                    "erp_codes": {"autocount": "NT"},
                },
            ]
        }
        result = score_tax_coa(golden_doc, actual_doc, "autocount")
        # Only 1 scoreable golden line (line 1); it should pair with actual_lines[1]
        # and match → score 1.0.  Under the old bug it would pair with actual_lines[0]
        # and NOT match → score 0.0.
        assert result["score"] == 1.0, (
            f"Expected 1.0 (correct alignment), got {result['score']}. "
            f"Explanation: {result['explanation']}"
        )


# ---------------------------------------------------------------------------
# Test: score_creditor
# ---------------------------------------------------------------------------


class TestScoreCreditor:
    def test_match_scores_1(self):
        from ledgr_agent.metrics.golden_field_match import score_creditor

        g = {"creditor_code": "400-A0001"}
        a = {"creditor_code": "400-A0001"}
        assert score_creditor(g, a)["score"] == 1.0

    def test_mismatch_scores_0(self):
        from ledgr_agent.metrics.golden_field_match import score_creditor

        g = {"creditor_code": "400-A0001"}
        a = {"creditor_code": "400-Z9999"}
        result = score_creditor(g, a)
        assert result["score"] == 0.0
        assert "400-A0001" in result["explanation"]

    def test_empty_golden_creditor_returns_na(self):
        from ledgr_agent.metrics.golden_field_match import score_creditor

        # SG doc has creditor_code=""
        g = {"creditor_code": ""}
        a = {"creditor_code": ""}
        result = score_creditor(g, a)
        assert result["score"] is None
        assert "N/A" in result["explanation"]

    def test_absent_golden_creditor_returns_na(self):
        from ledgr_agent.metrics.golden_field_match import score_creditor

        g = {}
        a = {"creditor_code": "400-A0001"}
        result = score_creditor(g, a)
        assert result["score"] is None

    def test_actual_missing_creditor_returns_na(self):
        from ledgr_agent.metrics.golden_field_match import score_creditor

        g = {"creditor_code": "400-A0001"}
        a = {}  # no creditor_code key at all
        result = score_creditor(g, a)
        assert result["score"] is None
        assert "live gap" in result["explanation"]


# ---------------------------------------------------------------------------
# Test: golden_field_match_code (entry point)
# ---------------------------------------------------------------------------


class TestGoldenFieldMatchCode:
    def test_no_tool_call_scores_zero(self, tmp_path, monkeypatch):
        from ledgr_agent.metrics.golden_field_match import golden_field_match_code

        monkeypatch.setenv("LEDGR_GOLDEN_MANIFEST", _write_manifest(tmp_path, _MINIMAL_GOLDEN))
        result = golden_field_match_code(_make_instance(None))
        assert result["score"] == 0.0
        assert "no process_document_batch result" in result["explanation"]

    def test_file_not_in_golden_scores_zero(self, tmp_path, monkeypatch):
        from ledgr_agent.metrics.golden_field_match import golden_field_match_code

        monkeypatch.setenv("LEDGR_GOLDEN_MANIFEST", _write_manifest(tmp_path, _MINIMAL_GOLDEN))
        batch = _make_batch(filename="unknown-vendor-doc.pdf")
        result = golden_field_match_code(_make_instance(batch))
        assert result["score"] == 0.0
        assert "not in golden v2" in result["explanation"]
        assert "unknown-vendor-doc.pdf" in result["explanation"]

    def test_good_trace_non_zero_overall(self, tmp_path, monkeypatch):
        from ledgr_agent.metrics.golden_field_match import golden_field_match_code

        monkeypatch.setenv("LEDGR_GOLDEN_MANIFEST", _write_manifest(tmp_path, _MINIMAL_GOLDEN))
        # doc_type matches golden (invoice); credits match (1 used, 1 expected)
        # doc_count=1.0, credits=1.0, classification=1.0, fields=N/A (no vendor/currency
        # keys in live projection), tax_coa[*]=N/A (no lines), creditor=N/A (no key).
        # Scored sub-scores: doc_count=1.0, credits=1.0, classification=1.0 → mean=1.0
        batch = _make_batch(filename=MY_FILE, doc_type="invoice", credits_used=1)
        result = golden_field_match_code(_make_instance(batch))
        assert result["score"] is not None
        assert result["score"] > 0.5, (
            f"Expected meaningful score > 0.5, got {result['score']}. "
            f"Explanation: {result['explanation']}"
        )
        assert "overall=" in result["explanation"]

    def test_doc_count_mismatch_drives_score_down(self, tmp_path, monkeypatch):
        from ledgr_agent.metrics.golden_field_match import golden_field_match_code

        golden = {
            "file_expectations": {
                MY_FILE: {
                    "expected_doc_count": 5,
                    "expected_billable_credits": 5,
                    "jurisdiction": "MY",
                    "page_count": 5,
                    "note": "",
                }
            },
            "documents": [_GOLDEN_MY_DOC],
        }
        monkeypatch.setenv("LEDGR_GOLDEN_MANIFEST", _write_manifest(tmp_path, golden))
        # Engine only produced 1 doc
        batch = _make_batch(filename=MY_FILE, doc_type="invoice", credits_used=5)
        result = golden_field_match_code(_make_instance(batch))
        assert result["score"] is not None
        assert result["score"] < 1.0
        assert "doc_count=0.0" in result["explanation"]

    def test_credits_not_billable_excluded_from_mean(self, tmp_path, monkeypatch):
        from ledgr_agent.metrics.golden_field_match import golden_field_match_code

        monkeypatch.setenv("LEDGR_GOLDEN_MANIFEST", _write_manifest(tmp_path, _MINIMAL_GOLDEN))
        batch = _make_batch(filename=MY_FILE, doc_type="invoice", credits_used=0, credit_status="not_billable")
        result = golden_field_match_code(_make_instance(batch))
        # credits N/A should not drag score to 0
        assert "credits=N/A" in result["explanation"]

    def test_env_var_manifest_path_used(self, tmp_path, monkeypatch):
        from ledgr_agent.metrics.golden_field_match import golden_field_match_code

        custom_path = _write_manifest(tmp_path, _MINIMAL_GOLDEN)
        monkeypatch.setenv("LEDGR_GOLDEN_MANIFEST", custom_path)
        batch = _make_batch(filename=MY_FILE)
        result = golden_field_match_code(_make_instance(batch))
        assert result["score"] is not None

    def test_explanation_contains_all_metric_labels(self, tmp_path, monkeypatch):
        from ledgr_agent.metrics.golden_field_match import golden_field_match_code

        monkeypatch.setenv("LEDGR_GOLDEN_MANIFEST", _write_manifest(tmp_path, _MINIMAL_GOLDEN))
        batch = _make_batch(filename=MY_FILE, doc_type="invoice", credits_used=1)
        result = golden_field_match_code(_make_instance(batch))
        exp = result["explanation"]
        assert "doc_count=" in exp
        assert "credits=" in exp
        assert "classification=" in exp
        assert "fields=" in exp
        assert "tax_coa[autocount]=" in exp
        assert "tax_coa[sql_account]=" in exp
        assert "creditor=" in exp
        assert "overall=" in exp

    def test_bank_statement_line_scorers_all_na(self, tmp_path, monkeypatch):
        from ledgr_agent.metrics.golden_field_match import golden_field_match_code

        monkeypatch.setenv("LEDGR_GOLDEN_MANIFEST", _write_manifest(tmp_path, _MINIMAL_GOLDEN))
        # Bank statement: credits = 10 pages
        batch = _make_batch(filename=BANK_FILE, doc_type="bank_statement", credits_used=10, total=None)
        result = golden_field_match_code(_make_instance(batch))
        exp = result["explanation"]
        # Line-based scorers should be N/A (no lines in golden bank doc)
        assert "tax_coa[autocount]=N/A" in exp
        assert "tax_coa[sql_account]=N/A" in exp
        assert "creditor=N/A" in exp

    def test_sg_telco_creditor_na(self, tmp_path, monkeypatch):
        from ledgr_agent.metrics.golden_field_match import golden_field_match_code

        monkeypatch.setenv("LEDGR_GOLDEN_MANIFEST", _write_manifest(tmp_path, _MINIMAL_GOLDEN))
        # SG doc: credits = 3 pages
        batch = _make_batch(filename=SG_FILE, doc_type="invoice", credits_used=3, total=321.0)
        result = golden_field_match_code(_make_instance(batch))
        # SG golden has creditor_code="" → creditor should be N/A
        assert "creditor=N/A" in result["explanation"]

    def test_multi_file_trace_credits_na(self, tmp_path, monkeypatch):
        """Fix 2: when a trace matches TWO golden files, credits must be N/A
        (excluded from the mean) because the batch total cannot be attributed
        to a single file.  doc_count is still scored per file.
        """
        from ledgr_agent.metrics.golden_field_match import golden_field_match_code

        golden = {
            "file_expectations": {
                MY_FILE: {
                    "expected_doc_count": 1,
                    "expected_billable_credits": 1,
                    "jurisdiction": "MY",
                    "page_count": 1,
                    "note": "",
                },
                SG_FILE: {
                    "expected_doc_count": 1,
                    "expected_billable_credits": 3,
                    "jurisdiction": "SG",
                    "page_count": 3,
                    "note": "",
                },
            },
            "documents": [_GOLDEN_MY_DOC, _GOLDEN_SG_DOC],
        }
        monkeypatch.setenv("LEDGR_GOLDEN_MANIFEST", _write_manifest(tmp_path, golden))

        # Batch processed both files
        batch = _make_batch(
            filename=MY_FILE,
            doc_type="invoice",
            credits_used=4,
            documents_processed=2,
            extra_posted=[
                {
                    "path": f"/uploads/{SG_FILE}",
                    "file_name": SG_FILE,
                    "doc_type": "invoice",
                    "invoice_number": "INV-SG-001",
                    "invoice_date": "2025-12-01",
                    "total": 321.0,
                }
            ],
        )
        result = golden_field_match_code(_make_instance(batch))
        exp = result["explanation"]

        # Credits should be N/A for both files (multi-file trace)
        assert "credits=N/A" in exp, f"Expected credits=N/A in: {exp}"
        # doc_count should still be scored per file
        assert "doc_count=" in exp
        # The numeric score should be based only on non-credit sub-scores
        assert result["score"] is not None

    def test_multi_doc_file_two_posted_docs(self, tmp_path, monkeypatch):
        from ledgr_agent.metrics.golden_field_match import golden_field_match_code

        # Golden expects 2 docs from the same file
        golden = {
            "file_expectations": {
                MY_FILE: {
                    "expected_doc_count": 2,
                    "expected_billable_credits": 2,
                    "jurisdiction": "MY",
                    "page_count": 2,
                    "note": "",
                }
            },
            "documents": [
                {**_GOLDEN_MY_DOC},
                {**_GOLDEN_MY_DOC, "total": 530.0, "tax_amount": 30.0},
            ],
        }
        monkeypatch.setenv("LEDGR_GOLDEN_MANIFEST", _write_manifest(tmp_path, golden))
        batch = _make_batch(
            filename=MY_FILE,
            doc_type="invoice",
            credits_used=2,
            documents_processed=2,
            extra_posted=[
                {
                    "path": f"/uploads/{MY_FILE}",
                    "file_name": MY_FILE,
                    "doc_type": "invoice",
                    "invoice_number": "INV-002",
                    "invoice_date": "2025-12-02",
                    "total": 530.0,
                }
            ],
        )
        result = golden_field_match_code(_make_instance(batch))
        # doc_count should match (2==2)
        assert "doc_count=1.0" in result["explanation"]
        assert result["score"] is not None
