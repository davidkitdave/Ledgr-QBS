"""Tests for app/blocks.py — pure Block Kit builders."""

from __future__ import annotations

from datetime import date

import pytest

from app.blocks import (
    coa_prompt_blocks,
    onboarding_modal,
    profile_summary_blocks,
    result_card,
    welcome_blocks,
)
from invoice_processing.export.models import NormalizedInvoice, PartyInfo
from invoice_processing.export.routing import DocRoute
from invoice_processing.pipeline import ProcessedDoc


class TestWelcomeBlocks:

    def test_returns_list(self):
        blocks = welcome_blocks()
        assert isinstance(blocks, list)
        assert len(blocks) >= 1

    def test_has_ledgr_setup_open_action(self):
        blocks = welcome_blocks()
        action_ids = []
        for block in blocks:
            for el in block.get("elements", []):
                action_ids.append(el.get("action_id"))
        assert "ledgr_setup_open" in action_ids


class TestOnboardingModal:

    def _modal(self, prefill=None):
        return onboarding_modal(prefill)

    def test_callback_id(self):
        assert self._modal()["callback_id"] == "ledgr_onboarding"

    def test_type_is_modal(self):
        assert self._modal()["type"] == "modal"

    def test_title(self):
        assert self._modal()["title"]["text"] == "Set up client"

    def test_submit_label(self):
        assert self._modal()["submit"]["text"] == "Save"

    def test_exactly_four_input_blocks(self):
        blocks = self._modal()["blocks"]
        input_blocks = [b for b in blocks if b["type"] == "input"]
        assert len(input_blocks) == 4

    def test_block_ids(self):
        blocks = self._modal()["blocks"]
        block_ids = [b["block_id"] for b in blocks if b["type"] == "input"]
        assert block_ids == ["client_name", "fye_month", "accounting_software", "gst_registered"]

    def test_action_ids_are_val(self):
        blocks = self._modal()["blocks"]
        for block in blocks:
            if block["type"] == "input":
                assert block["element"]["action_id"] == "val"

    def test_client_name_is_plain_text_input(self):
        block = next(b for b in self._modal()["blocks"] if b.get("block_id") == "client_name")
        assert block["element"]["type"] == "plain_text_input"

    def test_fye_month_is_static_select_with_12_options(self):
        block = next(b for b in self._modal()["blocks"] if b.get("block_id") == "fye_month")
        assert block["element"]["type"] == "static_select"
        assert len(block["element"]["options"]) == 12

    def test_fye_month_option_values_are_strings_1_to_12(self):
        block = next(b for b in self._modal()["blocks"] if b.get("block_id") == "fye_month")
        values = [opt["value"] for opt in block["element"]["options"]]
        assert values == [str(i) for i in range(1, 13)]

    def test_accounting_software_is_static_select(self):
        block = next(b for b in self._modal()["blocks"] if b.get("block_id") == "accounting_software")
        assert block["element"]["type"] == "static_select"
        option_values = [o["value"] for o in block["element"]["options"]]
        assert "QBS Ledger" in option_values
        assert "Xero" in option_values

    def test_gst_registered_is_radio_buttons(self):
        block = next(b for b in self._modal()["blocks"] if b.get("block_id") == "gst_registered")
        assert block["element"]["type"] == "radio_buttons"
        option_values = [o["value"] for o in block["element"]["options"]]
        assert "yes" in option_values
        assert "no" in option_values

    # --- prefill ---

    def test_prefill_client_name_sets_initial_value(self):
        modal = self._modal(prefill={"client_name": "Acme Pte Ltd"})
        block = next(b for b in modal["blocks"] if b.get("block_id") == "client_name")
        assert block["element"].get("initial_value") == "Acme Pte Ltd"

    def test_prefill_fye_month_sets_initial_option(self):
        modal = self._modal(prefill={"fye_month": 3})
        block = next(b for b in modal["blocks"] if b.get("block_id") == "fye_month")
        initial = block["element"].get("initial_option")
        assert initial is not None
        assert initial["value"] == "3"
        assert initial["text"]["text"] == "March"

    def test_prefill_accounting_software_sets_initial_option(self):
        modal = self._modal(prefill={"accounting_software": "Xero"})
        block = next(b for b in modal["blocks"] if b.get("block_id") == "accounting_software")
        initial = block["element"].get("initial_option")
        assert initial is not None
        assert initial["value"] == "Xero"

    def test_prefill_gst_registered_true_sets_yes(self):
        modal = self._modal(prefill={"gst_registered": True})
        block = next(b for b in modal["blocks"] if b.get("block_id") == "gst_registered")
        initial = block["element"].get("initial_option")
        assert initial is not None
        assert initial["value"] == "yes"

    def test_prefill_gst_registered_false_sets_no(self):
        modal = self._modal(prefill={"gst_registered": False})
        block = next(b for b in modal["blocks"] if b.get("block_id") == "gst_registered")
        initial = block["element"].get("initial_option")
        assert initial is not None
        assert initial["value"] == "no"

    def test_no_prefill_no_initial_values(self):
        modal = self._modal()
        for block in modal["blocks"]:
            if block["type"] == "input":
                el = block["element"]
                assert "initial_value" not in el
                assert "initial_option" not in el


