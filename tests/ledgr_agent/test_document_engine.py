from datetime import date
from typing import Any

from invoice_processing.classify.document_classifier import ClassificationResult
from invoice_processing.export.client_context import ClientContext, CoaAccount
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice, PartyInfo
from invoice_processing.extract.process_invoice_document import InvoiceProcessResult
from ledgr_agent.tools.batch_mapper import map_engine_batch_to_contract
from ledgr_agent.tools.document_engine import process_batch_with_document_spine


def _client() -> ClientContext:
    return ClientContext(
        client_id="acme-auto",
        client_name="Acme Auto Sdn Bhd",
        region="MALAYSIA",
        accounting_software="qbs",
        base_currency="MYR",
        tax_registered=True,
        fye_month=12,
        coa=[
            CoaAccount(
                code="500-010",
                description="Cost of Sales - Vehicle Parts & Accessories",
            )
        ],
        category_mapping={"vehicle_parts": "500-010"},
    )


def _invoice(number: str, inv_date: date, total: float) -> NormalizedInvoice:
    invoice = NormalizedInvoice(
        doc_type="purchase",
        invoice_number=number,
        invoice_date=inv_date,
        currency="MYR",
        supplier=PartyInfo(name="MYCARKIT ASIA SUPPLY (M) SDN BHD"),
        doc_subtotal=total,
        doc_gst_total=0.0,
        doc_total=total,
        reconciled=True,
        reconcile_note="ok",
        document_kind="invoice",
    )
    invoice.lines.append(
        InvoiceLine(
            description=f"Part for {number}",
            net_amount=total,
            gst_amount=0.0,
            account_code="500-010",
        )
    )
    return invoice


def test_document_spine_fans_out_soa_embedded_invoices(tmp_path) -> None:
    pdf = tmp_path / "mycarkit-soa.pdf"
    pdf.write_bytes(b"%PDF fake")

    def classify_fn(path: str) -> ClassificationResult:
        return ClassificationResult(
            doc_type="statement_of_account",
            confidence=0.99,
            issuer_name="MYCARKIT ASIA SUPPLY (M) SDN BHD",
            bill_to_name="Acme Auto Sdn Bhd",
            reason="test",
        )

    def direction_fn(cls, **kwargs) -> str:
        return "purchase"

    def invoice_process_fn(*args, **kwargs) -> InvoiceProcessResult:
        return InvoiceProcessResult(
            normalized=[
                _invoice("IV-10181", date(2025, 12, 1), 700.0),
                _invoice("IV-10276", date(2025, 12, 8), 510.0),
                _invoice("IV-10390", date(2025, 12, 18), 185.0),
                _invoice("IV-10400", date(2025, 12, 19), 230.0),
            ],
            extraction_path="understand",
            skipped_pages=[1],
            input_page_count=5,
        )

    engine = process_batch_with_document_spine(
        [pdf],
        _client(),
        classify_fn=classify_fn,
        direction_fn=direction_fn,
        invoice_process_fn=invoice_process_fn,
        categorize_fn=lambda *_args, **_kwargs: None,
    )
    batch = map_engine_batch_to_contract(
        engine,
        client=_client(),
        source_files=[str(pdf)],
        missing_files=[],
    )

    assert batch.documents_processed == 4
    assert [doc.normalized.invoice_number for doc in engine.docs] == [
        "IV-10181",
        "IV-10276",
        "IV-10390",
        "IV-10400",
    ]
    assert {row["Invoice Number"] for row in batch.export_rows} == {
        "IV-10181",
        "IV-10276",
        "IV-10390",
        "IV-10400",
    }


# ---------------------------------------------------------------------------
# A.4: LLM-direction tests — difflib must NOT override the LLM's answer
# ---------------------------------------------------------------------------


def _client_sg() -> ClientContext:
    """A simple SG client for direction tests."""
    return ClientContext(
        client_id="test-corp",
        client_name="TEST CORP PTE LTD",
        region="SINGAPORE",
        accounting_software="qbs",
        base_currency="SGD",
        tax_registered=True,
        fye_month=12,
        coa=[CoaAccount(code="400-000", description="Sales Revenue")],
        category_mapping={},
    )


