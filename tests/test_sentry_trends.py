"""Hermetic tests for WS-6.4 Sentry trend logging."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

import accounting_agents.observability.sentry_trends as sentry_trends
from accounting_agents import nodes
from accounting_agents.observability.sentry_trends import (
    emit_from_struggle_state,
    emit_pipeline_quality_event,
    init_sentry_if_configured,
)
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice, PartyInfo


@pytest.fixture(autouse=True)
def _reset_sentry_module_state(monkeypatch):
    """Keep tests hermetic across DSN on/off cases."""
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.delenv("SENTRY_ENVIRONMENT", raising=False)
    sentry_trends._initialized = False
    yield
    sentry_trends._initialized = False


def _invoice_dict(**overrides) -> dict:
    inv = NormalizedInvoice(
        invoice_number=overrides.pop("invoice_number", "INV-1"),
        invoice_date=overrides.pop("invoice_date", date(2025, 1, 15)),
        doc_total=overrides.pop("doc_total", 109.0),
        reconciled=overrides.pop("reconciled", True),
        reconcile_note=overrides.pop("reconcile_note", None),
        supplier=overrides.pop("supplier", PartyInfo(name="Acme Supplies")),
        lines=overrides.pop(
            "lines",
            [
                InvoiceLine(
                    description="Goods",
                    net_amount=100.0,
                    gst_amount=9.0,
                    account_code="6100",
                )
            ],
        ),
        **overrides,
    )
    return nodes._inv_to_dict(inv)


def test_emit_pipeline_quality_event_noop_without_dsn():
    with patch.object(sentry_trends.sentry_sdk, "capture_message") as capture:
        emit_pipeline_quality_event(
            client_id="client-a",
            vendor="Acme Supplies",
            reconciled=False,
            reason="unreconciled: INV-1 (totals mismatch)",
            confidence=0.82,
        )
    capture.assert_not_called()


def test_emit_pipeline_quality_event_calls_sentry_when_dsn_set(monkeypatch):
    monkeypatch.setenv("SENTRY_DSN", "https://examplePublicKey@o0.ingest.sentry.io/0")
    monkeypatch.setenv("SENTRY_ENVIRONMENT", "test")

    scope = MagicMock()
    with patch.object(sentry_trends.sentry_sdk, "init") as init, patch.object(
        sentry_trends.sentry_sdk, "new_scope"
    ) as new_scope, patch.object(
        sentry_trends.sentry_sdk, "capture_message"
    ) as capture:
        new_scope.return_value.__enter__.return_value = scope
        emit_pipeline_quality_event(
            client_id="client-a",
            vendor="Acme Supplies",
            reconciled=False,
            reason="blank_account_code: INV-1 line #1",
            confidence=0.75,
            extra={"line_index": 1},
        )

    init.assert_called_once()
    scope.set_tag.assert_any_call("client_id", "client-a")
    scope.set_tag.assert_any_call("vendor", "Acme Supplies")
    scope.set_tag.assert_any_call("reconciled", "false")
    scope.set_tag.assert_any_call("reason", "blank_account_code: INV-1 line #1")
    scope.set_tag.assert_any_call("confidence", "0.75")
    scope.set_extra.assert_any_call("line_index", 1)
    capture.assert_called_once_with("pipeline.quality", level="info")


def test_init_sentry_if_configured_noop_without_dsn():
    with patch.object(sentry_trends.sentry_sdk, "init") as init:
        init_sentry_if_configured()
    init.assert_not_called()


def test_emit_from_struggle_state_unreconciled_invoice(monkeypatch):
    monkeypatch.setenv("SENTRY_DSN", "https://examplePublicKey@o0.ingest.sentry.io/0")
    state = {
        "client_id": "client-b",
        nodes.CLASSIFY_CONFIDENCE_KEY: 0.91,
        nodes.NORMALIZED_KEY: [
            _invoice_dict(
                reconciled=False,
                reconcile_note="subtotal mismatch",
            )
        ],
        nodes.DIRECTION_KEY: "purchase",
    }
    reasons = ["unreconciled: INV-1 (subtotal mismatch)"]

    with patch.object(
        sentry_trends, "emit_pipeline_quality_event"
    ) as emit_event:
        emit_from_struggle_state(state, reasons)

    emit_event.assert_called_once()
    kwargs = emit_event.call_args.kwargs
    assert kwargs["client_id"] == "client-b"
    assert kwargs["vendor"] == "Acme Supplies"
    assert kwargs["reconciled"] is False
    assert kwargs["reason"] == "unreconciled: INV-1 (subtotal mismatch)"
    assert kwargs["confidence"] == 0.91
    assert kwargs["extra"] == {"reconcile_note": "subtotal mismatch"}


def test_emit_from_struggle_state_blank_account_code(monkeypatch):
    monkeypatch.setenv("SENTRY_DSN", "https://examplePublicKey@o0.ingest.sentry.io/0")
    state = {
        "client_id": "client-c",
        nodes.CLASSIFY_CONFIDENCE_KEY: 0.88,
        nodes.NORMALIZED_KEY: [
            _invoice_dict(
                lines=[InvoiceLine(description="Goods", net_amount=50.0, account_code="")]
            )
        ],
        nodes.DIRECTION_KEY: "purchase",
    }
    reasons = ["blank_account_code: INV-1 line #1"]

    with patch.object(
        sentry_trends, "emit_pipeline_quality_event"
    ) as emit_event:
        emit_from_struggle_state(state, reasons)

    assert emit_event.call_count == 1
    kwargs = emit_event.call_args.kwargs
    assert kwargs["reason"] == "blank_account_code: INV-1 line #1"
    assert kwargs["reconciled"] is True


def test_detect_struggle_emits_trend_for_unreconciled(monkeypatch):
    monkeypatch.setenv("SENTRY_DSN", "https://examplePublicKey@o0.ingest.sentry.io/0")
    inv = NormalizedInvoice(
        invoice_number="INV-9",
        invoice_date=date(2025, 2, 1),
        doc_total=50.0,
        reconciled=False,
        reconcile_note="totals mismatch",
        supplier=PartyInfo(name="Telco Co"),
        lines=[InvoiceLine(description="Plan", net_amount=50.0, account_code="6100")],
    )
    state = {
        nodes.NORMALIZED_KEY: [nodes._inv_to_dict(inv)],
        nodes.DOC_TYPE_KEY: "invoice",
        nodes.CLASSIFY_CONFIDENCE_KEY: 0.95,
        nodes.TAX_JURISDICTION_KEY: "SINGAPORE",
        "client_id": "client-d",
    }

    with patch.object(
        nodes, "emit_from_struggle_state"
    ) as emit_trends:
        tripped, reasons = nodes.detect_struggle(state)

    assert tripped is True
    assert any(r.startswith("unreconciled:") for r in reasons)
    emit_trends.assert_called_once()
    assert emit_trends.call_args.args[0] is state
    assert any(r.startswith("unreconciled:") for r in emit_trends.call_args.args[1])