class TestCoaPromptBlocks:

    def test_returns_list(self):
        blocks = coa_prompt_blocks()
        assert isinstance(blocks, list)
        assert len(blocks) >= 1

    def test_has_ledgr_use_standard_coa_action(self):
        blocks = coa_prompt_blocks()
        action_ids = []
        for block in blocks:
            for el in block.get("elements", []):
                action_ids.append(el.get("action_id"))
        assert "ledgr_use_standard_coa" in action_ids

    def test_mentions_profile_saved(self):
        blocks = coa_prompt_blocks()
        text = " ".join(
            str(b.get("text", {}).get("text", "")) for b in blocks
        )
        assert "Profile saved" in text


# --------------------------------------------------------------------------- #
# Rich per-doc completion card (WS1)
# --------------------------------------------------------------------------- #

def _route(fy: int = 2025, workbook: str = "Ledger_FY2025.xlsx") -> DocRoute:
    return DocRoute(
        fy=fy,
        bucket="purchase",
        archive_path=f"client-1/FY{fy}/purchase/doc.pdf",
        workbook=workbook,
        sheet="Purchase",
    )


def _invoice_doc(
    *,
    supplier_name: str = "Acme Supplies Pte Ltd",
    invoice_number: str = "INV-1001",
    invoice_date: date = date(2025, 3, 14),
    doc_total: float = 1234.5,
    currency: str = "SGD",
    reconciled: bool = True,
    note: str = "ok",
    direction: str = "purchase",
) -> ProcessedDoc:
    norm = NormalizedInvoice(
        doc_type="purchase",
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        currency=currency,
        supplier=PartyInfo(name=supplier_name),
        doc_total=doc_total,
    )
    return ProcessedDoc(
        path="/tmp/doc.pdf",
        doc_type="invoice",
        direction=direction,
        normalized=norm,
        bank=None,
        route=_route(),
        reconciled=reconciled,
        note=note,
    )


def _all_block_text(blocks: list) -> str:
    return str(blocks)


