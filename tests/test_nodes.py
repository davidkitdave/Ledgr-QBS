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
from invoice_processing.extract.document_record import (
    DocumentRecord,
    DocumentRecordBundle,
    LabeledField,
    LineCapture,
)


def _document_record_from_ex(ex: ExtractedInvoice) -> DocumentRecord:
    fields: list[LabeledField] = []
    if ex.invoice_number:
        fields.append(LabeledField(label="Invoice Number", value=ex.invoice_number))
    if ex.invoice_date:
        fields.append(LabeledField(label="Invoice Date", value=ex.invoice_date))
    if ex.currency:
        fields.append(LabeledField(label="Currency", value=ex.currency))
    if ex.fx_rate is not None:
        fields.append(LabeledField(label="Exchange Rate", value=str(ex.fx_rate)))
    totals: list[LabeledField] = []
    if ex.subtotal is not None:
        totals.append(LabeledField(label="Sub Total", value=str(ex.subtotal)))
    if ex.gst_total is not None:
        totals.append(LabeledField(label="GST", value=str(ex.gst_total)))
    if ex.total is not None:
        totals.append(LabeledField(label="Total", value=str(ex.total)))
    return DocumentRecord(
        labeled_fields=fields,
        line_items=[
            LineCapture(
                description=ln.description,
                net_amount=ln.net_amount,
                tax_label=ln.tax_label,
            )
            for ln in ex.lines
        ],
        totals=totals,
    )


def _doc_bundle_from_ex_bundle(bundle: ExtractedInvoiceBundle) -> DocumentRecordBundle:
    return DocumentRecordBundle(
        documents=[_document_record_from_ex(ex) for ex in bundle.invoices],
        skipped_pages=bundle.skipped_pages,
        notes=bundle.notes,
    )


def _install_legacy_extract_mock(doc_bundle: DocumentRecordBundle) -> None:
    """Hermetic tests: drive ``extract_invoice_document_node`` via legacy normalize."""
    from invoice_processing.extract.document_normalizer import (
        normalize_document_bundle,
        slim_document_record_for_state,
    )
    from invoice_processing.extract.process_invoice_document import InvoiceProcessResult

    def _extract(data, mime, **kw):
        normalized = normalize_document_bundle(
            doc_bundle,
            direction=kw.get("direction") or "purchase",
            our_gst_registered=kw.get("our_gst_registered", True),
            base_currency=kw.get("base_currency") or "SGD",
            client_name=kw.get("client_name"),
            client_uen=kw.get("client_uen"),
        )
        return InvoiceProcessResult(
            normalized=normalized,
            extraction_path="legacy",
            document_records=[
                slim_document_record_for_state(d) for d in doc_bundle.documents
            ],
            skipped_pages=doc_bundle.skipped_pages,
            document_read_notes=doc_bundle.notes,
        )

    nodes.EXTRACT_INVOICE_DOCUMENT_FN = _extract