def _sales_invoice(number: str = "SL-001") -> NormalizedInvoice:
    """Normalized doc the LLM resolved as SALES."""
    inv = NormalizedInvoice(
        doc_type="sales",          # LLM resolved → sales
        invoice_number=number,
        invoice_date=date(2025, 6, 1),
        currency="SGD",
        customer=PartyInfo(name="BUYER CORP PTE LTD"),
        supplier=PartyInfo(name="TEST CORP PTE LTD"),
        doc_subtotal=1000.0,
        doc_gst_total=90.0,
        doc_total=1090.0,
        reconciled=True,
        reconcile_note="ok",
        document_kind="invoice",
    )
    inv.lines.append(
        InvoiceLine(
            description="Consulting services",
            net_amount=1000.0,
            gst_amount=90.0,
            account_code="400-000",
        )
    )
    return inv


def _unknown_direction_invoice(number: str = "UNK-001") -> NormalizedInvoice:
    """Normalized doc the LLM could NOT resolve — direction_needs_review fired."""
    inv = NormalizedInvoice(
        doc_type="purchase",       # structural default used by extracted_document_to_normalized
        invoice_number=number,
        invoice_date=date(2025, 6, 1),
        currency="SGD",
        supplier=PartyInfo(name="MYSTERY VENDOR"),
        doc_subtotal=500.0,
        doc_gst_total=45.0,
        doc_total=545.0,
        reconciled=False,          # append_direction_review_note sets this False
        reconcile_note=(
            "needs review: direction unknown — could not determine whether "
            "client is issuer or bill-to; defaulted to purchase for routing"
        ),
        document_kind="invoice",
    )
    inv.lines.append(
        InvoiceLine(
            description="Unknown service",
            net_amount=500.0,
            gst_amount=45.0,
            account_code="400-000",
        )
    )
    return inv


def test_llm_direction_sales_wins_over_difflib(tmp_path: Any) -> None:
    """A.4 test 1: LLM resolves direction=sales; difflib would say purchase.
    ProcessedDoc.direction and route.sheet must be Sales, not Purchase.
    """
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF fake")

    # difflib stub: always returns "purchase" (as if fuzzy-name match said buyer)
    def difflib_direction_fn(cls: Any, **kwargs: Any) -> str:
        return "purchase"

    # classify_fn: doc is an invoice (not bank)
    def classify_fn(path: str) -> ClassificationResult:
        return ClassificationResult(
            doc_type="invoice",
            confidence=0.95,
            issuer_name="TEST CORP PTE LTD",
            bill_to_name="BUYER CORP PTE LTD",
            reason="test",
        )

    # invoice_process_fn: LLM decided SALES — normalized.doc_type == "sales"
    captured_direction: list[str] = []

    def invoice_process_fn(*args: Any, **kwargs: Any) -> InvoiceProcessResult:
        captured_direction.append(kwargs.get("direction", "<missing>"))
        return InvoiceProcessResult(
            normalized=[_sales_invoice()],
            extraction_path="understand",
        )

    engine = process_batch_with_document_spine(
        [pdf],
        _client_sg(),
        classify_fn=classify_fn,
        direction_fn=difflib_direction_fn,
        invoice_process_fn=invoice_process_fn,
        categorize_fn=lambda *_a, **_kw: None,
    )

    assert len(engine.docs) == 1
    doc = engine.docs[0]

    # The resolved direction comes from normalized.doc_type, not difflib
    assert doc.normalized.doc_type == "sales", (
        f"normalized.doc_type should be 'sales', got {doc.normalized.doc_type!r}"
    )
    assert doc.direction == "sales", (
        f"ProcessedDoc.direction should be 'sales', got {doc.direction!r}"
    )
    assert doc.route.sheet == "Sales", (
        f"route.sheet should be 'Sales', got {doc.route.sheet!r}"
    )

    # The engine must have called invoice_process_fn with direction="auto"
    assert captured_direction == ["auto"], (
        f"invoice_process_fn should receive direction='auto', got {captured_direction}"
    )