class TestResultCardPerDoc:

    def test_renders_per_doc_fields(self):
        doc = _invoice_doc(
            supplier_name="Acme Supplies Pte Ltd",
            invoice_number="INV-1001",
            invoice_date=date(2025, 3, 14),
            doc_total=1234.5,
        )
        blocks = result_card(
            n_files=1, n_processed=1, workbooks=["Ledger_FY2025.xlsx"], errors=[], docs=[doc]
        )
        combined = _all_block_text(blocks)
        assert "Acme Supplies Pte Ltd" in combined
        assert "INV-1001" in combined
        assert "2025-03-14" in combined
        assert "1,234.50" in combined
        # landed-in destination: FY + workbook
        assert "FY2025" in combined
        assert "Ledger_FY2025.xlsx" in combined

    def test_sales_doc_uses_customer_name(self):
        norm = NormalizedInvoice(
            doc_type="sales",
            invoice_number="SO-9",
            invoice_date=date(2025, 1, 2),
            customer=PartyInfo(name="BigBuyer Ltd"),
            doc_total=500.0,
        )
        doc = ProcessedDoc(
            path="/tmp/s.pdf",
            doc_type="invoice",
            direction="sales",
            normalized=norm,
            bank=None,
            route=_route(),
            reconciled=True,
            note="ok",
        )
        blocks = result_card(n_files=1, n_processed=1, workbooks=[], errors=[], docs=[doc])
        assert "BigBuyer Ltd" in _all_block_text(blocks)

    def test_needs_review_marker_when_not_reconciled(self):
        doc = _invoice_doc(reconciled=False, note="needs review: missing supplier GST no.")
        blocks = result_card(n_files=1, n_processed=1, workbooks=[], errors=[], docs=[doc])
        combined = _all_block_text(blocks)
        assert "needs review" in combined
        assert "missing supplier GST no." in combined

    def test_clean_doc_has_no_needs_review_marker(self):
        doc = _invoice_doc(reconciled=True, note="ok")
        blocks = result_card(n_files=1, n_processed=1, workbooks=[], errors=[], docs=[doc])
        assert "needs review" not in _all_block_text(blocks)

    def test_bank_statement_simpler_line(self):
        from invoice_processing.extract.bank_statement_extractor import (
            ExtractedAccount,
            ExtractedBankStatement,
            ExtractedBankTxn,
        )

        bank = ExtractedBankStatement(
            accounts=[
                ExtractedAccount(
                    bank_name="OCBC - 5001",
                    statement_period="01 DEC 2024 - 31 DEC 2024",
                    transactions=[
                        ExtractedBankTxn(description="t1"),
                        ExtractedBankTxn(description="t2"),
                        ExtractedBankTxn(description="t3"),
                    ],
                )
            ]
        )
        doc = ProcessedDoc(
            path="/tmp/b.pdf",
            doc_type="bank_statement",
            direction=None,
            normalized=None,
            bank=bank,
            route=_route(workbook="BankStatement_FY2025.xlsx"),
            reconciled=True,
            note="ok",
        )
        blocks = result_card(n_files=1, n_processed=1, workbooks=[], errors=[], docs=[doc])
        combined = _all_block_text(blocks)
        assert "OCBC - 5001" in combined
        assert "01 DEC 2024 - 31 DEC 2024" in combined
        assert "3 transactions" in combined

    def test_many_docs_truncated_with_more_line(self):
        docs = [_invoice_doc(invoice_number=f"INV-{i}") for i in range(13)]
        blocks = result_card(
            n_files=13, n_processed=13, workbooks=["Ledger_FY2025.xlsx"], errors=[], docs=docs
        )
        # header + 10 doc sections + "+N more" context + workbooks section
        section_blocks = [b for b in blocks if b.get("type") == "section"]
        # header(1) + 10 docs + workbooks(1) = 12 section blocks
        assert len(section_blocks) == 12
        combined = _all_block_text(blocks)
        assert "+3 more" in combined
        # well under the Slack ~50-block ceiling
        assert len(blocks) <= 50

    def test_docs_none_is_backward_compatible(self):
        # No docs → no per-doc section; behaviour identical to summary-only card.
        blocks = result_card(n_files=2, n_processed=2, workbooks=["Ledger_FY2025.xlsx"], errors=[])
        # header + workbooks section only
        section_blocks = [b for b in blocks if b.get("type") == "section"]
        assert len(section_blocks) == 2

    def _section_texts(self, blocks: list) -> list[str]:
        return [b["text"]["text"] for b in blocks if b.get("type") == "section"]

    def test_error_doc_renders_without_crash(self):
        # The real pipeline error doc: doc_type="unknown", normalized=None, bank=None.
        doc = ProcessedDoc(
            path="/tmp/x.pdf", doc_type="unknown", direction=None,
            normalized=None, bank=None, route=_route(),
            reconciled=False, note="ERROR: boom",
        )
        blocks = result_card(n_files=1, n_processed=0, workbooks=[], errors=[], docs=[doc])
        texts = self._section_texts(blocks)
        assert all(t.strip() for t in texts)            # no empty section (Slack rejects those)
        combined = _all_block_text(blocks)
        assert "failed to process" in combined          # distinct from "needs review"
        assert "Unknown" in combined

    def test_long_error_note_truncated_under_slack_limit(self):
        # An unbounded exception note must not exceed Slack's 3000-char section ceiling.
        doc = _invoice_doc(reconciled=False, note="ERROR: " + ("x" * 5000))
        blocks = result_card(n_files=1, n_processed=0, workbooks=[], errors=[], docs=[doc])
        for t in self._section_texts(blocks):
            assert len(t) <= 3000

    def test_bank_empty_accounts_no_crash(self):
        from invoice_processing.extract.bank_statement_extractor import ExtractedBankStatement
        doc = ProcessedDoc(
            path="/tmp/b0.pdf", doc_type="bank_statement", direction=None,
            normalized=None, bank=ExtractedBankStatement(accounts=[]),
            route=_route(workbook="BankStatement_FY2025.xlsx"),
            reconciled=True, note="ok",
        )
        blocks = result_card(n_files=1, n_processed=1, workbooks=[], errors=[], docs=[doc])
        assert "0 transactions" in _all_block_text(blocks)


# --------------------------------------------------------------------------- #
# Profile summary card (Task 3)
# --------------------------------------------------------------------------- #


def _flat_text(blocks):
    return " ".join(
        b.get("text", {}).get("text", "")
        for b in blocks if isinstance(b.get("text"), dict)
    )


def test_profile_summary_shows_all_registered_fields():
    blocks = profile_summary_blocks({
        "client_name": "Auditair International Pte. Ltd.",
        "accounting_software": "Xero",
        "fye_month": 10,
        "gst_registered": False,
    })
    text = _flat_text(blocks)
    assert "Auditair International Pte. Ltd." in text
    assert "Xero" in text
    assert "October" in text          # fye_month 10 -> month name
    assert "Not GST-registered" in text


def test_profile_summary_positive_gst_case():
    blocks = profile_summary_blocks({
        "client_name": "X Ltd",
        "accounting_software": "QBS Ledger",
        "fye_month": 12,
        "gst_registered": True,
    })
    text = _flat_text(blocks)
    assert "GST-registered" in text
    assert "Not GST-registered" not in text
    assert "December" in text


def test_profile_summary_falls_back_for_missing_fields():
    blocks = profile_summary_blocks({})
    text = _flat_text(blocks)
    assert "(unnamed client)" in text
    assert "—" in text  # software + FYE both fall back
    assert "Not GST-registered" in text  # falsy default


def test_profile_summary_accepts_string_fye_month():
    blocks = profile_summary_blocks({
        "client_name": "X Ltd",
        "accounting_software": "Xero",
        "fye_month": "10",  # string variant
        "gst_registered": False,
    })
    text = _flat_text(blocks)
    assert "October" in text

