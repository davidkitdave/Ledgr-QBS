"""Tests for app/blocks.py — pure Block Kit builders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

import app.native_blocks_compat as compat
import urllib.parse

import pytest

from app.blocks import (
    _dedup_value,
    dedup_callout_card,
    job_summary_text,
    ledger_preview_data_table,
    onboarding_modal,
    processing_plan_headline,
    profile_summary_blocks,
    welcome_blocks,
)
from ledgr_slack.export.models import NormalizedInvoice, PartyInfo
from ledgr_slack.export.routing import DocRoute


@dataclass
class ProcessedDoc:
    path: str
    doc_type: str
    direction: Optional[str]
    normalized: Optional[NormalizedInvoice]
    bank: object | None
    route: DocRoute
    reconciled: bool
    note: str


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

    def test_exactly_five_input_blocks(self):
        blocks = self._modal()["blocks"]
        input_blocks = [b for b in blocks if b["type"] == "input"]
        assert len(input_blocks) == 5

    def test_block_ids(self):
        blocks = self._modal()["blocks"]
        block_ids = [b["block_id"] for b in blocks if b["type"] == "input"]
        assert block_ids == [
            "client_name",
            "region",
            "fye_month",
            "accounting_software",
            "gst_registered",
        ]

    def test_action_ids_are_val(self):
        blocks = self._modal()["blocks"]
        for block in blocks:
            if block["type"] == "input":
                assert block["element"]["action_id"] == "val"

    def test_region_is_static_select_with_supported_regions(self):
        from ledgr_slack.jurisdiction import supported_regions

        block = next(b for b in self._modal()["blocks"] if b.get("block_id") == "region")
        assert block["element"]["type"] == "static_select"
        option_values = [o["value"] for o in block["element"]["options"]]
        assert option_values == supported_regions()

    def test_prefill_region_sets_initial_option(self):
        modal = self._modal(prefill={"region": "MALAYSIA"})
        block = next(b for b in modal["blocks"] if b.get("block_id") == "region")
        initial = block["element"].get("initial_option")
        assert initial is not None
        assert initial["value"] == "MALAYSIA"

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
# Dedup callout card (batch bank-statement replace/keep)
# =========================================================================== #

_EXISTING = {"rows": 12, "date_range": "September 2025", "workbook": "Acme - Ledger_FY2025.xlsx"}
_INCOMING = {"rows": 8, "date_range": "September 2025", "file_label": "Invoice-Sept.pdf"}


class TestDedupCalloutCardNative:

    @pytest.fixture(autouse=True)
    def _force_native(self, monkeypatch):
        monkeypatch.setenv("LEDGR_NATIVE_BLOCKS", "1")

    def test_returns_one_card_block(self):
        blocks = dedup_callout_card(
            vendor="Acme Supplies", fy=2025, month="September 2025",
            existing=_EXISTING, incoming=_INCOMING,
        )
        assert len(blocks) == 1
        assert blocks[0]["type"] == "card"

    def test_title_is_mrkdwn_object(self):
        card = dedup_callout_card(
            vendor="Acme Supplies", fy=2025, month="September 2025",
            existing=_EXISTING, incoming=_INCOMING,
        )[0]
        title = card["title"]
        assert isinstance(title, dict)
        assert title["type"] == "mrkdwn"
        assert "September 2025" in title["text"]
        assert "Acme Supplies" in title["text"]

    def test_subtitle_is_mrkdwn_object_with_fy_and_workbook(self):
        card = dedup_callout_card(
            vendor="Acme Supplies", fy=2025, month="September 2025",
            existing=_EXISTING, incoming=_INCOMING,
        )[0]
        subtitle = card["subtitle"]
        assert isinstance(subtitle, dict)
        assert subtitle["type"] == "mrkdwn"
        assert "FY2025" in subtitle["text"]
        assert "Acme - Ledger_FY2025.xlsx" in subtitle["text"]

    def test_body_is_mrkdwn_object_with_row_counts(self):
        card = dedup_callout_card(
            vendor="Acme Supplies", fy=2025, month="September 2025",
            existing=_EXISTING, incoming=_INCOMING,
        )[0]
        body = card["body"]
        assert isinstance(body, dict)
        assert body["type"] == "mrkdwn"
        assert "12" in body["text"]
        assert "8" in body["text"]

    def test_two_actions_replace_is_danger(self):
        card = dedup_callout_card(
            vendor="Acme Supplies", fy=2025, month="September 2025",
            existing=_EXISTING, incoming=_INCOMING,
        )[0]
        actions = card["actions"]
        assert len(actions) == 2
        replace_btn = actions[0]
        assert replace_btn["action_id"] == "ledgr_dedup_replace"
        assert replace_btn.get("style") == "danger"
        keep_btn = actions[1]
        assert keep_btn["action_id"] == "ledgr_dedup_keep"

    def test_button_values_round_trip(self):
        blocks = dedup_callout_card(
            vendor="Acme Supplies", fy=2025, month="September 2025",
            existing=_EXISTING, incoming=_INCOMING, op_id="OP-42",
        )
        card = blocks[0]
        btn_value = card["actions"][0]["value"]
        parts = btn_value.split("|")
        assert urllib.parse.unquote(parts[0]) == "Acme Supplies"
        assert parts[1] == "2025"
        assert urllib.parse.unquote(parts[2]) == "September 2025"
        assert parts[3] == "OP-42"

    def test_button_value_never_empty(self):
        blocks = dedup_callout_card(
            vendor="", fy=0, month="", existing=_EXISTING, incoming=_INCOMING,
        )
        for btn in blocks[0]["actions"]:
            assert btn["value"]
            assert btn["value"] != ""

    def test_body_capped_at_500_chars_with_ellipsis(self):
        # date_range must be long enough to push the body over 500 chars.
        long_dr = "D" * 550
        existing_long = {"rows": 1, "date_range": long_dr, "workbook": "Ledger_FY2025.xlsx"}
        card = dedup_callout_card(
            vendor="V", fy=2025, month="Jan 2025",
            existing=existing_long, incoming=_INCOMING,
        )[0]
        assert len(card["body"]["text"]) <= 500
        assert card["body"]["text"].endswith("…")

    def test_both_buttons_share_same_value(self):
        card = dedup_callout_card(
            vendor="Acme", fy=2025, month="Oct 2025",
            existing=_EXISTING, incoming=_INCOMING,
        )[0]
        assert card["actions"][0]["value"] == card["actions"][1]["value"]


class TestDedupCalloutCardFallback:

    @pytest.fixture(autouse=True)
    def _force_fallback(self, monkeypatch):
        monkeypatch.setenv("LEDGR_NATIVE_BLOCKS", "0")

    def test_returns_section_plus_actions(self):
        blocks = dedup_callout_card(
            vendor="Acme Supplies", fy=2025, month="September 2025",
            existing=_EXISTING, incoming=_INCOMING,
        )
        assert len(blocks) == 2
        assert blocks[0]["type"] == "section"
        assert blocks[1]["type"] == "actions"

    def test_section_text_contains_title_and_body(self):
        blocks = dedup_callout_card(
            vendor="Acme Supplies", fy=2025, month="September 2025",
            existing=_EXISTING, incoming=_INCOMING,
        )
        text = blocks[0]["text"]["text"]
        assert "September 2025" in text
        assert "Acme Supplies" in text
        assert "12" in text

    def test_fallback_has_same_two_buttons(self):
        blocks = dedup_callout_card(
            vendor="Acme", fy=2025, month="Oct 2025",
            existing=_EXISTING, incoming=_INCOMING,
        )
        action_ids = [el["action_id"] for el in blocks[1]["elements"]]
        assert "ledgr_dedup_replace" in action_ids
        assert "ledgr_dedup_keep" in action_ids


class TestDedupValue:

    def test_round_trips_vendor_and_month(self):
        val = _dedup_value("Acme & Sons", 2025, "September 2025", "OP-1")
        parts = val.split("|")
        assert urllib.parse.unquote(parts[0]) == "Acme & Sons"
        assert parts[1] == "2025"
        assert urllib.parse.unquote(parts[2]) == "September 2025"
        assert parts[3] == "OP-1"

    def test_none_op_id_gives_dash_not_empty(self):
        val = _dedup_value("V", 2025, "Jan 2025", None)
        parts = val.split("|")
        assert parts[3] == "-"

    def test_empty_vendor_gives_dash(self):
        val = _dedup_value("", 2025, "Jan 2025", None)
        parts = val.split("|")
        assert parts[0] == "-"


# --------------------------------------------------------------------------- #
# Commit 4 (updated): ledger_preview_data_table — per-software / per-sheet
# --------------------------------------------------------------------------- #

from app.blocks import preview_column_spec

# QBS Purchase exporter row shape
_QBS_PURCHASE_ROWS = [
    {"Invoice Date": "15/09/2025", "Invoice Number": "INV-001", "Vendor Name": "Acme Trading",
     "Description": "Consulting", "Account Code / COA": "6090",
     "Sub Total": 1132.11, "Tax Amount": 102.39, "Total Amount": 1234.50},
    {"Invoice Date": "18/09/2025", "Invoice Number": "BIL-7788", "Vendor Name": "BetaCo",
     "Description": "SaaS", "Account Code / COA": "6090",
     "Sub Total": 450.00, "Tax Amount": 0.00, "Total Amount": 450.00},
    {"Invoice Date": "22/09/2025", "Invoice Number": "INV-002", "Vendor Name": "Gamma Pte",
     "Description": "Equipment", "Account Code / COA": "4000",
     "Sub Total": 920.00, "Tax Amount": 82.80, "Total Amount": 1002.80},
    {"Invoice Date": "25/09/2025", "Invoice Number": "SVC-09", "Vendor Name": "Delta Co",
     "Description": "Maintenance", "Account Code / COA": "6100",
     "Sub Total": 200.00, "Tax Amount": 18.00, "Total Amount": 218.00},
    {"Invoice Date": "28/09/2025", "Invoice Number": "BIL-2025", "Vendor Name": "Epsilon",
     "Description": "Freight", "Account Code / COA": "6200",
     "Sub Total": 80.00, "Tax Amount": 0.00, "Total Amount": 80.00},
]

# Xero Purchase exporter row shape
_XERO_PURCHASE_ROWS = [
    {"*ContactName": "Acme Trading Pte Ltd", "*InvoiceNumber": "INV-2025-0042",
     "*InvoiceDate": "15/09/2025", "Description": "Consulting services",
     "*AccountCode": "6090", "*TaxType": "SR", "*UnitAmount": 1132.11, "Total": 1234.50},
    {"*ContactName": "BetaCo", "*InvoiceNumber": "BIL-7788",
     "*InvoiceDate": "18/09/2025", "Description": "SaaS subscription",
     "*AccountCode": "6090", "*TaxType": "ZR", "*UnitAmount": 450.00, "Total": 450.00},
]

# Bank statement row shape
_BANK_ROWS = [
    {"Date": "15/09/2025", "Description": "Cheque deposit",
     "Withdrawal": 0.0, "Deposit": 5000.00, "Balance": 12340.50, "Currency": "SGD"},
    {"Date": "17/09/2025", "Description": "Vendor payment ACME",
     "Withdrawal": 1234.50, "Deposit": 0.0, "Balance": 11106.00, "Currency": "SGD"},
]


# --------------------------------------------------------------------------- #
# preview_column_spec
# --------------------------------------------------------------------------- #

class TestPreviewColumnSpec:

    def test_xero_purchase_returns_9_cols_starting_with_contact(self):
        spec = preview_column_spec(software="xero", sheet="Purchase")
        assert len(spec) == 9
        assert spec[0].row_key == "*ContactName"
        assert spec[0].header == "Contact"
        assert spec[-1].header == "Currency"

    def test_xero_sales_uses_star_description(self):
        spec = preview_column_spec(software="xero", sheet="Sales")
        desc_col = next(c for c in spec if "Description" in c.header)
        assert desc_col.row_key == "*Description"

    def test_qbs_purchase_uses_vendor_name(self):
        spec = preview_column_spec(software="qbs_ledger", sheet="Purchase")
        vendor_col = next(c for c in spec if c.row_key == "Vendor Name")
        assert vendor_col.header == "Vendor"

    def test_qbs_sales_uses_customer_name(self):
        spec = preview_column_spec(software="qbs_ledger", sheet="Sales")
        cust_col = next(c for c in spec if c.row_key == "Customer Name")
        assert cust_col.header == "Customer"

    def test_bank_sheet_returns_6_col_spec(self):
        spec = preview_column_spec(software="qbs_ledger", sheet="OCBC SGD")
        assert len(spec) == 6
        headers = [c.header for c in spec]
        assert "Withdrawal" in headers
        assert "Deposit" in headers

    def test_bank_sheet_any_software(self):
        spec_xero = preview_column_spec(software="xero", sheet="DBS MYR")
        spec_qbs = preview_column_spec(software="qbs_ledger", sheet="DBS MYR")
        assert spec_xero == spec_qbs  # bank spec is software-agnostic

    def test_unknown_software_returns_empty_preview_spec(self):
        spec_unknown = preview_column_spec(software="some_future_tool", sheet="Purchase")
        spec_qbs = preview_column_spec(software="qbs_ledger", sheet="Purchase")
        assert spec_unknown == []
        assert spec_qbs

    def test_normalised_software_strings_resolve(self):
        # "qbs" and "QBS Ledger" and "qbs_ledger" all map to the same spec.
        assert (
            preview_column_spec(software="qbs", sheet="Purchase")
            == preview_column_spec(software="QBS Ledger", sheet="Purchase")
            == preview_column_spec(software="qbs_ledger", sheet="Purchase")
        )
        assert (
            preview_column_spec(software="Xero", sheet="Purchase")
            == preview_column_spec(software="xero", sheet="Purchase")
        )

    def test_withdrawal_deposit_balance_are_raw_number(self):
        spec = preview_column_spec(software="qbs_ledger", sheet="OCBC SGD")
        num_keys = {c.row_key for c in spec if c.cell_type == "raw_number"}
        assert {"Withdrawal", "Deposit", "Balance"} == num_keys


class TestPreviewColumnSpecSlackLimit:
    """WS-5.2 fix: Slack's data_table block allows at most 20 columns. The
    AutoCount full export list is 21 cols, so the preview must use a curated
    ≤20-col subset that still includes the amount/total column."""

    @pytest.mark.parametrize("software", ["autocount", "sql_account", "xero", "qbs_ledger"])
    @pytest.mark.parametrize("sheet", ["Purchase", "Sales"])
    def test_preview_spec_at_most_20_cols(self, software, sheet):
        spec = preview_column_spec(software=software, sheet=sheet)
        assert len(spec) <= 20, (
            f"{software}/{sheet} preview has {len(spec)} cols — Slack rejects >20"
        )

    def test_autocount_purchase_preview_keeps_amount_and_identity(self):
        spec = preview_column_spec(software="autocount", sheet="Purchase")
        headers = [c.header for c in spec]
        # AutoCount col #21 (Amount) is the most important preview column.
        assert "Amount" in headers
        assert "DocNo" in headers
        assert "DocDate" in headers
        assert "CreditorCode" in headers

    def test_autocount_sales_preview_keeps_amount_and_identity(self):
        spec = preview_column_spec(software="autocount", sheet="Sales")
        headers = [c.header for c in spec]
        assert "Amount" in headers
        assert "DocNo" in headers
        assert "DocDate" in headers
        assert "DebtorCode" in headers

    def test_autocount_preview_is_curated_subset_of_export_cols(self):
        # Preview row_keys must be a subset of the full export columns so the
        # exporter row dicts actually carry a value for every preview column.
        from ledgr_slack.export.exporters import load_erp_profile_for_system

        profile = load_erp_profile_for_system("autocount")
        for sheet, full_key in (("Purchase", "purchase_cols"), ("Sales", "sales_cols")):
            spec = preview_column_spec(software="autocount", sheet=sheet)
            full = set(profile[full_key])
            assert {c.row_key for c in spec} <= full

    def test_autocount_amount_is_numeric_cell(self):
        spec = preview_column_spec(software="autocount", sheet="Purchase")
        amount = next(c for c in spec if c.header == "Amount")
        assert amount.cell_type == "raw_number"


# AutoCount exporter row shape (full 21-col export dict, keyed by ERP column).
_AUTOCOUNT_PURCHASE_ROWS = [
    {
        "DocNo": "<<New>>", "DocDate": "01/06/2024", "CreditorCode": "400-A0001",
        "SupplierInvoiceNo": "INV-JBI-001", "JournalType": "PURCHASE",
        "DisplayTerm": "", "PurchaseAgent": "", "Description": "Auto parts",
        "CurrencyRate": "", "RefNo2": "", "Note": "", "InclusiveTax": "F",
        "AccNo": "510-000", "ToAccountRate": "", "DetailDescription": "Auto parts",
        "ProjNo": "", "DeptNo": "", "TaxType": "SV-6", "TaxableAmt": 1000.0,
        "TaxAdjustment": "", "Amount": 1000.0,
    },
]


class TestLedgerPreviewDataTableSlackLimit:
    """The assembled data_table must never carry >20 cells per row."""

    @pytest.fixture(autouse=True)
    def _force_native(self, monkeypatch):
        monkeypatch.setenv("LEDGR_NATIVE_BLOCKS", "1")

    def test_autocount_data_table_rows_at_most_20_cells(self):
        blocks = ledger_preview_data_table(
            rows=_AUTOCOUNT_PURCHASE_ROWS, workbook_name="Ledger_FY2024.xlsx",
            fy=2024, sheet="Purchase", software="autocount",
        )
        table = next(b for b in blocks if b["type"] == "data_table")
        for row in table["rows"]:
            assert len(row) <= 20, f"data_table row has {len(row)} cells (>20)"

    def test_autocount_data_table_header_includes_amount(self):
        blocks = ledger_preview_data_table(
            rows=_AUTOCOUNT_PURCHASE_ROWS, workbook_name="Ledger_FY2024.xlsx",
            fy=2024, sheet="Purchase", software="autocount",
        )
        table = next(b for b in blocks if b["type"] == "data_table")
        header_texts = [c["text"] for c in table["rows"][0]]
        assert "Amount" in header_texts
        assert "DocNo" in header_texts


class TestLedgerPreviewDataTableNative:

    @pytest.fixture(autouse=True)
    def _force_native(self, monkeypatch):
        monkeypatch.setenv("LEDGR_NATIVE_BLOCKS", "1")

    def test_empty_rows_returns_empty_list(self):
        assert ledger_preview_data_table(
            rows=[], workbook_name="Ledger_FY2025.xlsx", fy=2025,
            sheet="Purchase", software="qbs_ledger",
        ) == []

    def test_five_rows_produces_data_table_block(self):
        blocks = ledger_preview_data_table(
            rows=_QBS_PURCHASE_ROWS, workbook_name="Ledger_FY2025.xlsx", fy=2025,
            sheet="Purchase", software="qbs_ledger",
        )
        table_blocks = [b for b in blocks if b.get("type") == "data_table"]
        assert len(table_blocks) == 1

    def test_xero_purchase_header_order(self):
        blocks = ledger_preview_data_table(
            rows=_XERO_PURCHASE_ROWS, workbook_name="Purchase Ledger FY2025", fy=2025,
            sheet="Purchase", software="xero",
        )
        table = next(b for b in blocks if b["type"] == "data_table")
        header_texts = [c["text"] for c in table["rows"][0]]
        assert header_texts == [
            "Contact", "Invoice #", "Invoice Date", "Description",
            "Account", "Tax Type", "Unit Amount", "Total", "Currency",
        ]

    def test_xero_purchase_first_data_cell_is_contact_name(self):
        blocks = ledger_preview_data_table(
            rows=_XERO_PURCHASE_ROWS, workbook_name="Purchase Ledger FY2025", fy=2025,
            sheet="Purchase", software="xero",
        )
        table = next(b for b in blocks if b["type"] == "data_table")
        data_row = table["rows"][1]
        assert data_row[0]["type"] == "raw_text"
        assert data_row[0]["text"] == "Acme Trading Pte Ltd"

    def test_qbs_purchase_header_starts_with_invoice_date(self):
        blocks = ledger_preview_data_table(
            rows=_QBS_PURCHASE_ROWS, workbook_name="Ledger_FY2025.xlsx", fy=2025,
            sheet="Purchase", software="qbs_ledger",
        )
        table = next(b for b in blocks if b["type"] == "data_table")
        header = table["rows"][0]
        assert header[0]["text"] == "Invoice Date"
        assert header[2]["text"] == "Vendor"

    def test_bank_preview_has_6_col_shape(self):
        blocks = ledger_preview_data_table(
            rows=_BANK_ROWS, workbook_name="Bank — OCBC SGD", fy=2025,
            sheet="OCBC SGD", software="qbs_ledger",
        )
        table = next(b for b in blocks if b["type"] == "data_table")
        header_texts = [c["text"] for c in table["rows"][0]]
        assert header_texts == ["Date", "Description", "Withdrawal", "Deposit", "Balance", "Currency"]

    def test_bank_withdrawal_deposit_are_raw_number(self):
        blocks = ledger_preview_data_table(
            rows=_BANK_ROWS, workbook_name="Bank — OCBC SGD", fy=2025,
            sheet="OCBC SGD", software="qbs_ledger",
        )
        table = next(b for b in blocks if b["type"] == "data_table")
        data_row = table["rows"][1]
        # col 2 = Withdrawal, col 3 = Deposit
        assert data_row[2]["type"] == "raw_number"
        assert data_row[3]["type"] == "raw_number"
        assert data_row[3]["value"] == 5000.00

    def test_five_data_rows_follow_header(self):
        blocks = ledger_preview_data_table(
            rows=_QBS_PURCHASE_ROWS, workbook_name="Ledger_FY2025.xlsx", fy=2025,
            sheet="Purchase", software="qbs_ledger",
        )
        table = next(b for b in blocks if b["type"] == "data_table")
        assert len(table["rows"]) == 6  # 1 header + 5 data

    def test_text_cols_are_raw_text_number_cols_raw_number(self):
        blocks = ledger_preview_data_table(
            rows=_QBS_PURCHASE_ROWS[:1], workbook_name="Ledger_FY2025.xlsx", fy=2025,
            sheet="Purchase", software="qbs_ledger",
        )
        table = next(b for b in blocks if b["type"] == "data_table")
        data_row = table["rows"][1]
        # QBS Purchase: cols 0-4 are text, cols 5-7 are numeric
        for idx in range(5):
            assert data_row[idx]["type"] == "raw_text"
        for idx in range(5, 8):
            assert data_row[idx]["type"] == "raw_number"

    def test_numeric_cells_have_float_value(self):
        blocks = ledger_preview_data_table(
            rows=_QBS_PURCHASE_ROWS[:1], workbook_name="Ledger_FY2025.xlsx", fy=2025,
            sheet="Purchase", software="qbs_ledger",
        )
        table = next(b for b in blocks if b["type"] == "data_table")
        data_row = table["rows"][1]
        sub_total_cell = data_row[5]   # Sub Total
        total_amt_cell = data_row[7]   # Total Amount
        assert sub_total_cell["type"] == "raw_number"
        assert isinstance(sub_total_cell["value"], float)
        assert sub_total_cell["text"] == "1132.11"
        assert total_amt_cell["type"] == "raw_number"
        assert total_amt_cell["text"] == "1234.50"

    def test_caption_contains_sheet_and_fy(self):
        blocks = ledger_preview_data_table(
            rows=_QBS_PURCHASE_ROWS, workbook_name="Purchase Ledger FY2025", fy=2025,
            sheet="Purchase", software="qbs_ledger",
        )
        table = next(b for b in blocks if b["type"] == "data_table")
        assert "Purchase" in table["caption"]
        assert "FY2025" in table["caption"]

    def test_row_header_column_index_is_zero(self):
        blocks = ledger_preview_data_table(
            rows=_QBS_PURCHASE_ROWS, workbook_name="Ledger_FY2025.xlsx", fy=2025,
            sheet="Purchase", software="qbs_ledger",
        )
        table = next(b for b in blocks if b["type"] == "data_table")
        assert table["row_header_column_index"] == 0

    def test_page_size_is_ten(self):
        blocks = ledger_preview_data_table(
            rows=_QBS_PURCHASE_ROWS, workbook_name="Ledger_FY2025.xlsx", fy=2025,
            sheet="Purchase", software="qbs_ledger",
        )
        table = next(b for b in blocks if b["type"] == "data_table")
        assert table["page_size"] == 10

    def test_overflow_appends_context_block(self):
        rows_15 = _QBS_PURCHASE_ROWS * 3  # 15 rows
        blocks = ledger_preview_data_table(
            rows=rows_15, workbook_name="Ledger_FY2025.xlsx", fy=2025,
            sheet="Purchase", software="qbs_ledger", max_rows=10,
        )
        table = next(b for b in blocks if b["type"] == "data_table")
        assert len(table["rows"]) == 11  # 1 header + 10 data
        context_blocks = [b for b in blocks if b["type"] == "context"]
        assert len(context_blocks) == 1
        assert "5 more" in context_blocks[0]["elements"][0]["text"]

    def test_no_overflow_no_context_block(self):
        blocks = ledger_preview_data_table(
            rows=_QBS_PURCHASE_ROWS, workbook_name="Ledger_FY2025.xlsx", fy=2025,
            sheet="Purchase", software="qbs_ledger", max_rows=10,
        )
        assert not any(b["type"] == "context" for b in blocks)

    def test_missing_keys_yield_empty_raw_text_not_raw_number(self):
        # A row with no matching keys should produce empty raw_text for numeric cols
        # (raw_number without a value is rejected by Slack).
        sparse = [{}]
        blocks = ledger_preview_data_table(
            rows=sparse, workbook_name="Ledger_FY2025.xlsx", fy=2025,
            sheet="Purchase", software="qbs_ledger",
        )
        table = next(b for b in blocks if b["type"] == "data_table")
        data_row = table["rows"][1]
        for cell in data_row:
            # Every cell must have a "text" key (raw_number also carries "text").
            assert "text" in cell
        # Missing keys → em dash (Slack rejects zero-length raw_text).
        for idx in range(5, 8):
            assert data_row[idx]["type"] == "raw_text"
            assert data_row[idx]["text"] == "—"

    def test_caption_includes_software_and_fy(self):
        blocks = ledger_preview_data_table(
            rows=_QBS_PURCHASE_ROWS, workbook_name="Ledger_FY2025.xlsx", fy=2025,
            sheet="Purchase", software="qbs_ledger",
        )
        table = next(b for b in blocks if b["type"] == "data_table")
        assert "FY2025" in table["caption"]
        assert "QBS Ledger" in table["caption"]

    def test_xero_software_label_in_caption(self):
        blocks = ledger_preview_data_table(
            rows=_XERO_PURCHASE_ROWS, workbook_name="Ledger_FY2025.xlsx", fy=2025,
            sheet="Purchase", software="xero",
        )
        table = next(b for b in blocks if b["type"] == "data_table")
        assert "Xero" in table["caption"]

    def test_qbs_software_label_in_caption(self):
        blocks = ledger_preview_data_table(
            rows=_QBS_PURCHASE_ROWS, workbook_name="Ledger_FY2025.xlsx", fy=2025,
            sheet="Purchase", software="qbs_ledger",
        )
        table = next(b for b in blocks if b["type"] == "data_table")
        assert "QBS Ledger" in table["caption"]


# --------------------------------------------------------------------------- #
# AutoCount / SQL Account — preview_column_spec + software_label
# --------------------------------------------------------------------------- #

from app.blocks import software_label


class TestPreviewColumnSpecAutoCountSQL:

    def test_autocount_purchase_first_col_matches_profile(self):
        from ledgr_slack.export.exporters import load_erp_profile_for_system

        profile = load_erp_profile_for_system("autocount")
        spec = preview_column_spec(software="AutoCount", sheet="Purchase")
        assert spec[0].row_key == profile["purchase_cols"][0]

    def test_autocount_purchase_row_keys_match_curated_preview_cols(self):
        # WS-5.2 fix: the AutoCount preview uses the curated ≤20-col
        # `purchase_preview_cols` subset (the full 21-col `purchase_cols` export
        # list exceeds Slack's data_table limit), and every preview key is a
        # real export column so the exporter rows carry a value for it.
        from ledgr_slack.export.exporters import load_erp_profile_for_system

        profile = load_erp_profile_for_system("autocount")
        spec = preview_column_spec(software="AutoCount", sheet="Purchase")
        assert [c.row_key for c in spec] == profile["purchase_preview_cols"]
        assert {c.row_key for c in spec} <= set(profile["purchase_cols"])
        assert len(spec) <= 20

    def test_autocount_purchase_has_creditor_col(self):
        spec = preview_column_spec(software="AutoCount", sheet="Purchase")
        row_keys = [c.row_key for c in spec]
        assert "CreditorCode" in row_keys

    def test_autocount_sales_has_debtor_col(self):
        spec = preview_column_spec(software="AutoCount", sheet="Sales")
        row_keys = [c.row_key for c in spec]
        assert "DebtorCode" in row_keys

    def test_autocount_sales_curated_preview_drops_currency_code(self):
        # CurrencyCode is a non-key export column trimmed from the curated Slack
        # preview to fit the 20-col limit; it remains in the full .xlsx export.
        from ledgr_slack.export.exporters import load_erp_profile_for_system

        profile = load_erp_profile_for_system("autocount")
        spec = preview_column_spec(software="AutoCount", sheet="Sales")
        row_keys = [c.row_key for c in spec]
        assert "CurrencyCode" not in row_keys
        assert "CurrencyCode" in profile["sales_cols"]

    def test_autocount_sales_row_keys_match_curated_preview_cols(self):
        from ledgr_slack.export.exporters import load_erp_profile_for_system

        profile = load_erp_profile_for_system("autocount")
        spec = preview_column_spec(software="AutoCount", sheet="Sales")
        assert [c.row_key for c in spec] == profile["sales_preview_cols"]
        assert {c.row_key for c in spec} <= set(profile["sales_cols"])
        assert len(spec) <= 20

    def test_sql_account_purchase_row_keys_match_profile_cols(self):
        from ledgr_slack.export.exporters import load_erp_profile_for_system

        profile = load_erp_profile_for_system("sql_account")
        spec = preview_column_spec(software="SQL Account", sheet="Purchase")
        assert [c.row_key for c in spec] == profile["purchase_cols"]

    def test_sql_account_purchase_has_code10_col(self):
        spec = preview_column_spec(software="SQL Account", sheet="Purchase")
        row_keys = [c.row_key for c in spec]
        assert "CODE(10)" in row_keys

    def test_sql_account_sales_has_code10_col(self):
        spec = preview_column_spec(software="SQL Account", sheet="Sales")
        row_keys = [c.row_key for c in spec]
        assert "CODE(10)" in row_keys

    def test_sql_account_purchase_first_col_row_key_is_docno(self):
        spec = preview_column_spec(software="SQL Account", sheet="Purchase")
        assert spec[0].row_key == "DOCNO(20)"

    def test_profile_numeric_cols_use_raw_number(self):
        from ledgr_slack.export.exporters import load_erp_profile_for_system

        load_erp_profile_for_system("sql_account")  # smoke: profile loads from skill
        spec = preview_column_spec(software="SQL Account", sheet="Purchase")
        by_key = {c.row_key: c.cell_type for c in spec}
        assert by_key["_TAXAMT"] == "raw_number"
        assert by_key["_AMOUNT"] == "raw_number"
        assert by_key["CODE(10)"] == "raw_text"

    def test_autocount_and_sql_bank_sheet_returns_bank_cols(self):
        # Bank sheets always return the 6-col bank spec, regardless of software.
        spec_ac = preview_column_spec(software="AutoCount", sheet="OCBC SGD")
        spec_sql = preview_column_spec(software="SQL Account", sheet="DBS MYR")
        assert len(spec_ac) == 6
        assert len(spec_sql) == 6


class TestSoftwareLabel:

    def test_autocount_label(self):
        assert software_label("AutoCount") == "AutoCount"

    def test_sql_account_label(self):
        assert software_label("SQL Account") == "SQL Account"

    def test_xero_label(self):
        assert software_label("Xero") == "Xero"

    def test_qbs_ledger_label(self):
        assert software_label("QBS Ledger") == "QBS Ledger"

    def test_empty_returns_unknown_erp(self):
        assert software_label("") == "Unknown ERP"


# --------------------------------------------------------------------------- #
# software_label empty_label= (delivery summary path)
# --------------------------------------------------------------------------- #

from ledgr_slack.export.exporters import software_label as canonical_software_label


class TestSoftwareLabelForSummary:

    def test_autocount(self):
        assert canonical_software_label("AutoCount", empty_label="") == "AutoCount"

    def test_sql_account(self):
        assert canonical_software_label("SQL Account", empty_label="") == "SQL Account"

    def test_xero(self):
        assert canonical_software_label("Xero", empty_label="") == "Xero"

    def test_qbs_ledger(self):
        assert canonical_software_label("QBS Ledger", empty_label="") == "QBS Ledger"

    def test_empty_string_returns_empty(self):
        assert canonical_software_label("", empty_label="") == ""

    def test_none_equivalent_returns_empty(self):
        assert canonical_software_label(None, empty_label="") == ""  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Ledger preview data table fallback
# --------------------------------------------------------------------------- #


class TestLedgerPreviewDataTableFallback:

    @pytest.fixture(autouse=True)
    def _force_fallback(self, monkeypatch):
        monkeypatch.setenv("LEDGR_NATIVE_BLOCKS", "0")

    def test_empty_rows_returns_empty_list(self):
        assert ledger_preview_data_table(
            rows=[], workbook_name="Ledger_FY2025.xlsx", fy=2025,
            sheet="Purchase", software="qbs_ledger",
        ) == []

    def test_returns_section_with_mrkdwn_preblock(self):
        blocks = ledger_preview_data_table(
            rows=_QBS_PURCHASE_ROWS, workbook_name="Ledger_FY2025.xlsx", fy=2025,
            sheet="Purchase", software="qbs_ledger",
        )
        assert len(blocks) == 1
        assert blocks[0]["type"] == "section"
        assert blocks[0]["text"]["type"] == "mrkdwn"
        assert "```" in blocks[0]["text"]["text"]

    def test_fallback_text_under_3000_chars(self):
        rows_many = _QBS_PURCHASE_ROWS * 20  # 100 rows
        blocks = ledger_preview_data_table(
            rows=rows_many, workbook_name="Ledger_FY2025.xlsx", fy=2025,
            sheet="Purchase", software="qbs_ledger", max_rows=100,
        )
        assert len(blocks[0]["text"]["text"]) <= 3000

    def test_fallback_shows_invoice_date_and_invoice_number(self):
        blocks = ledger_preview_data_table(
            rows=_QBS_PURCHASE_ROWS[:1], workbook_name="Ledger_FY2025.xlsx", fy=2025,
            sheet="Purchase", software="qbs_ledger",
        )
        text = blocks[0]["text"]["text"]
        assert "15/09/2025" in text
        assert "INV-001" in text

    def test_xero_fallback_shows_contact_name_and_invoice_number(self):
        blocks = ledger_preview_data_table(
            rows=_XERO_PURCHASE_ROWS[:1], workbook_name="Ledger_FY2025.xlsx", fy=2025,
            sheet="Purchase", software="xero",
        )
        text = blocks[0]["text"]["text"]
        # Fallback truncates the date col to 12 chars — "Acme Trading" appears.
        assert "Acme Trading" in text
        assert "INV-2025-0042" in text


# --------------------------------------------------------------------------- #
# Commit 5: feedback_buttons_block + result_card integration
# --------------------------------------------------------------------------- #


class TestSummaryTableBlocks:

    def test_renders_category_details_rows(self, monkeypatch):
        monkeypatch.setenv("LEDGR_NATIVE_BLOCKS", "0")
        from app.blocks import summary_table_blocks

        rows = [
            {"category": "Vendor Name", "details": "Sample Vendor Pte Ltd"},
            {"category": "Invoice Number", "details": "2026/0210"},
        ]
        blocks = summary_table_blocks(rows)
        assert blocks[0]["type"] == "section"
        text = blocks[0]["text"]["text"]
        assert "Sample Vendor Pte Ltd" in text
        assert "2026/0210" in text

    def test_empty_returns_empty_list(self):
        from app.blocks import summary_table_blocks

        assert summary_table_blocks([]) == []


def test_processing_plan_headline_single_vs_multi():
    assert processing_plan_headline(total=1) == "Processing document"
    assert processing_plan_headline(total=2) == "Processing batch (2 documents)"
    assert processing_plan_headline(total=1, title="Custom") == "Custom"


# --------------------------------------------------------------------------- #
# Regression: review_card_blocks never emits an empty body.text
# --------------------------------------------------------------------------- #