def test_unknown_direction_flags_needs_review_not_silent_purchase(
    tmp_path: Any,
) -> None:
    """A.4 test 2: LLM can't resolve direction → doc flagged needs-review.
    Must NOT be silently booked as a clean purchase.
    """
    pdf = tmp_path / "ambiguous.pdf"
    pdf.write_bytes(b"%PDF fake")

    def classify_fn(path: str) -> ClassificationResult:
        return ClassificationResult(
            doc_type="invoice",
            confidence=0.8,
            issuer_name="MYSTERY VENDOR",
            bill_to_name="MYSTERY VENDOR",   # self-referential / ambiguous
            reason="test",
        )

    def difflib_direction_fn(cls: Any, **kwargs: Any) -> str:
        return "purchase"

    def invoice_process_fn(*args: Any, **kwargs: Any) -> InvoiceProcessResult:
        return InvoiceProcessResult(
            normalized=[_unknown_direction_invoice()],
            extraction_path="understand",
        )

    engine = process_batch_with_document_spine(
        [pdf],
        _client_sg(),
        classify_fn=classify_fn,
        direction_fn=difflib_direction_fn,
        invoice_process_fn=invoice_process_fn,
        categorize_fn=lambda *_a, **_kw: None,
    )

    assert len(engine.docs) == 1
    doc = engine.docs[0]

    # Doc must be flagged needs-review (not reconciled)
    assert doc.reconciled is False, (
        "Unknown-direction doc should not be reconciled=True"
    )
    assert "needs review" in (doc.note or ""), (
        f"note should mention 'needs review', got: {doc.note!r}"
    )
    # The direction-review text must surface (not be overwritten)
    assert "direction" in (doc.note or ""), (
        f"note should mention 'direction', got: {doc.note!r}"
    )


def test_bank_statement_lane_unchanged(tmp_path: Any) -> None:
    """A.4 test 3: bank_statement classification still routes via the bank lane."""
    pdf = tmp_path / "bank.pdf"
    pdf.write_bytes(b"%PDF fake")

    def classify_fn(path: str) -> ClassificationResult:
        return ClassificationResult(
            doc_type="bank_statement",
            confidence=0.99,
            issuer_name="OCBC Bank",
            bill_to_name="TEST CORP PTE LTD",
            reason="test",
        )

    bank_called: list[bool] = []

    def bank_fn(path: str) -> Any:
        bank_called.append(True)
        from invoice_processing.extract.bank_statement_extractor import (
            ExtractedBankStatement,
        )
        return ExtractedBankStatement(accounts=[])

    invoice_called: list[bool] = []

    def invoice_process_fn(*args: Any, **kwargs: Any) -> InvoiceProcessResult:
        invoice_called.append(True)
        return InvoiceProcessResult(normalized=[], extraction_path="understand")

    engine = process_batch_with_document_spine(
        [pdf],
        _client_sg(),
        classify_fn=classify_fn,
        direction_fn=lambda cls, **kw: "purchase",
        bank_fn=bank_fn,
        invoice_process_fn=invoice_process_fn,
        categorize_fn=lambda *_a, **_kw: None,
    )

    assert bank_called, "bank_fn should have been called for bank_statement"
    assert not invoice_called, "invoice_process_fn should NOT be called for bank_statement"
    assert len(engine.docs) == 1
    assert engine.docs[0].doc_type == "bank_statement"


