"""Hermetic tests for accounting_agents.nodes.

The processing nodes wrap the invoice_processing brain. Every brain callable is
swapped for a deterministic fake via the node module's ``*_FN`` injection seams,
and the ADK ``ctx`` (state + artifact loading) is mocked — no Gemini / network /
real artifact service is touched.

Coverage:
- invoice bundle fan-out (3 invoices -> 3 normalized)
- multi-receipt page (4 receipts -> 4 normalized)
- SOA skip (bundle returns only the embedded invoice)
- classify routing (invoice vs bank_statement)
- bank list (2 accounts -> 2 statements)
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from accounting_agents import nodes
from invoice_processing.classify.document_classifier import ClassificationResult
from invoice_processing.extract.bank_statement_extractor import (
    ExtractedAccount,
    ExtractedBankStatement,
    ExtractedBankTxn,
)
from invoice_processing.extract.invoice_extractor import (
    ExtractedInvoice,
    ExtractedInvoiceBundle,
    ExtractedLine,
)


# =========================================================================== #
# Fake ADK Context
# =========================================================================== #


class FakeContext:
    """Duck-typed stand-in for google.adk.agents.context.Context.

    Provides a mutable ``.state`` dict and an async ``load_artifact`` that returns
    a fake ``types.Part`` carrying the configured PDF bytes.
    """

    def __init__(self, state: dict, pdf_bytes: bytes = b"%PDF-1.4 stub", mime="application/pdf"):
        self.state = dict(state)
        self._pdf_bytes = pdf_bytes
        self._mime = mime

    async def load_artifact(self, filename, version=None):
        inline = SimpleNamespace(data=self._pdf_bytes, mime_type=self._mime)
        return SimpleNamespace(inline_data=inline)


def _base_state(**overrides) -> dict:
    state = {
        nodes.ARTIFACT_NAME_KEY: nodes.ARTIFACT_NAME_FMT.format(file_id="F123"),
        "client_id": "test-client",
        "client_name": "Test Client Pte Ltd",
        "fye_month": 3,
        "tax_registered": True,
        "coa": [],
        "category_mapping": {},
        "entity_memory": [],
    }
    state.update(overrides)
    return state


# =========================================================================== #
# Builders for fake extracted objects
# =========================================================================== #


def _ex_invoice(number: str, net: float = 100.0, gst: float = 9.0) -> ExtractedInvoice:
    return ExtractedInvoice(
        doc_type="invoice",
        invoice_number=number,
        invoice_date="2025-01-15",
        currency="SGD",
        issuer_name="Acme Supplier",
        issuer_gst_regno="200012345A",
        bill_to_name="Test Client Pte Ltd",
        lines=[ExtractedLine(description="Goods", net_amount=net, gst_amount=gst, tax_label="SR")],
        subtotal=net,
        gst_total=gst,
        total=net + gst,
    )


def _ex_receipt(number: str) -> ExtractedInvoice:
    return ExtractedInvoice(
        doc_type="receipt",
        invoice_number=number,
        invoice_date="2025-02-01",
        currency="SGD",
        issuer_name="Coffee Shop",
        bill_to_name="Test Client Pte Ltd",
        lines=[ExtractedLine(description="Meal", net_amount=10.0, gst_amount=0.9, tax_label="SR")],
        subtotal=10.0,
        gst_total=0.9,
        total=10.9,
    )


@pytest.fixture(autouse=True)
def _restore_seams():
    """Snapshot and restore the node injection seams around every test."""
    saved = {
        name: getattr(nodes, name)
        for name in (
            "CLASSIFY_FN",
            "DIRECTION_FN",
            "EXTRACT_BUNDLE_FN",
            "EXTRACT_BANK_FN",
            "CATEGORIZE_FN",
        )
    }
    yield
    for name, fn in saved.items():
        setattr(nodes, name, fn)


# =========================================================================== #
# classify_node routing
# =========================================================================== #


def test_classify_routes_invoice():
    nodes.CLASSIFY_FN = lambda data, mime, **kw: ClassificationResult(
        doc_type="invoice",
        issuer_name="Acme Supplier",
        bill_to_name="Test Client Pte Ltd",
        currency="SGD",
        total_amount=109.0,
        confidence=0.99,
        reason="stub",
    )
    nodes.DIRECTION_FN = lambda cls, client_name=None, **kw: "purchase"

    ctx = FakeContext(_base_state())
    event = asyncio.run(nodes.classify_node(ctx))

    assert event.actions.route == nodes.ROUTE_INVOICE
    assert ctx.state[nodes.DOC_TYPE_KEY] == "invoice"
    assert ctx.state[nodes.DIRECTION_KEY] == "purchase"


def test_classify_routes_bank_statement():
    nodes.CLASSIFY_FN = lambda data, mime, **kw: ClassificationResult(
        doc_type="bank_statement",
        confidence=0.97,
        reason="stub",
    )

    ctx = FakeContext(_base_state())
    event = asyncio.run(nodes.classify_node(ctx))

    assert event.actions.route == nodes.ROUTE_BANK
    assert ctx.state[nodes.DOC_TYPE_KEY] == "bank_statement"
    assert ctx.state[nodes.DIRECTION_KEY] is None


def test_classify_missing_artifact_raises():
    ctx = FakeContext({})  # no artifact-name key
    with pytest.raises(ValueError):
        asyncio.run(nodes.classify_node(ctx))


# =========================================================================== #
# extract_invoice_node fan-out
# =========================================================================== #


def test_invoice_bundle_fanout_three():
    bundle = ExtractedInvoiceBundle(
        invoices=[_ex_invoice("INV-1"), _ex_invoice("INV-2"), _ex_invoice("INV-3")]
    )
    nodes.EXTRACT_BUNDLE_FN = lambda data, mime, **kw: bundle

    ctx = FakeContext(_base_state(**{nodes.DIRECTION_KEY: "purchase"}))
    event = asyncio.run(nodes.extract_invoice_node(ctx))

    assert event.output == {"count": 3}
    normalized = ctx.state[nodes.NORMALIZED_KEY]
    assert len(normalized) == 3
    assert {n["invoice_number"] for n in normalized} == {"INV-1", "INV-2", "INV-3"}
    assert all(n["doc_type"] == "purchase" for n in normalized)


def test_multi_receipt_page_fanout_four():
    bundle = ExtractedInvoiceBundle(
        invoices=[_ex_receipt(f"R-{i}") for i in range(1, 5)],
        notes="4 receipts on one scanned page",
    )
    nodes.EXTRACT_BUNDLE_FN = lambda data, mime, **kw: bundle

    ctx = FakeContext(_base_state(**{nodes.DIRECTION_KEY: "purchase"}))
    event = asyncio.run(nodes.extract_invoice_node(ctx))

    assert event.output == {"count": 4}
    assert len(ctx.state[nodes.NORMALIZED_KEY]) == 4


def test_soa_skip_extracts_only_embedded_invoice():
    # An SOA package: the fake extractor skips the summary/cover page and returns
    # only the single embedded real invoice.
    bundle = ExtractedInvoiceBundle(
        invoices=[_ex_invoice("EMBEDDED-INV")],
        skipped_pages=[1],
        notes="skipped SOA summary cover page",
    )
    nodes.EXTRACT_BUNDLE_FN = lambda data, mime, **kw: bundle

    ctx = FakeContext(_base_state(**{nodes.DIRECTION_KEY: "purchase"}))
    event = asyncio.run(nodes.extract_invoice_node(ctx))

    assert event.output == {"count": 1}
    normalized = ctx.state[nodes.NORMALIZED_KEY]
    assert len(normalized) == 1
    assert normalized[0]["invoice_number"] == "EMBEDDED-INV"


def test_invoice_node_defaults_direction_to_purchase():
    nodes.EXTRACT_BUNDLE_FN = lambda data, mime, **kw: ExtractedInvoiceBundle(
        invoices=[_ex_invoice("INV-X")]
    )
    ctx = FakeContext(_base_state())  # no direction in state
    asyncio.run(nodes.extract_invoice_node(ctx))
    assert ctx.state[nodes.NORMALIZED_KEY][0]["doc_type"] == "purchase"


# =========================================================================== #
# categorize_node + tax_node (chained off extract)
# =========================================================================== #


def test_categorize_and_tax_chain():
    nodes.EXTRACT_BUNDLE_FN = lambda data, mime, **kw: ExtractedInvoiceBundle(
        invoices=[_ex_invoice("INV-1")]
    )
    # Fake categorizer stamps a fixed account code on every line.
    def _fake_categorize(inv, *, coa, category_mapping, entity_memory, **kw):
        for line in inv.lines:
            line.account_code = "500"
        return inv

    nodes.CATEGORIZE_FN = _fake_categorize

    ctx = FakeContext(_base_state(**{nodes.DIRECTION_KEY: "purchase"}))
    asyncio.run(nodes.extract_invoice_node(ctx))
    asyncio.run(nodes.categorize_node(ctx))
    asyncio.run(nodes.tax_node(ctx))

    line = ctx.state[nodes.NORMALIZED_KEY][0]["lines"][0]
    assert line["account_code"] == "500"
    # tax_node ran the real TaxClassifier: SR line + supplier GST-registered.
    assert line["tax_treatment"] == "SR"


# =========================================================================== #
# extract_bank_node list
# =========================================================================== #


def test_bank_node_multi_account_list():
    ex_bank = ExtractedBankStatement(
        accounts=[
            ExtractedAccount(
                bank_name="OCBC - 5001",
                account_number="5001",
                currency="SGD",
                opening_balance=1000.0,
                closing_balance=1200.0,
                transactions=[
                    ExtractedBankTxn(date="2025-01-10", description="In", deposit=200.0, balance=1200.0)
                ],
            ),
            ExtractedAccount(
                bank_name="OCBC - 9002 USD",
                account_number="9002",
                currency="USD",
                opening_balance=500.0,
                closing_balance=450.0,
                transactions=[
                    ExtractedBankTxn(date="2025-01-12", description="Out", withdrawal=50.0, balance=450.0)
                ],
            ),
        ]
    )
    # Fake returns (statement, mode) tuple like the real extractor.
    nodes.EXTRACT_BANK_FN = lambda data, mime, **kw: (ex_bank, "vision")

    ctx = FakeContext(_base_state())
    event = asyncio.run(nodes.extract_bank_node(ctx))

    assert event.output == {"count": 2}
    statements = ctx.state[nodes.BANK_STATEMENTS_KEY]
    assert len(statements) == 2
    assert {s["currency"] for s in statements} == {"SGD", "USD"}


def test_bank_node_accepts_bare_statement():
    ex_bank = ExtractedBankStatement(
        accounts=[
            ExtractedAccount(
                bank_name="DBS - 1",
                currency="SGD",
                opening_balance=0.0,
                transactions=[ExtractedBankTxn(date="2025-03-01", description="x", deposit=1.0, balance=1.0)],
            )
        ]
    )
    nodes.EXTRACT_BANK_FN = lambda data, mime, **kw: ex_bank  # no tuple
    ctx = FakeContext(_base_state())
    event = asyncio.run(nodes.extract_bank_node(ctx))
    assert event.output == {"count": 1}


# =========================================================================== #
# route_node
# =========================================================================== #


def test_route_node_invoice_fy_and_sheet():
    nodes.EXTRACT_BUNDLE_FN = lambda data, mime, **kw: ExtractedInvoiceBundle(
        invoices=[_ex_invoice("INV-1"), _ex_invoice("INV-2")]
    )
    ctx = FakeContext(_base_state(**{nodes.DIRECTION_KEY: "purchase"}))
    asyncio.run(nodes.extract_invoice_node(ctx))
    event = asyncio.run(nodes.route_node(ctx))

    assert event.output == {"count": 2}
    routes = ctx.state[nodes.ROUTES_KEY]
    # Jan 2025 with fye_month=3 -> FY2025
    assert all(r["workbook"] == "Ledger_FY2025.xlsx" for r in routes)
    assert all(r["sheet"] == "Purchase" for r in routes)


def test_route_node_bank_workbook():
    ex_bank = ExtractedBankStatement(
        accounts=[
            ExtractedAccount(
                bank_name="OCBC - 5001",
                currency="SGD",
                opening_balance=0.0,
                transactions=[ExtractedBankTxn(date="2025-01-10", description="x", deposit=1.0, balance=1.0)],
            )
        ]
    )
    nodes.EXTRACT_BANK_FN = lambda data, mime, **kw: (ex_bank, "vision")
    ctx = FakeContext(_base_state(**{nodes.DOC_TYPE_KEY: "bank_statement"}))
    asyncio.run(nodes.extract_bank_node(ctx))
    event = asyncio.run(nodes.route_node(ctx))

    assert event.output == {"count": 1}
    assert ctx.state[nodes.ROUTES_KEY][0]["workbook"] == "BankStatement_FY2025.xlsx"


# =========================================================================== #
# apply_decision_node — apply human approve/edit/reject decision in the spine
# =========================================================================== #


def test_apply_decision_node_applies_line_edits():
    state = _base_state()
    state[nodes.NORMALIZED_KEY] = [{
        "invoice_number": "INV-1", "lines": [
            {"description": "Room", "account_code": None, "tax_code": "SR", "amount": 51.49}
        ],
    }]
    ctx = FakeContext(state)
    decision = {"decision": "edit", "edits": {"lines": [
        {"index": 0, "account_code": "6010", "tax_code": "ZR"}
    ]}}
    asyncio.run(nodes.apply_decision_node(ctx, decision))
    line = ctx.state[nodes.NORMALIZED_KEY][0]["lines"][0]
    assert line["account_code"] == "6010"
    assert line["tax_code"] == "ZR"
    assert ctx.state[nodes.APPROVAL_STATUS_KEY] == "edit"


def test_apply_decision_node_reject_clears_invoices():
    state = _base_state()
    state[nodes.NORMALIZED_KEY] = [{"invoice_number": "INV-1", "lines": []}]
    ctx = FakeContext(state)
    asyncio.run(nodes.apply_decision_node(ctx, {"decision": "reject"}))
    assert ctx.state[nodes.NORMALIZED_KEY] == []
    assert ctx.state[nodes.APPROVAL_STATUS_KEY] == "reject"


def test_apply_decision_node_autoapprove_passthrough():
    state = _base_state()
    state[nodes.NORMALIZED_KEY] = [{"invoice_number": "INV-1", "lines": []}]
    ctx = FakeContext(state)
    asyncio.run(nodes.apply_decision_node(ctx, None))  # no HITL → node_input is None
    assert ctx.state[nodes.NORMALIZED_KEY] == [{"invoice_number": "INV-1", "lines": []}]


def test_apply_decision_node_approve_passes_through_with_status():
    state = _base_state()
    state[nodes.NORMALIZED_KEY] = [{"invoice_number": "INV-1", "lines": []}]
    ctx = FakeContext(state)
    asyncio.run(nodes.apply_decision_node(ctx, {"decision": "approve"}))
    assert ctx.state[nodes.NORMALIZED_KEY] == [{"invoice_number": "INV-1", "lines": []}]
    assert ctx.state[nodes.APPROVAL_STATUS_KEY] == "approve"


@pytest.mark.parametrize("edits_payload, expected_line", [
    ({"lines": []}, {"description": "Room", "account_code": None, "tax_code": "SR", "amount": 51.49}),
    ({"lines": [{"index": 99, "account_code": "6010"}]}, {"description": "Room", "account_code": None, "tax_code": "SR", "amount": 51.49}),
    ({"lines": [{"index": "0", "account_code": "6010"}]}, {"description": "Room", "account_code": None, "tax_code": "SR", "amount": 51.49}),
])
def test_apply_decision_node_edit_edge_cases_no_op(edits_payload, expected_line):
    state = _base_state()
    state[nodes.NORMALIZED_KEY] = [{
        "invoice_number": "INV-1",
        "lines": [{"description": "Room", "account_code": None, "tax_code": "SR", "amount": 51.49}],
    }]
    ctx = FakeContext(state)
    asyncio.run(nodes.apply_decision_node(ctx, {"decision": "edit", "edits": edits_payload}))
    assert ctx.state[nodes.NORMALIZED_KEY][0]["lines"][0] == expected_line


def test_apply_decision_node_edit_with_multi_invoice_logs_warning(caplog):
    import logging
    state = _base_state()
    state[nodes.NORMALIZED_KEY] = [
        {"invoice_number": "INV-1", "lines": [{"description": "A", "account_code": None, "tax_code": "SR", "amount": 10.0}]},
        {"invoice_number": "INV-2", "lines": [{"description": "B", "account_code": None, "tax_code": "SR", "amount": 20.0}]},
    ]
    ctx = FakeContext(state)
    with caplog.at_level(logging.WARNING, logger="accounting_agents.nodes"):
        asyncio.run(nodes.apply_decision_node(ctx, {"decision": "edit", "edits": {"lines": [{"index": 0, "account_code": "6010"}]}}))
    assert any("invoice_index" in r.message for r in caplog.records)
    # First invoice mutated, second untouched
    assert ctx.state[nodes.NORMALIZED_KEY][0]["lines"][0]["account_code"] == "6010"
    assert ctx.state[nodes.NORMALIZED_KEY][1]["lines"][0]["account_code"] is None


# =========================================================================== #
# deliver_node summary echoes the accounting-software target
# =========================================================================== #


def test_deliver_echoes_software_target():
    ctx = FakeContext({
        nodes.LEDGER_ROWS_KEY: {
            "fy": "2026", "kind": "invoice", "software": "Xero",
            "batches": [{"sheet": "Purchase", "rows": [{"Total Amount": 10}]}],
        }
    })
    asyncio.run(nodes.deliver_node(ctx))
    summary = ctx.state[nodes.DELIVER_SUMMARY_KEY]
    assert "Xero" in summary
    assert "FY2026" in summary


# =========================================================================== #
# Self-referential / dividend guard — end-to-end through extract_invoice_node
# =========================================================================== #


def _self_ref_bundle() -> "ExtractedInvoiceBundle":
    """Simulate a dividend cert where issuer == bill_to == client."""
    inv = ExtractedInvoice(
        doc_type="invoice",
        invoice_number="DIV-001",
        invoice_date="2025-03-31",
        currency="SGD",
        issuer_name="Test Client Pte Ltd",
        issuer_gst_regno=None,
        bill_to_name="Test Client Pte Ltd",
        lines=[
            ExtractedLine(
                description="Dividend payout",
                net_amount=5000.0,
                gst_amount=0.0,
                tax_label="OS",
            )
        ],
        subtotal=5000.0,
        gst_total=0.0,
        total=5000.0,
    )
    return ExtractedInvoiceBundle(invoices=[inv])


def test_extract_node_self_referential_flagged_for_review():
    """extract_invoice_node must mark self-referential docs reconciled=False
    with a 'needs review' note — never silently book as a clean purchase."""
    nodes.EXTRACT_BUNDLE_FN = lambda data, mime, **kw: _self_ref_bundle()

    state = _base_state(**{nodes.DIRECTION_KEY: "self_referential"})
    ctx = FakeContext(state)
    asyncio.run(nodes.extract_invoice_node(ctx))

    normalized_list = ctx.state[nodes.NORMALIZED_KEY]
    assert len(normalized_list) == 1
    inv = normalized_list[0]

    # Must be flagged for review.
    assert inv.get("reconciled") is False, (
        f"Expected reconciled=False on self-referential doc, got {inv.get('reconciled')!r}"
    )
    note = inv.get("reconcile_note") or ""
    assert "self-referential" in note.lower(), (
        f"Expected self-referential note, got: {note!r}"
    )
    assert "needs review" in note.lower(), (
        f"Expected 'needs review' in note, got: {note!r}"
    )


def test_extract_node_unknown_direction_flagged_for_review():
    """extract_invoice_node must flag 'unknown' direction for review too."""
    nodes.EXTRACT_BUNDLE_FN = lambda data, mime, **kw: _self_ref_bundle()

    state = _base_state(**{nodes.DIRECTION_KEY: "unknown"})
    ctx = FakeContext(state)
    asyncio.run(nodes.extract_invoice_node(ctx))

    normalized_list = ctx.state[nodes.NORMALIZED_KEY]
    assert len(normalized_list) == 1
    inv = normalized_list[0]

    assert inv.get("reconciled") is False
    note = inv.get("reconcile_note") or ""
    assert "needs review" in note.lower()
    assert "unknown" in note.lower()


def test_extract_node_clean_purchase_unaffected():
    """A normal purchase (direction='purchase') must NOT be flagged for review
    by the guard — the guard must only fire on self_referential / unknown."""
    nodes.EXTRACT_BUNDLE_FN = lambda data, mime, **kw: ExtractedInvoiceBundle(
        invoices=[_ex_invoice("INV-NORMAL")]
    )

    state = _base_state(**{nodes.DIRECTION_KEY: "purchase"})
    ctx = FakeContext(state)
    asyncio.run(nodes.extract_invoice_node(ctx))

    inv = ctx.state[nodes.NORMALIZED_KEY][0]
    note = inv.get("reconcile_note") or ""
    assert "self-referential" not in note.lower()
    assert "direction unknown" not in note.lower()

