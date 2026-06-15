"""Tests for app/blocks.py — pure Block Kit builders."""

from __future__ import annotations

from datetime import date

import pytest

import app.native_blocks_compat as compat
from app.blocks import (
    approval_card_blocks,
    coa_prompt_blocks,
    invoice_edit_modal,
    job_summary_text,
    onboarding_modal,
    per_doc_card,
    proactive_redo_blocks,
    proactive_redo_modal,
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
        # header(1) + 10 doc blocks (section or card) + "+N more" context + workbooks(1) = 13
        doc_blocks = [b for b in blocks if b.get("type") in ("section", "card")]
        # header(1) + 10 docs + workbooks(1) = 12
        assert len(doc_blocks) == 12
        assert "+3 more" in str(blocks)
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
        # Use str(blocks) to cover both card.subtext and section.text representations.
        combined = str(blocks)
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


# --------------------------------------------------------------------------- #
# HITL approval card (Task 5)
# --------------------------------------------------------------------------- #


class TestApprovalCardBlocks:

    def _head_text(self, blocks: list) -> str:
        """First section's mrkdwn text — the visible header on the card."""
        for b in blocks:
            if b.get("type") == "section":
                return b.get("text", {}).get("text", "")
        return ""

    def _action_ids(self, blocks: list) -> set:
        ids: set = set()
        for b in blocks:
            if b.get("type") == "actions":
                for el in b.get("elements", []):
                    if el.get("action_id"):
                        ids.add(el["action_id"])
        return ids

    def test_approval_card_names_the_document(self):
        # Task 5: passing doc_label renders it above the existing header line.
        blocks = approval_card_blocks(
            summary="not reconciled (lines $51.49 vs $44.74 + GST)",
            op_id="OP1",
            doc_label="📄 Receipt-Hotel.pdf · Hotel Booking · $51.49",
        )
        head = self._head_text(blocks)
        assert "Receipt-Hotel.pdf" in head

    def test_approval_card_label_appears_above_summary(self):
        # The doc label is a leading line, not appended after the header.
        blocks = approval_card_blocks(
            summary="needs review",
            op_id="OP2",
            doc_label="📄 INV-1001.pdf",
        )
        head = self._head_text(blocks)
        assert head.index("INV-1001.pdf") < head.index("Review needed")

    def test_approval_card_keeps_existing_three_action_buttons(self):
        # Backward-compat: action set must NOT change when a doc_label is added.
        blocks = approval_card_blocks(
            summary="x", op_id="OP3", doc_label="📄 foo.pdf"
        )
        assert self._action_ids(blocks) == {"approve", "edit", "reject"}

    def test_approval_card_no_label_does_not_break(self):
        # No label → behaves like before (backward-compatible default).
        blocks = approval_card_blocks(summary="x", op_id="OP4")
        head = self._head_text(blocks)
        assert "Review needed" in head
        assert "📄" not in head  # no document emoji when no label given

    def test_approval_card_label_does_not_drop_summary(self):
        # The summary must still be present when a label is supplied.
        blocks = approval_card_blocks(
            summary="lines $51.49 vs $44.74",
            op_id="OP5",
            doc_label="📄 foo.pdf",
        )
        head = self._head_text(blocks)
        assert "lines $51.49 vs $44.74" in head


# --------------------------------------------------------------------------- #
# Invoice edit modal (Task 7)
# --------------------------------------------------------------------------- #


class TestInvoiceEditModal:

    def test_callback_id_and_private_metadata(self):
        view = invoice_edit_modal(
            op_id="OP1",
            lines=[{"description": "Room", "account_code": "6010", "tax_code": "SR", "amount": 51.49}],
            coa_options=[("6010", "6010 — Travel"), ("6200", "6200 — Office")],
        )
        assert view["callback_id"] == "ledgr_invoice_edit"
        assert view["private_metadata"] == "OP1"

    def test_one_input_group_per_line_min_three(self):
        # One input group per line (account + tax + amount) → at least 3 input blocks.
        view = invoice_edit_modal(
            op_id="OP1",
            lines=[{"description": "Room", "account_code": "6010", "tax_code": "SR", "amount": 51.49}],
            coa_options=[("6010", "6010 — Travel"), ("6200", "6200 — Office")],
        )
        inputs = [b for b in view["blocks"] if b.get("type") == "input"]
        assert len(inputs) >= 3

    def test_empty_coa_omits_account_select(self):
        """A client with accounting software but no COA must not produce a
        ``static_select`` with empty ``options`` — Slack rejects that and the
        whole modal fails to open. The account-code block is dropped; tax and
        amount stay editable so the modal still opens and works.
        """
        view = invoice_edit_modal(
            op_id="OP1",
            lines=[{"description": "Room", "account_code": None, "tax_code": "SR", "amount": 51.49}],
            coa_options=[],
        )
        # No empty-options static_select anywhere.
        for b in view["blocks"]:
            el = b.get("element", {})
            if el.get("type") == "static_select":
                assert el["options"], "static_select must never have empty options"
        input_block_ids = [b["block_id"] for b in view["blocks"] if b.get("type") == "input"]
        assert "acct_0" not in input_block_ids   # account block omitted
        assert input_block_ids == ["tax_0", "amt_0"]

    def test_block_id_encoding_uses_acct_tax_amt_prefixes(self):
        """Lock the block_id encoding so the modal builder and ``_edits_from_view_state`` stay in sync."""
        view = invoice_edit_modal(
            op_id="OP1",
            lines=[
                {"description": "Room", "account_code": "6010", "tax_code": "SR", "amount": 51.49},
                {"description": "Tax", "account_code": None, "tax_code": "ZR", "amount": 3.60},
            ],
            coa_options=[("6010", "6010 — Travel")],
        )
        input_block_ids = [b["block_id"] for b in view["blocks"] if b.get("type") == "input"]
        assert input_block_ids == [
            "acct_0", "tax_0", "amt_0",
            "acct_1", "tax_1", "amt_1",
        ]


# --------------------------------------------------------------------------- #
# Job summary line for a batch drop (Task 9 / ADR-0007)
# --------------------------------------------------------------------------- #


class TestJobSummaryText:

    def test_includes_total_posted_needs_review_fy(self):
        t = job_summary_text(total=10, posted=7, needs_review=3, software="Xero", fy="2026")
        assert "10" in t and "7" in t and "3" in t and "FY2026" in t

    def test_bank_kind_says_bank_statement_not_ledger(self):
        # A bank batch must say "bank statement", never "ledger" (F3 consistency).
        t = job_summary_text(total=1, posted=1, fy="2025", kind="bank")
        assert "bank statement" in t
        assert "ledger" not in t

    def test_invoice_kind_says_ledger(self):
        t = job_summary_text(total=1, posted=1, fy="2026", kind="invoice")
        assert "ledger" in t
        assert "bank statement" not in t

    def test_singular_when_one_document(self):
        # No trailing 's' on "document" when total == 1.
        t = job_summary_text(total=1, posted=1, needs_review=0, software="Xero", fy="2026")
        assert "document " in t or "document." in t or "document—" in t or "1 document" in t
        assert "documents" not in t

    def test_omits_needs_review_suffix_when_zero(self):
        t = job_summary_text(total=2, posted=2, needs_review=0, software="Xero", fy="2026")
        assert "need your review" not in t

    def test_blank_software_and_fy_omitted(self):
        # No extra "to your  ledger" / " FY" tokens when software/fy are blank.
        t = job_summary_text(total=3, posted=2, needs_review=1, software="", fy="")
        assert "your ledger" in t or "ledger" in t  # headline still mentions the ledger
        # No double-space artefacts from the dropped prefixes.
        assert "to your  " not in t
        assert " FY" not in t

    def test_rejected_appears_in_summary(self):
        t = job_summary_text(total=3, posted=1, needs_review=0, rejected=2)
        assert "1 posted" in t
        assert "2 rejected" in t
        assert "Received 3" in t

    def test_all_rejected_shows_nothing_new(self):
        t = job_summary_text(total=1, posted=0, needs_review=0, rejected=1)
        assert "1 rejected" in t
        assert "0 posted" not in t

    def test_zero_posted_zero_rejected_says_nothing_new(self):
        t = job_summary_text(total=1, posted=0, needs_review=0, rejected=0)
        assert "nothing new" in t

    def test_duplicates_appears_in_summary(self):
        t = job_summary_text(total=3, posted=1, needs_review=0, duplicates=2)
        assert "1 posted" in t
        assert "2 already recorded" in t

    def test_all_duplicates_no_posted(self):
        t = job_summary_text(total=2, posted=0, needs_review=0, duplicates=2)
        assert "2 already recorded" in t
        assert "posted" not in t

    def test_mixed_posted_duplicates_rejected(self):
        t = job_summary_text(total=5, posted=2, needs_review=0, rejected=1, duplicates=2)
        assert "2 posted" in t
        assert "2 already recorded" in t
        assert "1 rejected" in t


# =========================================================================== #
# Step 8: proactive post-delivery re-extract offer card + hint modal
# =========================================================================== #


class TestProactiveRedoBlocks:

    def _text(self, blocks: list) -> str:
        return blocks[0]["text"]["text"]

    def test_humanizes_reasons_and_carries_file_id(self):
        blocks = proactive_redo_blocks(
            "F-123",
            ["unreconciled: Invoice (FX off by 0.02)", "low_classify_confidence"],
        )
        text = self._text(blocks)
        # Humanized phrases appear; raw machine prefixes do NOT leak through.
        assert "the totals didn't reconcile" in text
        assert "wasn't confident how to categorise it" in text
        assert "unreconciled:" not in text
        assert "low_classify_confidence" not in text
        # The single action button carries the file_id as its value + the right id.
        actions = [b for b in blocks if b.get("type") == "actions"][0]
        button = actions["elements"][0]
        assert button["action_id"] == "proactive_redo"
        assert button["value"] == "F-123"
        assert button["text"]["text"] == "Re-extract with a hint"

    def test_dedupes_repeated_phrases(self):
        # Two reasons that map to the same human phrase render the phrase once.
        blocks = proactive_redo_blocks(
            "F-9",
            ["lines_empty: Invoice", "lines_empty: Receipt"],
        )
        text = self._text(blocks)
        assert text.count("a document had no line items") == 1

    def test_unknown_reason_falls_back_to_deslugged(self):
        blocks = proactive_redo_blocks("F-1", ["some_new_signal"])
        assert "some new signal" in self._text(blocks)

    def test_empty_reasons_still_offers_button(self):
        blocks = proactive_redo_blocks("F-1", [])
        text = self._text(blocks)
        assert "want me to re-read it" in text
        actions = [b for b in blocks if b.get("type") == "actions"][0]
        assert actions["elements"][0]["value"] == "F-1"


class TestProactiveRedoModal:

    def test_callback_id_and_private_metadata_carry_file_id(self):
        view = proactive_redo_modal("F-XYZ")
        assert view["type"] == "modal"
        assert view["callback_id"] == "ledgr_proactive_redo"
        assert view["private_metadata"] == "F-XYZ"

    def test_has_single_hint_input(self):
        view = proactive_redo_modal("F-1")
        inputs = [b for b in view["blocks"] if b.get("type") == "input"]
        assert len(inputs) == 1
        assert inputs[0]["block_id"] == "hint_block"
        assert inputs[0]["element"]["action_id"] == "hint_input"


# --------------------------------------------------------------------------- #
# per_doc_card tests
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _reset_compat_cache():
    compat._reset_for_tests()
    yield
    compat._reset_for_tests()


class TestPerDocCardNative:

    @pytest.fixture(autouse=True)
    def _force_native(self, monkeypatch):
        monkeypatch.setenv("LEDGR_NATIVE_BLOCKS", "1")

    def test_invoice_produces_one_card_block(self):
        doc = _invoice_doc()
        blocks = per_doc_card(doc)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "card"

    def test_invoice_title_is_vendor_name(self):
        doc = _invoice_doc(supplier_name="Acme Inc")
        card = per_doc_card(doc)[0]
        assert card["title"] == "Acme Inc"
        assert isinstance(card["title"], str)

    def test_invoice_subtitle_contains_number_and_date(self):
        doc = _invoice_doc(invoice_number="INV-007", invoice_date=date(2025, 9, 15))
        card = per_doc_card(doc)[0]
        assert "Invoice #INV-007" in card["subtitle"]
        assert "2025-09-15" in card["subtitle"]

    def test_invoice_body_contains_total_and_workbook(self):
        doc = _invoice_doc(doc_total=1234.5)
        card = per_doc_card(doc)[0]
        assert "1,234.50" in card["body"]
        assert "FY2025" in card["body"]
        assert "Ledger_FY2025.xlsx" in card["body"]

    def test_three_actions_in_order(self):
        doc = _invoice_doc()
        card = per_doc_card(doc, actions=["reextract", "edit", "view_row"])[0]
        acts = card["actions"]
        assert len(acts) == 3
        assert acts[0]["action_id"] == "ledgr_per_doc_reextract"
        assert acts[0]["text"]["text"] == "Re-extract"
        assert acts[1]["action_id"] == "ledgr_per_doc_edit"
        assert acts[1]["text"]["text"] == "Edit"
        assert acts[2]["action_id"] == "ledgr_per_doc_view_row"
        assert acts[2]["text"]["text"] == "View row"

    def test_empty_actions_omits_actions_key(self):
        doc = _invoice_doc()
        card = per_doc_card(doc, actions=[])[0]
        assert "actions" not in card

    def test_no_actions_arg_omits_actions_key(self):
        doc = _invoice_doc()
        card = per_doc_card(doc)[0]
        assert "actions" not in card

    def test_reconciled_false_adds_needs_review_subtext(self):
        doc = _invoice_doc(reconciled=False, note="missing supplier GST no.")
        card = per_doc_card(doc)[0]
        assert "needs review" in card["subtext"]
        assert "missing supplier GST no." in card["subtext"]

    def test_error_note_uses_failed_to_process_label(self):
        doc = _invoice_doc(reconciled=False, note="ERROR: pipeline blew up")
        card = per_doc_card(doc)[0]
        assert "failed to process" in card["subtext"]

    def test_clean_doc_has_no_subtext(self):
        doc = _invoice_doc(reconciled=True)
        card = per_doc_card(doc)[0]
        assert "subtext" not in card

    def test_body_length_cap_with_long_workbook(self):
        long_wb = "A" * 300 + ".xlsx"
        doc = ProcessedDoc(
            path="/tmp/d.pdf",
            doc_type="invoice",
            direction="purchase",
            normalized=NormalizedInvoice(
                doc_type="purchase",
                invoice_number="INV-1",
                invoice_date=date(2025, 1, 1),
                supplier=PartyInfo(name="Vendor"),
                doc_total=99.0,
            ),
            bank=None,
            route=DocRoute(fy=2025, bucket="purchase", archive_path="x", workbook=long_wb, sheet="P"),
            reconciled=True,
            note="ok",
        )
        card = per_doc_card(doc)[0]
        assert len(card["body"]) <= 200
        assert card["body"].endswith("…")

    def test_bank_doc_title_is_bank_name(self):
        from invoice_processing.extract.bank_statement_extractor import (
            ExtractedAccount,
            ExtractedBankStatement,
        )
        bank = ExtractedBankStatement(
            accounts=[ExtractedAccount(bank_name="OCBC - 5001", statement_period="DEC 2024")]
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
        card = per_doc_card(doc)[0]
        assert card["title"] == "OCBC - 5001"
        assert "DEC 2024" in card.get("subtitle", "") or "DEC 2024" in card.get("body", "")

    def test_bank_doc_type_is_card(self):
        from invoice_processing.extract.bank_statement_extractor import ExtractedBankStatement
        doc = ProcessedDoc(
            path="/tmp/b.pdf",
            doc_type="bank_statement",
            direction=None,
            normalized=None,
            bank=ExtractedBankStatement(accounts=[]),
            route=_route(workbook="BankStatement_FY2025.xlsx"),
            reconciled=True,
            note="ok",
        )
        blocks = per_doc_card(doc)
        assert blocks[0]["type"] == "card"


class TestPerDocCardFallback:

    @pytest.fixture(autouse=True)
    def _force_fallback(self, monkeypatch):
        monkeypatch.setenv("LEDGR_NATIVE_BLOCKS", "0")

    def test_fallback_produces_section_block(self):
        doc = _invoice_doc()
        blocks = per_doc_card(doc)
        assert blocks[0]["type"] == "section"

    def test_fallback_with_actions_appends_actions_block(self):
        doc = _invoice_doc()
        blocks = per_doc_card(doc, actions=["reextract", "edit"])
        assert len(blocks) == 2
        assert blocks[1]["type"] == "actions"
        action_ids = [el["action_id"] for el in blocks[1]["elements"]]
        assert "ledgr_per_doc_reextract" in action_ids
        assert "ledgr_per_doc_edit" in action_ids

    def test_fallback_no_actions_no_actions_block(self):
        doc = _invoice_doc()
        blocks = per_doc_card(doc, actions=[])
        assert len(blocks) == 1
        assert blocks[0]["type"] == "section"

    def test_fallback_section_text_is_mrkdwn(self):
        doc = _invoice_doc(supplier_name="Fallback Vendor")
        blocks = per_doc_card(doc)
        assert blocks[0]["text"]["type"] == "mrkdwn"
        assert "Fallback Vendor" in blocks[0]["text"]["text"]


class TestResultCardNativeMode:

    @pytest.fixture(autouse=True)
    def _force_native(self, monkeypatch):
        monkeypatch.setenv("LEDGR_NATIVE_BLOCKS", "1")

    def test_needs_review_doc_gets_actions(self):
        docs = [
            _invoice_doc(reconciled=True),
            _invoice_doc(reconciled=True),
            _invoice_doc(reconciled=False, note="bad total"),
        ]
        blocks = result_card(
            n_files=3, n_processed=3, workbooks=[], errors=[], docs=docs, channel_id="C1"
        )
        card_blocks = [b for b in blocks if b.get("type") == "card"]
        assert len(card_blocks) == 3
        clean_cards = [c for c in card_blocks if "actions" not in c]
        review_cards = [c for c in card_blocks if "actions" in c]
        assert len(clean_cards) == 2
        assert len(review_cards) == 1
        action_ids = [a["action_id"] for a in review_cards[0]["actions"]]
        assert "ledgr_per_doc_reextract" in action_ids
        assert "ledgr_per_doc_edit" in action_ids

    def test_clean_docs_have_no_actions(self):
        docs = [_invoice_doc(reconciled=True) for _ in range(3)]
        blocks = result_card(
            n_files=3, n_processed=3, workbooks=[], errors=[], docs=docs, channel_id="C1"
        )
        for card in (b for b in blocks if b.get("type") == "card"):
            assert "actions" not in card


class TestResultCardFallbackMode:

    @pytest.fixture(autouse=True)
    def _force_fallback(self, monkeypatch):
        monkeypatch.setenv("LEDGR_NATIVE_BLOCKS", "0")

    def test_structure_matches_legacy_section_layout(self):
        docs = [_invoice_doc(), _invoice_doc()]
        blocks = result_card(
            n_files=2, n_processed=2, workbooks=["Ledger_FY2025.xlsx"], errors=[], docs=docs
        )
        section_blocks = [b for b in blocks if b.get("type") == "section"]
        # header(1) + 2 doc sections + workbooks(1) = 4
        assert len(section_blocks) == 4

    def test_needs_review_in_fallback_appends_actions_block(self):
        docs = [_invoice_doc(reconciled=False, note="check totals")]
        blocks = result_card(
            n_files=1, n_processed=1, workbooks=[], errors=[], docs=docs
        )
        action_blocks = [b for b in blocks if b.get("type") == "actions"]
        assert len(action_blocks) == 1
        action_ids = [el["action_id"] for el in action_blocks[0]["elements"]]
        assert "ledgr_per_doc_reextract" in action_ids