def test_invoice_process_fn_receives_direction_auto(tmp_path: Any) -> None:
    """A.4 test 4: invoice lane always passes direction='auto' to invoice_process_fn."""
    pdf = tmp_path / "any-invoice.pdf"
    pdf.write_bytes(b"%PDF fake")

    def classify_fn(path: str) -> ClassificationResult:
        return ClassificationResult(
            doc_type="invoice",
            confidence=0.9,
            issuer_name="VENDOR A",
            bill_to_name="TEST CORP PTE LTD",
            reason="test",
        )

    captured: list[str] = []

    def invoice_process_fn(*args: Any, **kwargs: Any) -> InvoiceProcessResult:
        captured.append(kwargs.get("direction", "<missing>"))
        inv = NormalizedInvoice(
            doc_type="purchase",
            invoice_number="INV-999",
            invoice_date=date(2025, 1, 1),
            currency="SGD",
            supplier=PartyInfo(name="VENDOR A"),
            doc_subtotal=100.0,
            doc_gst_total=9.0,
            doc_total=109.0,
            reconciled=True,
            reconcile_note="ok",
            document_kind="invoice",
        )
        inv.lines.append(
            InvoiceLine(description="Widget", net_amount=100.0, gst_amount=9.0)
        )
        return InvoiceProcessResult(
            normalized=[inv],
            extraction_path="understand",
        )

    process_batch_with_document_spine(
        [pdf],
        _client_sg(),
        classify_fn=classify_fn,
        direction_fn=lambda cls, **kw: "purchase",
        invoice_process_fn=invoice_process_fn,
        categorize_fn=lambda *_a, **_kw: None,
    )

    assert captured == ["auto"], (
        f"invoice_process_fn must receive direction='auto', got {captured}"
    )


# ---------------------------------------------------------------------------
# A.4 causal tests — real engine chain (monkeypatch EXTRACT_LEDGER_FN seam)
# ---------------------------------------------------------------------------
#
# These tests do NOT stub invoice_process_fn. They monkeypatch
# process_invoice_document.EXTRACT_LEDGER_FN so the real chain:
#   process_invoice_document → _normalize_ledger_bundle
#     → extracted_document_to_normalized → _effective_direction("auto")
#       → append_direction_review_note
# runs end-to-end against a controlled ExtractedDocumentBundle.
#
# Causal guarantee: if document_engine.py is reverted to pass
# direction="purchase" (concrete) instead of direction="auto", the
# _effective_direction branch that reads doc.direction_for_client is
# bypassed and both tests will FAIL — confirming they guard the regression.


def _make_bundle(direction_for_client: str) -> Any:
    """Build a minimal ExtractedDocumentBundle with one document."""
    from invoice_processing.extract.ledger_extract import (
        ExtractedDocument,
        ExtractedDocumentBundle,
        ExtractedDocumentLine,
    )

    doc = ExtractedDocument(
        doc_type="invoice",
        page_range=[1, 1],
        vendor="MYSTERY VENDOR",
        buyer="TEST CORP PTE LTD",
        reference="IV-CAUSAL-001",
        date="2025-06-01",
        currency="SGD",
        lines=[
            ExtractedDocumentLine(
                description="Consulting services",
                net_amount=500.0,
                gst_amount=45.0,
            )
        ],
        subtotal=500.0,
        tax_total=45.0,
        grand_total=545.0,
        tax_visible_on_document=True,
        direction_for_client=direction_for_client,  # type: ignore[arg-type]
    )
    return ExtractedDocumentBundle(documents=[doc])