def _legacy_result_from_ex_bundle(bundle: ExtractedInvoiceBundle):
    """Build an ``InvoiceProcessResult`` for review/HITL hermetic tests."""
    from invoice_processing.extract.document_normalizer import (
        normalize_document_bundle,
        slim_document_record_for_state,
    )
    from invoice_processing.extract.process_invoice_document import InvoiceProcessResult

    doc_bundle = _doc_bundle_from_ex_bundle(bundle)
    normalized = normalize_document_bundle(
        doc_bundle,
        direction="purchase",
        our_gst_registered=True,
        base_currency="SGD",
    )
    return InvoiceProcessResult(
        normalized=normalized,
        extraction_path="legacy",
        document_records=[
            slim_document_record_for_state(d) for d in doc_bundle.documents
        ],
        skipped_pages=doc_bundle.skipped_pages,
        document_read_notes=doc_bundle.notes,
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
            "EXTRACT_DOCUMENT_FN",
            "EXTRACT_INVOICE_DOCUMENT_FN",
            "NORMALIZE_DOCUMENT_FN",
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
    # Track whether resolve_direction is called from the invoice lane. After
    # the Batch Direction slice, classify_node should NOT call it for invoices
    # — the Understand call owns ``direction_for_client`` instead.
    resolve_called = {"n": 0}
    def _spy_resolve(cls, **kw):
        resolve_called["n"] += 1
        return "purchase"
    nodes.DIRECTION_FN = _spy_resolve

    ctx = FakeContext(_base_state())
    event = asyncio.run(nodes.classify_node._func(ctx))

    assert event.actions.route == nodes.ROUTE_INVOICE
    assert ctx.state[nodes.DOC_TYPE_KEY] == "invoice"
    # Direction defaults to "auto"; the Understand call later fills it in via
    # ``direction_for_client``. ``"auto"`` is the sentinel that the
    # ledger_extract_to_normalized adapter interprets as "use the Understand
    # call's direction_for_client verdict".
    assert ctx.state[nodes.DIRECTION_KEY] == "auto"
    assert resolve_called["n"] == 0, (
        "classify_node must not call resolve_direction in the invoice lane; "
        "the Understand call owns direction_for_client instead."
    )


def test_classify_routes_bank_statement():
    nodes.CLASSIFY_FN = lambda data, mime, **kw: ClassificationResult(
        doc_type="bank_statement",
        confidence=0.97,
        reason="stub",
    )

    ctx = FakeContext(_base_state())
    event = asyncio.run(nodes.classify_node._func(ctx))

    assert event.actions.route == nodes.ROUTE_BANK
    assert ctx.state[nodes.DOC_TYPE_KEY] == "bank_statement"
    assert ctx.state[nodes.DIRECTION_KEY] is None


def test_classify_missing_artifact_raises():
    ctx = FakeContext({})  # no artifact-name key
    with pytest.raises(ValueError):
        asyncio.run(nodes.classify_node._func(ctx))


# =========================================================================== #
# extract_invoice_node fan-out
# =========================================================================== #


def test_invoice_bundle_fanout_three():
    bundle = ExtractedInvoiceBundle(
        invoices=[_ex_invoice("INV-1"), _ex_invoice("INV-2"), _ex_invoice("INV-3")]
    )
    _install_legacy_extract_mock(_doc_bundle_from_ex_bundle(bundle))

    ctx = FakeContext(_base_state(**{nodes.DIRECTION_KEY: "purchase"}))
    event = asyncio.run(nodes.extract_invoice_node._func(ctx))

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
    _install_legacy_extract_mock(_doc_bundle_from_ex_bundle(bundle))

    ctx = FakeContext(_base_state(**{nodes.DIRECTION_KEY: "purchase"}))
    event = asyncio.run(nodes.extract_invoice_node._func(ctx))

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
    _install_legacy_extract_mock(_doc_bundle_from_ex_bundle(bundle))

    ctx = FakeContext(_base_state(**{nodes.DIRECTION_KEY: "purchase"}))
    event = asyncio.run(nodes.extract_invoice_node._func(ctx))

    assert event.output == {"count": 1}
    normalized = ctx.state[nodes.NORMALIZED_KEY]
    assert len(normalized) == 1
    assert normalized[0]["invoice_number"] == "EMBEDDED-INV"


def test_soa_phantom_invoices_dropped_by_normalize_bundle():
    """Regression: _normalize_bundle (nodes.py path) must apply the SOA hard-gate.

    This captures the live failure where the bot reported "18 sub-documents /
    30 total lines" for Sample Vendor Inc DEC 2025 (expected 10/22).  The extractor
    returns a bundle with 8 phantom invoices whose lines all have a bare
    'INVOICE' description and gst_amount==0 — the exact shape hallucinated from
    the SOA cover table.  They must be dropped before NORMALIZED_KEY is written.
    """
    phantom_line = ExtractedLine(description="INVOICE", net_amount=100.0, gst_amount=0.0)
    phantom_inv_a = ExtractedInvoice(
        doc_type="invoice",
        invoice_number="IA-07316",
        invoice_date="2025-12-01",
        currency="MYR",
        issuer_name="Sample Vendor Inc",
        bill_to_name="Sample Auto Enterprise",
        lines=[phantom_line],
        subtotal=100.0,
        gst_total=0.0,
        total=100.0,
    )
    phantom_inv_b = ExtractedInvoice(
        doc_type="invoice",
        invoice_number="IA-07330",
        invoice_date="2025-12-01",
        currency="MYR",
        issuer_name="Sample Vendor Inc",
        bill_to_name="Sample Auto Enterprise",
        lines=[phantom_line],
        subtotal=200.0,
        gst_total=0.0,
        total=200.0,
    )
    real_inv = ExtractedInvoice(
        doc_type="invoice",
        invoice_number="IA-07465",
        invoice_date="2025-12-05",
        currency="MYR",
        issuer_name="Sample Vendor Inc",
        bill_to_name="Sample Auto Enterprise",
        lines=[ExtractedLine(description="Electricity supply Dec 2025", net_amount=100.0, gst_amount=9.0, tax_label="SR")],
        subtotal=100.0,
        gst_total=9.0,
        total=109.0,
    )
    bundle = ExtractedInvoiceBundle(
        invoices=[phantom_inv_a, phantom_inv_b, real_inv],
        skipped_pages=[1],
    )
    _install_legacy_extract_mock(_doc_bundle_from_ex_bundle(bundle))

    ctx = FakeContext(_base_state(**{nodes.DIRECTION_KEY: "purchase"}))
    event = asyncio.run(nodes.extract_invoice_node._func(ctx))

    # Gate must have dropped the 2 phantoms — only the real invoice survives.
    assert event.output == {"count": 1}, (
        f"Expected count=1 after SOA gate, got {event.output}"
    )
    normalized = ctx.state[nodes.NORMALIZED_KEY]
    assert len(normalized) == 1, (
        f"Expected 1 normalized invoice, got {len(normalized)}: "
        f"{[n['invoice_number'] for n in normalized]}"
    )
    assert normalized[0]["invoice_number"] == "IA-07465"


def test_invoice_node_defaults_direction_to_purchase():
    _install_legacy_extract_mock(_doc_bundle_from_ex_bundle(
        ExtractedInvoiceBundle(invoices=[_ex_invoice("INV-X")])
    ))
    ctx = FakeContext(_base_state())  # no direction in state
    asyncio.run(nodes.extract_invoice_node._func(ctx))
    assert ctx.state[nodes.NORMALIZED_KEY][0]["doc_type"] == "purchase"


# =========================================================================== #
# categorize_node + tax_node (chained off extract)
# =========================================================================== #


def test_categorize_and_tax_chain():
    _install_legacy_extract_mock(_doc_bundle_from_ex_bundle(
        ExtractedInvoiceBundle(invoices=[_ex_invoice("INV-1")])
    ))
    # Fake categorizer stamps a fixed account code on every line.
    def _fake_categorize(inv, *, coa, category_mapping, entity_memory, **kw):
        for line in inv.lines:
            line.account_code = "500"
        return inv

    nodes.CATEGORIZE_FN = _fake_categorize

    ctx = FakeContext(_base_state(**{nodes.DIRECTION_KEY: "purchase"}))
    asyncio.run(nodes.extract_invoice_node._func(ctx))
    asyncio.run(nodes.categorize_node._func(ctx))
    asyncio.run(nodes.tax_node._func(ctx))

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
    event = asyncio.run(nodes.extract_bank_node._func(ctx))

    assert event.output == {"count": 2}
    statements = ctx.state[nodes.BANK_STATEMENTS_KEY]
    assert len(statements) == 2
    assert {s["currency"] for s in statements} == {"SGD", "USD"}


def test_consolidate_node_bank_sheet_titles_multi_currency():
    """consolidate_node emits distinct 'Bank - XXXX - CCY' sheet names per currency.

    Regression for the Akar DBS FY2024 case: a multi-currency statement of
    one account used to collapse into a single Excel tab because the sheet
    name was the LLM-supplied ``bank_name`` (no currency suffix). The merge
    in ``SlackLedgerStore._merge_bank_statement`` then silently rolled the
    sections together, so the workbook had one tab but the Slack delivery
    card showed N preview tables (one per batch).
    """
    ex_bank = ExtractedBankStatement(
        accounts=[
            ExtractedAccount(
                bank_name="DBS Bank Ltd",
                account_number="072-955554-5",
                currency="SGD",
                opening_balance=100.0,
                closing_balance=0.0,
                transactions=[
                    ExtractedBankTxn(date="2024-04-15", description="REMITTANCE",
                                      withdrawal=100.0, balance=0.0)
                ],
            ),
            ExtractedAccount(
                bank_name="DBS Bank Ltd",
                account_number="072-955554-5",
                currency="USD",
                opening_balance=50.0,
                closing_balance=50.0,
                transactions=[
                    ExtractedBankTxn(date="2024-04-15", description="ADVICE 055...",
                                      withdrawal=30.0, balance=20.0)
                ],
            ),
            ExtractedAccount(
                bank_name="DBS Bank Ltd",
                account_number="072-955554-5",
                currency="CNH",
                opening_balance=0.0,
                closing_balance=200.0,
                transactions=[
                    ExtractedBankTxn(date="2024-04-15", description="BUSINESS A...",
                                      deposit=200.0, balance=200.0)
                ],
            ),
        ]
    )
    nodes.EXTRACT_BANK_FN = lambda data, mime, **kw: (ex_bank, "vision")
    state = _base_state(**{nodes.DOC_TYPE_KEY: "bank_statement"})
    ctx = FakeContext(state)
    asyncio.run(nodes.extract_bank_node._func(ctx))
    asyncio.run(nodes.route_node._func(ctx))
    asyncio.run(nodes.consolidate_node._func(ctx))

    payload = ctx.state[nodes.LEDGER_ROWS_KEY]
    assert payload["kind"] == "bank"
    sheets = [b["sheet"] for b in payload["batches"]]
    assert sheets == [
        "DBS Bank Ltd - 5545 - SGD",
        "DBS Bank Ltd - 5545 - USD",
        "DBS Bank Ltd - 5545 - CNH",
    ]
    # Dedupe identity must include currency so the 3 sections don't collide.
    keys = [b["doc_key"] for b in payload["batches"]]
    assert len(set(keys)) == 3
    assert all("SGD" in k or "USD" in k or "CNH" in k for k in keys)


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
    event = asyncio.run(nodes.extract_bank_node._func(ctx))
    assert event.output == {"count": 1}


# =========================================================================== #
# route_node
# =========================================================================== #


def test_route_node_invoice_fy_and_sheet():
    _install_legacy_extract_mock(_doc_bundle_from_ex_bundle(
        ExtractedInvoiceBundle(invoices=[_ex_invoice("INV-1"), _ex_invoice("INV-2")])
    ))
    ctx = FakeContext(_base_state(**{nodes.DIRECTION_KEY: "purchase"}))
    asyncio.run(nodes.extract_invoice_node._func(ctx))
    event = asyncio.run(nodes.route_node._func(ctx))

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
    asyncio.run(nodes.extract_bank_node._func(ctx))
    event = asyncio.run(nodes.route_node._func(ctx))

    assert event.output == {"count": 1}
    assert ctx.state[nodes.ROUTES_KEY][0]["workbook"] == "BankStatement_FY2025.xlsx"


def test_route_node_bank_fy_when_first_currency_has_no_transactions():
    """Jun 2024 must route to FY2024 even when CNH (listed first) has no txns.

    Regression: empty first account used date.today() -> FY2026 in 2026.
    """
    from invoice_processing.extract.bank_statement_extractor import (
        ExtractedAccount,
        ExtractedBankStatement,
        ExtractedBankTxn,
    )

    ex_bank = ExtractedBankStatement(
        accounts=[
            ExtractedAccount(
                bank_name="DBS Bank Ltd",
                account_number="072-955554-5",
                currency="CNH",
                statement_period="01 Jun 2024 - 30 Jun 2024",
                opening_balance=0.0,
                closing_balance=0.0,
                transactions=[],
            ),
            ExtractedAccount(
                bank_name="DBS Bank Ltd",
                account_number="072-955554-5",
                currency="SGD",
                statement_period="01 Jun 2024 - 30 Jun 2024",
                opening_balance=16189.43,
                closing_balance=26379.7,
                transactions=[
                    ExtractedBankTxn(
                        date="2024-06-14", description="GIRO",
                        deposit=12000.0, balance=28189.43,
                    ),
                ],
            ),
            ExtractedAccount(
                bank_name="DBS Bank Ltd",
                account_number="072-955554-5",
                currency="USD",
                statement_period="01 Jun 2024 - 30 Jun 2024",
                opening_balance=7668.0,
                closing_balance=8860.67,
                transactions=[
                    ExtractedBankTxn(
                        date="2024-06-11", description="TT",
                        withdrawal=4800.0, balance=2868.0,
                    ),
                ],
            ),
        ]
    )
    nodes.EXTRACT_BANK_FN = lambda data, mime, **kw: (ex_bank, "vision")
    ctx = FakeContext(_base_state(**{
        nodes.DOC_TYPE_KEY: "bank_statement",
        "fye_month": 12,
        "source_filename": "4. DBS - Jun 2024.pdf",
    }))
    asyncio.run(nodes.extract_bank_node._func(ctx))
    asyncio.run(nodes.route_node._func(ctx))
    asyncio.run(nodes.consolidate_node._func(ctx))

    routes = ctx.state[nodes.ROUTES_KEY]
    assert all(r["fy"] == 2024 for r in routes)
    assert all(r["workbook"] == "BankStatement_FY2024.xlsx" for r in routes)
    assert ctx.state[nodes.LEDGER_ROWS_KEY]["fy"] == "2024"


def test_parse_statement_period_dbs_dash_format():
    """DBS prints periods like '01-Jun-2024 to 30-Jun-2024'."""
    from datetime import date

    from accounting_agents.nodes import _parse_statement_period_anchor

    assert _parse_statement_period_anchor("01-Jun-2024 to 30-Jun-2024") == date(2024, 6, 1)
    assert _parse_statement_period_anchor("01 Apr 2024 - 30 Apr 2024") == date(2024, 4, 1)


def test_route_node_bank_fy_from_period_when_all_currencies_empty():
    """When every currency section is txn-empty, derive FY from statement_period."""
    from invoice_processing.extract.bank_statement_extractor import (
        ExtractedAccount,
        ExtractedBankStatement,
    )

    ex_bank = ExtractedBankStatement(
        accounts=[
            ExtractedAccount(
                bank_name="DBS Bank Ltd",
                account_number="072-955554-5",
                currency="CNH",
                statement_period="01-Jun-2024 to 30-Jun-2024",
                opening_balance=0.0,
                closing_balance=0.0,
                transactions=[],
            ),
        ]
    )
    nodes.EXTRACT_BANK_FN = lambda data, mime, **kw: (ex_bank, "vision")
    ctx = FakeContext(_base_state(**{
        nodes.DOC_TYPE_KEY: "bank_statement",
        "fye_month": 12,
    }))
    asyncio.run(nodes.extract_bank_node._func(ctx))
    asyncio.run(nodes.route_node._func(ctx))
    assert ctx.state[nodes.ROUTES_KEY][0]["fy"] == 2024


def test_route_node_bank_apr_2024_calendar_fye():
    """Apr 2024 statement routes to FY2024 when client FYE month is December."""
    ex_bank = ExtractedBankStatement(
        accounts=[
            ExtractedAccount(
                bank_name="DBS Bank Ltd",
                account_number="072-065554-5",
                currency="SGD",
                statement_period="01 Apr 2024 - 30 Apr 2024",
                opening_balance=0.0,
                transactions=[
                    ExtractedBankTxn(
                        date="2024-04-15", description="SALARY",
                        deposit=100.0, balance=100.0,
                    ),
                ],
            )
        ]
    )
    nodes.EXTRACT_BANK_FN = lambda data, mime, **kw: (ex_bank, "vision")
    ctx = FakeContext(_base_state(**{
        nodes.DOC_TYPE_KEY: "bank_statement",
        "fye_month": 12,
    }))
    asyncio.run(nodes.extract_bank_node._func(ctx))
    event = asyncio.run(nodes.route_node._func(ctx))

    assert event.output == {"count": 1}
    assert ctx.state[nodes.ROUTES_KEY][0]["fy"] == 2024
    assert ctx.state[nodes.ROUTES_KEY][0]["workbook"] == "BankStatement_FY2024.xlsx"


# =========================================================================== #
# apply_decision_node — apply human approve/edit/reject decision in the spine
# =========================================================================== #


def test_apply_decision_node_applies_line_edits():
    """HITL Edit MUST land on the CANONICAL InvoiceLine keys (``tax_treatment``,
    ``net_amount``) — the exporter reads those when writing the ledger row.
    The pre-2026-06-15 names (``tax_code``, ``amount``) silently no-op'd."""
    state = _base_state()
    state[nodes.NORMALIZED_KEY] = [{
        "invoice_number": "INV-1", "lines": [
            {"description": "Room", "account_code": None, "tax_treatment": "SR", "net_amount": 51.49}
        ],
    }]
    ctx = FakeContext(state)
    decision = {"decision": "edit", "edits": {"lines": [
        {"index": 0, "account_code": "6010", "tax_treatment": "ZR", "net_amount": 44.74}
    ]}}
    asyncio.run(nodes.apply_decision_node._func(ctx, decision))
    line = ctx.state[nodes.NORMALIZED_KEY][0]["lines"][0]
    assert line["account_code"] == "6010"
    assert line["tax_treatment"] == "ZR"
    assert line["net_amount"] == 44.74
    assert ctx.state[nodes.APPROVAL_STATUS_KEY] == "edit"


def test_apply_decision_node_edit_does_not_silently_drop_canonical_fields():
    """REGRESSION (live-QA 2026-06): an Edit DTO that writes ``tax_treatment``
    and ``net_amount`` lands on the SAME keys the exporter later reads from
    (``invoice_processing/export/exporters.py`` consumes ``line.tax_treatment``
    and ``line.net_amount``). Pre-fix the edit DTO used ``tax_code`` /
    ``amount`` and silently no-op'd against the canonical exporter fields.
    """
    state = _base_state()
    state[nodes.NORMALIZED_KEY] = [{
        "invoice_number": "INV-1", "lines": [
            {"description": "Room", "account_code": None,
             "tax_treatment": "SR", "net_amount": 100.00}
        ],
    }]
    ctx = FakeContext(state)
    asyncio.run(nodes.apply_decision_node._func(
        ctx,
        {"decision": "edit", "edits": {"lines": [
            {"index": 0, "tax_treatment": "ZR", "net_amount": 88.00}
        ]}},
    ))
    line = ctx.state[nodes.NORMALIZED_KEY][0]["lines"][0]
    assert line["tax_treatment"] == "ZR", "Edit must write canonical key (exporter reads tax_treatment)"
    assert line["net_amount"] == 88.00, "Edit must write canonical key (exporter reads net_amount)"


def test_apply_decision_node_reject_clears_invoices():
    state = _base_state()
    state[nodes.NORMALIZED_KEY] = [{"invoice_number": "INV-1", "lines": []}]
    ctx = FakeContext(state)
    asyncio.run(nodes.apply_decision_node._func(ctx, {"decision": "reject"}))
    assert ctx.state[nodes.NORMALIZED_KEY] == []
    assert ctx.state[nodes.APPROVAL_STATUS_KEY] == "reject"


def test_apply_decision_node_autoapprove_passthrough():
    state = _base_state()
    state[nodes.NORMALIZED_KEY] = [{"invoice_number": "INV-1", "lines": []}]
    ctx = FakeContext(state)
    asyncio.run(nodes.apply_decision_node._func(ctx, None))  # no HITL → node_input is None
    assert ctx.state[nodes.NORMALIZED_KEY] == [{"invoice_number": "INV-1", "lines": []}]


def test_apply_decision_node_approve_passes_through_with_status():
    state = _base_state()
    state[nodes.NORMALIZED_KEY] = [{"invoice_number": "INV-1", "lines": []}]
    ctx = FakeContext(state)
    asyncio.run(nodes.apply_decision_node._func(ctx, {"decision": "approve"}))
    assert ctx.state[nodes.NORMALIZED_KEY] == [{"invoice_number": "INV-1", "lines": []}]
    assert ctx.state[nodes.APPROVAL_STATUS_KEY] == "approve"


@pytest.mark.parametrize("edits_payload, expected_line", [
    ({"lines": []}, {"description": "Room", "account_code": None, "tax_treatment": "SR", "net_amount": 51.49}),
    ({"lines": [{"index": 99, "account_code": "6010"}]}, {"description": "Room", "account_code": None, "tax_treatment": "SR", "net_amount": 51.49}),
    ({"lines": [{"index": "0", "account_code": "6010"}]}, {"description": "Room", "account_code": None, "tax_treatment": "SR", "net_amount": 51.49}),
])
def test_apply_decision_node_edit_edge_cases_no_op(edits_payload, expected_line):
    state = _base_state()
    state[nodes.NORMALIZED_KEY] = [{
        "invoice_number": "INV-1",
        "lines": [{"description": "Room", "account_code": None, "tax_treatment": "SR", "net_amount": 51.49}],
    }]
    ctx = FakeContext(state)
    asyncio.run(nodes.apply_decision_node._func(ctx, {"decision": "edit", "edits": edits_payload}))
    assert ctx.state[nodes.NORMALIZED_KEY][0]["lines"][0] == expected_line


def test_apply_decision_node_edit_with_multi_invoice_logs_warning(caplog):
    import logging
    state = _base_state()
    state[nodes.NORMALIZED_KEY] = [
        {"invoice_number": "INV-1", "lines": [{"description": "A", "account_code": None, "tax_treatment": "SR", "net_amount": 10.0}]},
        {"invoice_number": "INV-2", "lines": [{"description": "B", "account_code": None, "tax_treatment": "SR", "net_amount": 20.0}]},
    ]
    ctx = FakeContext(state)
    with caplog.at_level(logging.WARNING, logger="accounting_agents.nodes"):
        asyncio.run(nodes.apply_decision_node._func(ctx, {"decision": "edit", "edits": {"lines": [{"index": 0, "account_code": "6010"}]}}))
    assert any("invoice_index" in r.message for r in caplog.records)
    # First invoice mutated, second untouched
    assert ctx.state[nodes.NORMALIZED_KEY][0]["lines"][0]["account_code"] == "6010"
    assert ctx.state[nodes.NORMALIZED_KEY][1]["lines"][0]["account_code"] is None


# =========================================================================== #
# deliver_node summary echoes the accounting-software target
# =========================================================================== #


def test_deliver_invoice_names_client_scoped_ledger():
    """Invoice summary names the destination as '<Client> – Ledger FY<fy>'."""
    ctx = FakeContext({
        nodes.LEDGER_ROWS_KEY: {
            "fy": "2026", "kind": "invoice", "software": "Xero",
            "client_name": "Company-A",
            "batches": [{"sheet": "Purchase", "rows": [{"Total Amount": 10}]}],
        }
    })
    asyncio.run(nodes.deliver_node._func(ctx))
    summary = ctx.state[nodes.DELIVER_SUMMARY_KEY]
    assert "Company-A" in summary
    assert "Ledger FY2026" in summary
    # An invoice is a ledger, never a bank statement.
    assert "Bank Statement" not in summary


def test_deliver_bank_names_bank_statement_not_ledger():
    """Bank summary says 'Bank Statement', never 'ledger' (F3)."""
    ctx = FakeContext({
        nodes.LEDGER_ROWS_KEY: {
            "fy": "2025", "kind": "bank", "software": "QBS Ledger",
            "client_name": "Sample Bank Client Pte Ltd",
            "batches": [{
                "sheet": "OCBC - 0001",
                "rows": [
                    {"Description": "BALANCE B/F", "Balance": 100.0, "Currency": "SGD"},
                    {"Date": "01/10/2025", "Description": "FAST PAYMENT",
                     "Withdrawal": 20.0, "Balance": 80.0, "Currency": "SGD"},
                ],
            }],
        }
    })
    asyncio.run(nodes.deliver_node._func(ctx))
    summary = ctx.state[nodes.DELIVER_SUMMARY_KEY]
    assert "Sample Bank Client Pte Ltd" in summary
    assert "Bank Statement FY2025" in summary
    # Must NOT mislabel a bank statement as a ledger.
    assert "ledger" not in summary.lower()


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
    _install_legacy_extract_mock(_doc_bundle_from_ex_bundle(_self_ref_bundle()))

    state = _base_state(**{nodes.DIRECTION_KEY: "self_referential"})
    ctx = FakeContext(state)
    asyncio.run(nodes.extract_invoice_node._func(ctx))

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
    _install_legacy_extract_mock(_doc_bundle_from_ex_bundle(_self_ref_bundle()))

    state = _base_state(**{nodes.DIRECTION_KEY: "unknown"})
    ctx = FakeContext(state)
    asyncio.run(nodes.extract_invoice_node._func(ctx))

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
    _install_legacy_extract_mock(_doc_bundle_from_ex_bundle(
        ExtractedInvoiceBundle(invoices=[_ex_invoice("INV-NORMAL")])
    ))

    state = _base_state(**{nodes.DIRECTION_KEY: "purchase"})
    ctx = FakeContext(state)
    asyncio.run(nodes.extract_invoice_node._func(ctx))

    inv = ctx.state[nodes.NORMALIZED_KEY][0]
    note = inv.get("reconcile_note") or ""
    assert "self-referential" not in note.lower()
    assert "direction unknown" not in note.lower()


# =========================================================================== #
# FX / base_currency threading — Task 3b
# =========================================================================== #


def _ex_usd_invoice(number: str, net: float = 100.0, gst: float = 0.0) -> ExtractedInvoice:
    """A USD-denominated invoice (no GST — overseas supplier)."""
    return ExtractedInvoice(
        doc_type="invoice",
        invoice_number=number,
        invoice_date="2025-03-10",
        currency="USD",
        issuer_name="Overseas Vendor Inc",
        issuer_gst_regno=None,
        bill_to_name="Test Client Pte Ltd",
        lines=[ExtractedLine(description="Consulting", net_amount=net, gst_amount=gst, tax_label="ZR")],
        subtotal=net,
        gst_total=gst,
        total=net + gst,
    )


def _ex_myr_invoice(number: str, net: float = 200.0) -> ExtractedInvoice:
    """An MYR-denominated invoice."""
    return ExtractedInvoice(
        doc_type="invoice",
        invoice_number=number,
        invoice_date="2025-04-01",
        currency="MYR",
        issuer_name="Malaysian Supplier Sdn Bhd",
        issuer_gst_regno=None,
        bill_to_name="Test Client Pte Ltd",
        lines=[ExtractedLine(description="Services", net_amount=net, gst_amount=0.0, tax_label="ZR")],
        subtotal=net,
        gst_total=0.0,
        total=net,
    )


def test_extract_node_usd_doc_sgd_client_books_in_usd():
    """USD doc for an SGD-ledger client with no fx_rate is booked in USD."""
    bundle = ExtractedInvoiceBundle(invoices=[_ex_usd_invoice("USD-INV-001")])
    _install_legacy_extract_mock(_doc_bundle_from_ex_bundle(bundle))

    state = _base_state(**{nodes.DIRECTION_KEY: "purchase", "base_currency": "SGD"})
    ctx = FakeContext(state)
    asyncio.run(nodes.extract_invoice_node._func(ctx))

    inv = ctx.state[nodes.NORMALIZED_KEY][0]
    assert inv.get("needs_fx_review") is False, (
        f"Expected needs_fx_review=False for single-currency USD doc, got {inv.get('needs_fx_review')!r}"
    )
    assert inv.get("reconciled") is True, (
        f"Expected reconciled=True for single-currency USD doc, got {inv.get('reconciled')!r}"
    )
    assert inv.get("currency") == "USD"


def test_extract_node_usd_doc_with_fx_rate_converts_amounts():
    """A USD doc that carries its own fx_rate must be converted to SGD amounts.
    original_total and original_currency must be stored; needs_fx_review must be False."""
    usd_inv = ExtractedInvoice(
        doc_type="invoice",
        invoice_number="USD-RATE-INV",
        invoice_date="2025-03-10",
        currency="USD",
        issuer_name="Overseas Vendor Inc",
        bill_to_name="Test Client Pte Ltd",
        lines=[ExtractedLine(description="Consulting", net_amount=100.0, gst_amount=0.0, tax_label="ZR")],
        subtotal=100.0,
        gst_total=0.0,
        total=100.0,
        fx_rate=1.35,  # document states its own exchange rate
    )
    bundle = ExtractedInvoiceBundle(invoices=[usd_inv])
    _install_legacy_extract_mock(_doc_bundle_from_ex_bundle(bundle))

    state = _base_state(**{nodes.DIRECTION_KEY: "purchase", "base_currency": "SGD"})
    ctx = FakeContext(state)
    asyncio.run(nodes.extract_invoice_node._func(ctx))

    inv = ctx.state[nodes.NORMALIZED_KEY][0]
    assert inv.get("needs_fx_review") is False, (
        f"Expected needs_fx_review=False when fx_rate supplied, got {inv.get('needs_fx_review')!r}"
    )
    assert inv.get("original_currency") == "USD"
    assert inv.get("original_total") == 100.0
    # doc_total should be converted: 100.0 * 1.35 = 135.0
    assert inv.get("doc_total") == pytest.approx(135.0), (
        f"Expected doc_total=135.0, got {inv.get('doc_total')!r}"
    )
    assert inv.get("fx_rate") == pytest.approx(1.35)


def test_extract_node_myr_client_myr_doc_not_flagged():
    """An MYR doc for an MYR-ledger client is the base currency — must NOT be
    flagged as foreign (needs_fx_review must be False, reconciled per normal)."""
    bundle = ExtractedInvoiceBundle(invoices=[_ex_myr_invoice("MYR-INV-001")])
    _install_legacy_extract_mock(_doc_bundle_from_ex_bundle(bundle))

    state = _base_state(**{nodes.DIRECTION_KEY: "purchase", "base_currency": "MYR"})
    ctx = FakeContext(state)
    asyncio.run(nodes.extract_invoice_node._func(ctx))

    inv = ctx.state[nodes.NORMALIZED_KEY][0]
    assert inv.get("needs_fx_review") is False, (
        f"Expected needs_fx_review=False for MYR doc on MYR client, got {inv.get('needs_fx_review')!r}"
    )
    note = inv.get("reconcile_note") or ""
    assert "fx" not in note.lower(), (
        f"Unexpected FX note for same-currency doc: {note!r}"
    )


def test_extract_node_needs_fx_review_routes_to_human_review():
    """A needs_fx_review doc (reconciled=False) must trigger _needs_review so
    approval_gate pauses for human review rather than auto-approving."""
    from accounting_agents.nodes import _needs_review

    # Simulate what extract_invoice_node writes for a USD doc with no rate.
    state = _base_state()
    state[nodes.NORMALIZED_KEY] = [
        {
            "doc_type": "purchase",
            "invoice_number": "FX-INV-001",
            "invoice_date": None,
            "due_date": None,
            "currency": "USD",
            "po_number": None,
            "supplier": {"name": "Overseas Vendor", "country": None, "gst_regno": None, "email": None},
            "customer": {"name": "Test Client", "country": None, "gst_regno": None, "email": None},
            "lines": [{"description": "Consulting", "quantity": None, "unit_amount": None,
                        "net_amount": 100.0, "gst_amount": 0.0, "account_code": None,
                        "item_code": None, "tax_keyword": "ZR",
                        "tax_treatment": None, "tax_confidence": None,
                        "tax_flagged": False, "tax_reason": None}],
            "doc_subtotal": 100.0,
            "doc_gst_total": 0.0,
            "doc_total": 100.0,
            "our_gst_registered": True,
            "fx_rate": None,
            "original_total": 100.0,
            "original_currency": "USD",
            "needs_fx_review": True,
            "reconciled": False,
            "reconcile_note": "needs fx review: document currency USD differs from base currency SGD; no exchange rate available",
        }
    ]

    needs_review, reasons = _needs_review(state)
    assert needs_review is True, (
        f"Expected _needs_review to return True for needs_fx_review doc, got {needs_review!r}"
    )
    assert len(reasons) >= 1, f"Expected at least one reason, got: {reasons!r}"


# =========================================================================== #
# approval_gate — multi-entity predicate (P1-3)
# =========================================================================== #


def _clean_invoice_dict(number: str, n_lines: int = 2) -> dict:
    """A fully reconciled, high-confidence invoice dict (no review flags)."""
    from invoice_processing.export.models import InvoiceLine, NormalizedInvoice

    inv = NormalizedInvoice(
        invoice_number=number,
        reconciled=True,
        lines=[
            InvoiceLine(description=f"Line {i}", tax_confidence=0.99, tax_flagged=False)
            for i in range(n_lines)
        ],
    )
    return nodes._inv_to_dict(inv)


async def _collect_gate_events(state: dict) -> list:
    """Run approval_gate and collect all yielded events."""
    from accounting_agents.nodes import approval_gate as _ag

    ctx = FakeContext(state)
    events = []
    async for event in _ag(ctx):
        events.append(event)
    return events, ctx


def test_multi_entity_clean_bundle_emits_approval_card():
    """A bundle with 3 clean invoices must yield a RequestInput even though
    every individual sub-doc passes deterministic checks (P1-3 fix)."""
    from google.adk.events import RequestInput

    from accounting_agents.nodes import APPROVAL_STATUS_KEY

    state = _base_state()
    state[nodes.NORMALIZED_KEY] = [
        _clean_invoice_dict("SOA-001"),
        _clean_invoice_dict("SOA-002"),
        _clean_invoice_dict("SOA-003"),
    ]
    events, ctx = asyncio.run(_collect_gate_events(state))

    assert len(events) == 1, f"Expected exactly 1 RequestInput, got {events!r}"
    assert isinstance(events[0], RequestInput), f"Expected RequestInput, got {type(events[0])}"
    assert ctx.state.get("approval_message"), "approval_message must be set in state"
    assert ctx.state.get(APPROVAL_STATUS_KEY) != "auto_approved", (
        "Multi-entity bundle must NOT be auto-approved"
    )


def test_single_entity_clean_bundle_still_auto_passes():
    """Regression: N==1 clean invoice still auto-approves (no yield)."""
    from accounting_agents.nodes import APPROVAL_STATUS_KEY

    state = _base_state()
    state[nodes.NORMALIZED_KEY] = [_clean_invoice_dict("INV-SINGLE")]
    events, ctx = asyncio.run(_collect_gate_events(state))

    assert events == [], f"Single clean invoice must NOT yield any event, got {events!r}"
    assert ctx.state.get(APPROVAL_STATUS_KEY) == "auto_approved"


def test_single_entity_flagged_still_emits_card():
    """Regression: N==1 but _needs_review returns True → RequestInput still yielded."""
    from google.adk.events import RequestInput

    from accounting_agents.nodes import APPROVAL_STATUS_KEY
    from invoice_processing.export.models import InvoiceLine, NormalizedInvoice

    inv = NormalizedInvoice(
        invoice_number="INV-FLAG",
        reconciled=True,
        lines=[InvoiceLine(description="Ambiguous", tax_confidence=0.30, tax_flagged=False)],
    )
    state = _base_state()
    state[nodes.NORMALIZED_KEY] = [nodes._inv_to_dict(inv)]
    events, ctx = asyncio.run(_collect_gate_events(state))

    assert len(events) == 1, f"Flagged single invoice must yield 1 RequestInput, got {events!r}"
    assert isinstance(events[0], RequestInput)
    assert ctx.state.get(APPROVAL_STATUS_KEY) != "auto_approved"


# =========================================================================== #
# ADK state-size guard (_guard_state_payload)
# =========================================================================== #


def test_guard_warns_on_count_exceeding_threshold(caplog):
    """(a) A list with > _MAX_STATE_ITEMS items must log a WARNING mentioning the key
    and the threshold."""
    import logging

    oversized = [{"i": i} for i in range(nodes._MAX_STATE_ITEMS + 1)]
    with caplog.at_level(logging.WARNING, logger="accounting_agents.nodes"):
        result = nodes._guard_state_payload(nodes.NORMALIZED_KEY, oversized)

    assert result is oversized  # items returned unchanged
    assert any(
        nodes.NORMALIZED_KEY in r.message and str(nodes._MAX_STATE_ITEMS) in r.message
        for r in caplog.records
    ), f"Expected count-threshold WARNING for key={nodes.NORMALIZED_KEY!r}; records={[r.message for r in caplog.records]}"


def test_guard_no_warning_below_thresholds(caplog):
    """(b) A small payload (under both thresholds) must NOT warn, and the state
    list must be stored unchanged through extract_invoice_node."""
    import logging

    _install_legacy_extract_mock(_doc_bundle_from_ex_bundle(
        ExtractedInvoiceBundle(invoices=[_ex_invoice("INV-GUARD")])
    ))
    ctx = FakeContext(_base_state(**{nodes.DIRECTION_KEY: "purchase"}))
    with caplog.at_level(logging.WARNING, logger="accounting_agents.nodes"):
        asyncio.run(nodes.extract_invoice_node._func(ctx))

    # No size-guard warnings should appear (filter to guard-specific messages only).
    guard_warnings = [
        r for r in caplog.records
        if "state-size" in r.message.lower() or "_MAX_STATE" in r.message
        or ("threshold" in r.message.lower() and nodes.NORMALIZED_KEY in r.message)
    ]
    assert guard_warnings == [], f"Unexpected guard warnings: {[r.message for r in guard_warnings]}"

    stored = ctx.state[nodes.NORMALIZED_KEY]
    assert len(stored) == 1
    assert stored[0]["invoice_number"] == "INV-GUARD"


def test_guard_never_raises_on_unserializable_item(caplog):
    """(c) The guard must never raise even when json.dumps fails (e.g., monkeypatched
    to raise). Items must be returned unchanged."""
    import logging
    from unittest.mock import patch

    items = [{"key": "value"}, {"key": "other"}]
    with patch("json.dumps", side_effect=TypeError("unserializable")):
        with caplog.at_level(logging.WARNING, logger="accounting_agents.nodes"):
            result = nodes._guard_state_payload(nodes.NORMALIZED_KEY, items)

    assert result is items  # data returned unchanged — guard never raises


def test_guard_warns_on_payload_size_exceeding_threshold(caplog):
    """Size threshold: a serialized payload > _MAX_STATE_PAYLOAD_BYTES must warn."""
    import logging

    # Build a list whose JSON serialization exceeds 256 KB.
    big_string = "x" * 1024  # 1 KB per item
    # 300 items × 1 KB ~ 300 KB > 256 KB threshold
    large_items = [{"data": big_string} for _ in range(300)]

    with caplog.at_level(logging.WARNING, logger="accounting_agents.nodes"):
        result = nodes._guard_state_payload(nodes.NORMALIZED_KEY, large_items)

    assert result is large_items  # items unchanged
    assert any(
        nodes.NORMALIZED_KEY in r.message and str(nodes._MAX_STATE_PAYLOAD_BYTES) in r.message
        for r in caplog.records
    ), f"Expected payload-size WARNING; records={[r.message for r in caplog.records]}"



# =========================================================================== #
# extract_invoice_node honors state["review_hint"] on the FIRST extraction
# (Step 7 / ADR-0010 — re_extract_document seeds the hint into run state).
# =========================================================================== #


def test_extract_invoice_document_node_passes_review_hint_to_extractor():
    """A seeded ``review_hint`` steers the FIRST read — orchestrator gets it."""
    captured: dict = {}

    def _recorder(data, mime, **kw):
        captured["hint"] = kw.get("hint")
        from invoice_processing.extract.process_invoice_document import InvoiceProcessResult
        return InvoiceProcessResult(normalized=[], extraction_path="understand")

    nodes.EXTRACT_INVOICE_DOCUMENT_FN = _recorder

    ctx = FakeContext(_base_state(**{
        nodes.DIRECTION_KEY: "purchase",
        nodes.DOC_TYPE_KEY: "invoice",
        "review_hint": "read as a credit note",
    }))
    asyncio.run(nodes.extract_invoice_document_node._func(ctx))

    assert captured["hint"] == "read as a credit note"


def test_extract_invoice_document_node_omits_hint_when_no_review_hint():
    """The normal drop path (no review_hint) calls the orchestrator WITHOUT a hint."""
    captured: dict = {"saw_key": True}

    def _recorder(data, mime, **kw):
        captured["saw_key"] = "hint" in kw and kw["hint"] is not None
        from invoice_processing.extract.process_invoice_document import InvoiceProcessResult
        return InvoiceProcessResult(normalized=[], extraction_path="understand")

    nodes.EXTRACT_INVOICE_DOCUMENT_FN = _recorder

    ctx = FakeContext(_base_state(**{
        nodes.DIRECTION_KEY: "purchase",
        nodes.DOC_TYPE_KEY: "invoice",
    }))
    asyncio.run(nodes.extract_invoice_document_node._func(ctx))

    assert captured["saw_key"] is False


# =========================================================================== #
# Codec round-trip — guards against the prior tax_visible_on_document /
# direction_reason leakage. See accounting_agents/normalized_invoice_codec.py
# and ADR-0014 / ADR-0015 for the fields that MUST survive.
# =========================================================================== #


def _round_trip_invoice(inv):
    """Serialize via nodes shim, deserialize back. Returns (rebuilt_inv, dict)."""
    d = nodes._inv_to_dict(inv)
    return nodes._dict_to_inv(d), d


def test_normalized_invoice_codec_round_trip_preserves_tax_visible_on_document():
    """Regression: prior codec silently dropped this field on Firestore round-trip."""
    from datetime import date as _date

    from invoice_processing.export.models import InvoiceLine, NormalizedInvoice

    inv = NormalizedInvoice(
        invoice_number="INV-001",
        invoice_date=_date(2025, 1, 15),
        tax_visible_on_document=False,   # the exact field the old codec dropped
        direction_reason="Expense claim — currency column matched, no Tax/GST",
        lines=[InvoiceLine(description="Mileage", net_amount=100.0)],
    )

    rebuilt, _ = _round_trip_invoice(inv)

    assert rebuilt.tax_visible_on_document is False, (
        "tax_visible_on_document must survive the round-trip; "
        "the old _dict_to_inv silently dropped it, which broke downstream "
        "categorization and HITL review (see ADR-0014)."
    )
    assert rebuilt.direction_reason == inv.direction_reason
    assert rebuilt.invoice_date == inv.invoice_date
    assert rebuilt.lines[0].description == "Mileage"
    assert rebuilt.lines[0].net_amount == 100.0


def test_normalized_invoice_codec_round_trip_handles_all_fields():
    """Every field on the dataclass must survive intact (round-trip identity)."""
    from datetime import date as _date

    from invoice_processing.export.models import InvoiceLine, NormalizedInvoice, PartyInfo

    inv = NormalizedInvoice(
        doc_type="sales",
        invoice_number="INV-002",
        invoice_date=_date(2025, 3, 1),
        due_date=_date(2025, 3, 31),
        currency="USD",
        po_number="PO-9",
        supplier=PartyInfo(name="Acme Co", country="US", gst_regno="US-EIN-12"),
        customer=PartyInfo(name="Client Co", country="SG", gst_regno="201912345A"),
        lines=[
            InvoiceLine(
                description="Widget",
                quantity=2.0,
                unit_amount=50.0,
                net_amount=100.0,
                gst_amount=9.0,
                account_code="4000",
                item_code="WID-1",
                tax_keyword="SR",
                tax_treatment="SR",
                tax_confidence=0.95,
                tax_flagged=False,
                tax_reason="local standard-rated",
            )
        ],
        doc_subtotal=100.0,
        doc_gst_total=9.0,
        doc_total=109.0,
        our_gst_registered=True,
        fx_rate=1.35,
        original_total=80.74,
        original_currency="USD",
        needs_fx_review=False,
        reconciled=True,
        reconcile_note="OK",
        tax_visible_on_document=True,
        direction_reason="Letterhead shows Client Co as buyer",
    )

    rebuilt, d = _round_trip_invoice(inv)

    assert rebuilt.doc_type == "sales"
    assert rebuilt.invoice_number == "INV-002"
    assert rebuilt.invoice_date == _date(2025, 3, 1)
    assert rebuilt.due_date == _date(2025, 3, 31)
    assert rebuilt.currency == "USD"
    assert rebuilt.po_number == "PO-9"
    assert rebuilt.supplier.name == "Acme Co"
    assert rebuilt.supplier.country == "US"
    assert rebuilt.supplier.gst_regno == "US-EIN-12"
    assert rebuilt.customer.name == "Client Co"
    assert rebuilt.customer.country == "SG"
    assert rebuilt.customer.gst_regno == "201912345A"
    assert len(rebuilt.lines) == 1
    line = rebuilt.lines[0]
    assert line.description == "Widget"
    assert line.quantity == 2.0
    assert line.unit_amount == 50.0
    assert line.net_amount == 100.0
    assert line.gst_amount == 9.0
    assert line.account_code == "4000"
    assert line.item_code == "WID-1"
    assert line.tax_keyword == "SR"
    assert line.tax_treatment == "SR"
    assert line.tax_confidence == 0.95
    assert line.tax_flagged is False
    assert line.tax_reason == "local standard-rated"
    assert rebuilt.doc_subtotal == 100.0
    assert rebuilt.doc_gst_total == 9.0
    assert rebuilt.doc_total == 109.0
    assert rebuilt.our_gst_registered is True
    assert rebuilt.fx_rate == 1.35
    assert rebuilt.original_total == 80.74
    assert rebuilt.original_currency == "USD"
    assert rebuilt.needs_fx_review is False
    assert rebuilt.reconciled is True
    assert rebuilt.reconcile_note == "OK"
    assert rebuilt.tax_visible_on_document is True
    assert rebuilt.direction_reason == "Letterhead shows Client Co as buyer"


def test_normalized_invoice_codec_date_serialized_as_iso_string():
    """The on-the-wire dict shape must use ISO date strings (Firestore-safe)."""
    from datetime import date as _date

    from invoice_processing.export.models import NormalizedInvoice

    inv = NormalizedInvoice(
        invoice_number="INV-DATE",
        invoice_date=_date(2025, 12, 1),
    )
    d = nodes._inv_to_dict(inv)

    assert d["invoice_date"] == "2025-12-01"
    assert d["due_date"] is None  # explicit None, not missing key


def test_normalized_invoice_codec_backward_compatible_with_existing_dicts():
    """Old serialized dicts (missing tax_visible_on_document / direction_reason)
    must still deserialize into valid NormalizedInvoice instances."""
    from invoice_processing.export.models import NormalizedInvoice

    old_dict = {
        "doc_type": "purchase",
        "invoice_number": "INV-OLD",
        "invoice_date": "2024-06-01",
        "due_date": None,
        "currency": "SGD",
        "po_number": None,
        "supplier": {"name": "Old Supplier"},
        "customer": {},
        "lines": [],
        "doc_subtotal": None,
        "doc_gst_total": None,
        "doc_total": None,
        "our_gst_registered": True,
        "fx_rate": 1.0,
        "original_total": None,
        "original_currency": None,
        "needs_fx_review": False,
        "reconciled": True,
        "reconcile_note": None,
        # NOTE: no tax_visible_on_document, no direction_reason — pre-ADR-0014 shape
    }

    rebuilt = nodes._dict_to_inv(old_dict)

    assert isinstance(rebuilt, NormalizedInvoice)
    assert rebuilt.invoice_number == "INV-OLD"
    assert rebuilt.supplier.name == "Old Supplier"
    # New fields default to None — graceful upgrade from older sessions.
    assert rebuilt.tax_visible_on_document is None
    assert rebuilt.direction_reason is None


def test_bank_statement_codec_round_trip():
    """Same round-trip guarantee for bank statements."""
    from datetime import date as _date

    from invoice_processing.export.models import BankStatement, BankTransaction

    stmt = BankStatement(
        bank_name="DBS",
        account_number="001",
        currency="SGD",
        statement_period="2024-01-01 to 2024-01-31",
        opening_balance=100.0,
        closing_balance=500.0,
        transactions=[
            BankTransaction(
                date=_date(2024, 1, 5),
                description="Coffee",
                withdrawal=4.5,
                deposit=None,
                balance=95.5,
                math_ok=True,
                note="",
            ),
            BankTransaction(
                date=_date(2024, 1, 10),
                description="Salary",
                withdrawal=None,
                deposit=400.0,
                balance=495.5,
                math_ok=True,
            ),
        ],
        source_file_id="F-1",
        extract_mode="digital",
        reconciled=True,
        reconcile_note="OK",
    )

    d = nodes._bank_to_dict(stmt)
    rebuilt = nodes._dict_to_bank(d)

    assert rebuilt.bank_name == "DBS"
    assert rebuilt.account_number == "001"
    assert rebuilt.currency == "SGD"
    assert rebuilt.opening_balance == 100.0
    assert rebuilt.closing_balance == 500.0
    assert rebuilt.source_file_id == "F-1"
    assert rebuilt.extract_mode == "digital"
    assert rebuilt.reconciled is True
    assert rebuilt.reconcile_note == "OK"
    assert len(rebuilt.transactions) == 2
    assert rebuilt.transactions[0].date == _date(2024, 1, 5)
    assert rebuilt.transactions[0].withdrawal == 4.5
    assert rebuilt.transactions[0].deposit is None
    assert rebuilt.transactions[1].deposit == 400.0
    assert rebuilt.transactions[1].balance == 495.5
