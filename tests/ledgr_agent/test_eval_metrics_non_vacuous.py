"""
Stream-0.4 de-false-green lock.

Asserts that every core eval metric returns score=0.0 when
process_document_batch was never called (no tool response in trace),
and returns meaningful non-zero scores when the tool actually runs
with injected stubs — proving no metric can vacuously pass a
text-only eval trace.
"""
from __future__ import annotations

import pytest

from invoice_processing.extract.invoice_extractor import ExtractedInvoice, ExtractedLine
from invoice_processing.classify.document_classifier import ClassificationResult
from ledgr_agent.metrics import (
    accounting_task_success_code,
    cost_efficiency_code,
    credit_charge_code,
    doc_type_code,
    erp_export_shape_code,
    hitl_noise_score,
    no_unneeded_llm_code,
    tax_validity_code,
)
from ledgr_agent.tools import process_document_batch

# ---------------------------------------------------------------------------
# All 8 core metrics under test
# ---------------------------------------------------------------------------
ALL_METRICS = [
    accounting_task_success_code,
    cost_efficiency_code,
    credit_charge_code,
    doc_type_code,
    erp_export_shape_code,
    hitl_noise_score,
    no_unneeded_llm_code,
    tax_validity_code,
]

METRIC_IDS = [fn.__name__ for fn in ALL_METRICS]


# ---------------------------------------------------------------------------
# Part A — false-green guard
# ---------------------------------------------------------------------------

def _no_tool_trace() -> dict:
    """Trace with a single text-only turn; process_document_batch never called."""
    return {
        "agent_data": {
            "turns": [
                {
                    "events": [
                        {
                            "content": {
                                "parts": [
                                    {"text": "I did not call any tool."}
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    }


def _unrelated_tool_trace() -> dict:
    """Trace with a function_response for a different tool, not process_document_batch."""
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
                                            "name": "some_other_tool",
                                            "response": {
                                                "status": "success",
                                                "data": "irrelevant",
                                            },
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


@pytest.mark.parametrize("metric", ALL_METRICS, ids=METRIC_IDS)
def test_metric_scores_zero_on_no_tool_call_trace(metric) -> None:
    """No metric may pass vacuously when the tool was never called."""
    result = metric(_no_tool_trace())
    assert result["score"] == 0.0, (
        f"{metric.__name__} returned score={result['score']} on a no-tool-call trace; "
        "it must return 0.0 to prevent false-greens in text-only eval prompts"
    )


@pytest.mark.parametrize("metric", ALL_METRICS, ids=METRIC_IDS)
def test_metric_scores_zero_on_unrelated_tool_trace(metric) -> None:
    """Metrics must only credit process_document_batch, not any other tool response."""
    result = metric(_unrelated_tool_trace())
    assert result["score"] == 0.0, (
        f"{metric.__name__} returned score={result['score']} on an unrelated-tool trace; "
        "the walker must only credit process_document_batch"
    )


# ---------------------------------------------------------------------------
# Part B — tool-actually-runs → non-vacuous
# ---------------------------------------------------------------------------

_FIXTURE_PATH = "tests/eval_invoices/invoice_8.pdf"


def _make_cls(doc_type: str = "invoice") -> ClassificationResult:
    return ClassificationResult(
        doc_type=doc_type,
        confidence=0.99,
        issuer_name="Supplier Inc",
        bill_to_name="Playground Client",
        reason="stub",
    )


def _classify_stub(path, **_kw):
    return _make_cls("invoice")


def _direction_stub(cls, **_kw):
    return "purchase"


def _extract_stub(path, **_kw):
    return ExtractedInvoice(
        doc_type="invoice",
        invoice_number="INV-8001",
        invoice_date="2026-06-25",
        currency="SGD",
        issuer_name="Supplier Inc",
        issuer_gst_regno="200012345A",
        bill_to_name="Playground Client",
        lines=[
            ExtractedLine(
                description="Professional services",
                net_amount=500.0,
                gst_amount=45.0,
                tax_label="SR",
            )
        ],
        subtotal=500.0,
        gst_total=45.0,
        total=545.0,
        issuer_tax_system="NONE",
    )


def _categorize_stub(inv, **kw):
    if inv.lines:
        inv.lines[0].account_code = "6100"


def _wrap_payload(payload: dict) -> dict:
    """Wrap a BatchResult dict in the trace shape the metrics walk."""
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
                                            "response": payload,
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


@pytest.fixture(scope="module")
def real_fixture_payload():
    """Run process_document_batch on invoice_8.pdf with injected stubs (no LLM)."""
    import os

    fixture = _FIXTURE_PATH
    if not os.path.exists(fixture):
        pytest.skip(f"Committed fixture not found: {fixture}")

    payload = process_document_batch(
        None,  # tool_context=None → playground default
        paths=[fixture],
        classify_fn=_classify_stub,
        direction_fn=_direction_stub,
        extract_fn=_extract_stub,
        categorize_fn=_categorize_stub,
    )
    return payload


def test_process_document_batch_on_fixture_status_success(real_fixture_payload) -> None:
    assert real_fixture_payload["status"] == "success"


def test_process_document_batch_on_fixture_documents_processed(real_fixture_payload) -> None:
    assert real_fixture_payload["documents_processed"] >= 1


def test_process_document_batch_on_fixture_posted_documents_non_empty(real_fixture_payload) -> None:
    assert len(real_fixture_payload["posted_documents"]) >= 1


def test_accounting_task_success_code_non_vacuous(real_fixture_payload) -> None:
    result = accounting_task_success_code(_wrap_payload(real_fixture_payload))
    assert result["score"] == 1.0, (
        f"accounting_task_success_code should be 1.0 on a success batch; got {result}"
    )


def test_doc_type_code_non_vacuous(real_fixture_payload) -> None:
    result = doc_type_code(_wrap_payload(real_fixture_payload))
    assert result["score"] == 1.0, (
        f"doc_type_code should be 1.0 when invoice doc_type is present; got {result}"
    )


def test_erp_export_shape_code_non_vacuous(real_fixture_payload) -> None:
    result = erp_export_shape_code(_wrap_payload(real_fixture_payload))
    assert result["score"] == 1.0, (
        f"erp_export_shape_code should be 1.0 when export_rows is a list; got {result}"
    )