def test_unknown_direction_real_chain_flags_review(tmp_path: Any, monkeypatch: Any) -> None:
    """A.4 causal test 1: direction_for_client='unknown' → reconciled=False + direction note.

    The real process_invoice_document chain runs; only the Gemini extraction seam
    (EXTRACT_LEDGER_FN) is monkeypatched to return a controlled bundle.

    Causal check: reverts to direction='purchase' (concrete) would bypass
    _effective_direction's "auto" branch, append_direction_review_note would
    not fire, and both assertions would fail.
    """
    import invoice_processing.extract.process_invoice_document as pid_mod

    pdf = tmp_path / "unknown-dir.pdf"
    pdf.write_bytes(b"%PDF fake")

    def classify_fn(path: str) -> ClassificationResult:
        return ClassificationResult(
            doc_type="invoice",
            confidence=0.9,
            issuer_name="MYSTERY VENDOR",
            bill_to_name="TEST CORP PTE LTD",
            reason="test",
        )

    # --- unknown direction bundle ---
    unknown_bundle = _make_bundle("unknown")
    monkeypatch.setattr(pid_mod, "EXTRACT_LEDGER_FN", lambda *_a, **_kw: unknown_bundle)

    engine_unknown = process_batch_with_document_spine(
        [pdf],
        _client_sg(),
        classify_fn=classify_fn,
        direction_fn=lambda cls, **kw: "purchase",
        invoice_process_fn=pid_mod.process_invoice_document,
        categorize_fn=lambda *_a, **_kw: None,
    )

    assert len(engine_unknown.docs) == 1
    doc_unknown = engine_unknown.docs[0]
    assert doc_unknown.reconciled is False, (
        "direction_for_client='unknown' must yield reconciled=False via the real chain"
    )
    note_unknown = doc_unknown.note or ""
    assert "direction" in note_unknown, (
        f"note must mention 'direction', got: {note_unknown!r}"
    )
    assert "needs review" in note_unknown, (
        f"note must mention 'needs review', got: {note_unknown!r}"
    )

    # --- sales direction bundle (control): same chain, resolved direction → clean ---
    sales_bundle = _make_bundle("sales")
    monkeypatch.setattr(pid_mod, "EXTRACT_LEDGER_FN", lambda *_a, **_kw: sales_bundle)

    engine_sales = process_batch_with_document_spine(
        [pdf],
        _client_sg(),
        classify_fn=classify_fn,
        direction_fn=lambda cls, **kw: "purchase",
        invoice_process_fn=pid_mod.process_invoice_document,
        categorize_fn=lambda *_a, **_kw: None,
    )

    assert len(engine_sales.docs) == 1
    doc_sales = engine_sales.docs[0]
    # Control arm: direction resolved to "sales" — no direction-review note
    assert doc_sales.direction == "sales", (
        f"direction should be 'sales' for control arm, got {doc_sales.direction!r}"
    )
    control_note = doc_sales.note or ""
    assert "direction unknown" not in control_note, (
        f"control arm note must not mention 'direction unknown', got: {control_note!r}"
    )
    assert "self-referential" not in control_note, (
        f"control arm note must not mention 'self-referential', got: {control_note!r}"
    )


def test_self_referential_direction_real_chain_flags_review(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A.4 causal test 2: direction_for_client='self_referential' → structural purchase
    routing BUT reconciled=False and note mentions 'self-referential'.

    This must NOT be a silent clean booked purchase.
    """
    import invoice_processing.extract.process_invoice_document as pid_mod

    pdf = tmp_path / "self-ref.pdf"
    pdf.write_bytes(b"%PDF fake")

    def classify_fn(path: str) -> ClassificationResult:
        return ClassificationResult(
            doc_type="invoice",
            confidence=0.9,
            issuer_name="TEST CORP PTE LTD",
            bill_to_name="TEST CORP PTE LTD",
            reason="test",
        )

    self_ref_bundle = _make_bundle("self_referential")
    monkeypatch.setattr(pid_mod, "EXTRACT_LEDGER_FN", lambda *_a, **_kw: self_ref_bundle)

    engine = process_batch_with_document_spine(
        [pdf],
        _client_sg(),
        classify_fn=classify_fn,
        direction_fn=lambda cls, **kw: "purchase",
        invoice_process_fn=pid_mod.process_invoice_document,
        categorize_fn=lambda *_a, **_kw: None,
    )

    assert len(engine.docs) == 1
    doc = engine.docs[0]

    # Structural direction defaults to purchase (routing safety) but must NOT be clean
    assert doc.direction == "purchase", (
        f"structural direction should be 'purchase' for self_referential, got {doc.direction!r}"
    )
    assert doc.reconciled is False, (
        "self_referential doc must not be reconciled=True — it is not a clean booked purchase"
    )
    note = doc.note or ""
    assert "self-referential" in note, (
        f"note must mention 'self-referential', got: {note!r}"
    )
