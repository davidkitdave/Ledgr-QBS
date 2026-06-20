"""Tests for the COA detection + confirmation UX (ADR-0006)."""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from app.coa_detect import SpreadsheetKind, classify_spreadsheet, preview_coa
from app.coa_ingest import preview_coa_from_file
from app.blocks import coa_confirm_blocks, coa_unknown_disambiguation_blocks


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _write_xlsx(path: str, *, sheet_name: str, headers: list[str], rows: list) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append(headers)
    for r in rows:
        ws.append(list(r))
    wb.save(path)


@pytest.fixture()
def coa_xlsx(tmp_path: Path) -> str:
    path = str(tmp_path / "client_setup.xlsx")
    _write_xlsx(
        path,
        sheet_name="COA",
        headers=["Account Code", "Description", "Account Type", "Financial Statement"],
        rows=[
            ("4000", "Sales", "Revenue", "Profit and Loss"),
            ("6100", "Office Supplies", "Expense", "Profit and Loss"),
            ("6200", "Rent", "Expense", "Profit and Loss"),
        ],
    )
    return path


@pytest.fixture()
def qbs_client_setup_xlsx(tmp_path: Path) -> str:
    """A QBS-style Client Setup workbook with the canonical ``COA`` sheet."""
    path = str(tmp_path / "Sample Test Group - Client Setup.xlsx")
    _write_xlsx(
        path,
        sheet_name="COA",
        headers=[
            "Account code", "Description", "Account type",
            "Financial Statement", "Nature", "AI Search Keywords",
        ],
        rows=[
            ("4-1000", "Sales", "Income", "Profit and Loss", "Credit", "sales"),
            ("6-1000", "Office Supplies", "Expense", "Profit and Loss", "Debit", "office"),
        ],
    )
    return path


@pytest.fixture()
def ledger_xlsx(tmp_path: Path) -> str:
    """A spreadsheet that looks like a ledger export, not a COA."""
    path = str(tmp_path / "Ledger_FY2025.xlsx")
    _write_xlsx(
        path,
        sheet_name="Ledger",
        headers=["Contact", "Invoice #", "Invoice Date", "Unit Amount", "Total"],
        rows=[
            ("Company-A", "INV-1", "2025-03-15", 100, 110),
            ("Sample Vendor Inc", "INV-2", "2025-03-16", 50, 55),
        ],
    )
    return path


@pytest.fixture()
def unknown_xlsx(tmp_path: Path) -> str:
    path = str(tmp_path / "mystery.xlsx")
    _write_xlsx(
        path,
        sheet_name="Notes",
        headers=["Date", "Note"],
        rows=[("2025-01-01", "todo")],
    )
    return path


@pytest.fixture()
def mixed_format_coa_xlsx(tmp_path: Path) -> str:
    """Real QBS Client Setup may mix numeric and alphanumeric codes."""
    path = str(tmp_path / "client_setup_mixed.xlsx")
    _write_xlsx(
        path,
        sheet_name="COA",
        headers=["Account code", "Description", "Account type", "Financial Statement"],
        rows=[
            ("6100", "Sales", "Revenue", "Profit and Loss"),
            ("EXP01", "Rent", "Expense", "Profit and Loss"),
        ],
    )
    return path


@pytest.fixture()
def invalid_coa_xlsx(tmp_path: Path) -> str:
    """Missing account_type — must surface as a validation error."""
    path = str(tmp_path / "bad.xlsx")
    _write_xlsx(
        path,
        sheet_name="COA",
        headers=["Account code", "Description", "Account type", "Financial Statement"],
        rows=[("6100", "Sales", "", "Profit and Loss")],
    )
    return path


# --------------------------------------------------------------------------- #
# classify_spreadsheet
# --------------------------------------------------------------------------- #


class TestClassifySpreadsheet:
    def test_coa_sheet_name_is_coa_candidate(self, qbs_client_setup_xlsx):
        assert classify_spreadsheet(qbs_client_setup_xlsx) is SpreadsheetKind.COA_CANDIDATE

    def test_coa_headers_with_sheet_fallback(self, coa_xlsx):
        # Sheet name is "COA" — strongest signal.
        assert classify_spreadsheet(coa_xlsx) is SpreadsheetKind.COA_CANDIDATE

    def test_ledger_shape_is_ledger_candidate(self, ledger_xlsx):
        assert classify_spreadsheet(ledger_xlsx) is SpreadsheetKind.LEDGER_CANDIDATE

    def test_unknown_sheet(self, unknown_xlsx):
        assert classify_spreadsheet(unknown_xlsx) is SpreadsheetKind.UNKNOWN

    def test_missing_file_is_unknown(self, tmp_path):
        assert classify_spreadsheet(str(tmp_path / "nope.xlsx")) is SpreadsheetKind.UNKNOWN

    def test_unsupported_extension_is_unknown(self, tmp_path):
        path = tmp_path / "data.txt"
        path.write_text("hello", encoding="utf-8")
        assert classify_spreadsheet(str(path)) is SpreadsheetKind.UNKNOWN


# --------------------------------------------------------------------------- #
# preview_coa_from_file
# --------------------------------------------------------------------------- #


class TestPreviewCoa:
    def test_valid_qbs_setup(self, qbs_client_setup_xlsx):
        preview = preview_coa_from_file(qbs_client_setup_xlsx)
        assert preview is not None
        assert preview["n_accounts"] == 2
        assert preview["n_income"] == 1
        assert preview["n_expense"] == 1
        assert preview["errors"] == []
        # Two sample rows surfaced for the card.
        assert len(preview["sample"]) == 2

    def test_mixed_formats_yields_warning_not_error(self, mixed_format_coa_xlsx):
        preview = preview_coa_from_file(mixed_format_coa_xlsx)
        assert preview is not None
        assert preview["errors"] == []
        assert any("mixed format" in w.lower() for w in preview["warnings"])

    def test_invalid_returns_errors(self, invalid_coa_xlsx):
        preview = preview_coa_from_file(invalid_coa_xlsx)
        assert preview is not None
        assert preview["errors"]
        # The card should disable confirm when errors are present.

    def test_preview_coa_dataclass(self, coa_xlsx):
        # Direct dataclass path (used by the live runner helper too).
        prev = preview_coa(coa_xlsx)
        assert prev is not None
        assert prev.n_accounts == 3
        assert prev.n_income == 1
        assert prev.n_expense == 2


# --------------------------------------------------------------------------- #
# Block Kit confirm / disambiguation cards
# --------------------------------------------------------------------------- #


class TestCoaConfirmBlocks:
    def test_confirm_card_uses_as_coa_for_pending(self, coa_xlsx):
        preview = preview_coa_from_file(coa_xlsx)
        blocks = coa_confirm_blocks(
            preview=preview,
            file_id="F-123",
            channel_state="pending_coa",
            filename="Client Setup.xlsx",
        )
        actions = [b for b in blocks if b.get("type") == "actions"][0]
        action_ids = [e.get("action_id") for e in actions["elements"]]
        assert "ledgr_coa_confirm" in action_ids
        # The primary confirm button copy differs by channel state.
        confirm = next(
            e for e in actions["elements"] if e.get("action_id") == "ledgr_coa_confirm"
        )
        assert confirm["text"]["text"] == "Use as COA"
        assert confirm.get("style") == "primary"

    def test_confirm_card_uses_replace_for_active(self, coa_xlsx):
        preview = preview_coa_from_file(coa_xlsx)
        blocks = coa_confirm_blocks(
            preview=preview,
            file_id="F-123",
            channel_state="active",
            filename="Client Setup.xlsx",
        )
        actions = [b for b in blocks if b.get("type") == "actions"][0]
        confirm = next(
            e for e in actions["elements"] if e.get("action_id") == "ledgr_coa_confirm"
        )
        assert confirm["text"]["text"] == "Replace COA"
        assert confirm.get("style") == "danger"

    def test_confirm_button_omitted_when_validation_errors(self, invalid_coa_xlsx):
        preview = preview_coa_from_file(invalid_coa_xlsx)
        blocks = coa_confirm_blocks(
            preview=preview,
            file_id="F-123",
            channel_state="pending_coa",
            filename="bad.xlsx",
        )
        actions = [b for b in blocks if b.get("type") == "actions"][0]
        action_ids = [e.get("action_id") for e in actions["elements"]]
        # Use as COA / Replace COA must NOT be present when there are hard errors.
        assert "ledgr_coa_confirm" not in action_ids
        assert "ledgr_coa_as_document" in action_ids
        assert "ledgr_coa_cancel" in action_ids

    def test_warnings_section_present_for_mixed_formats(self, mixed_format_coa_xlsx):
        preview = preview_coa_from_file(mixed_format_coa_xlsx)
        blocks = coa_confirm_blocks(
            preview=preview,
            file_id="F-123",
            channel_state="pending_coa",
            filename="mixed.xlsx",
        )
        # The card carries a Warnings section mentioning mixed formats.
        assert any(
            "mixed format" in (b.get("text", {}).get("text", "")).lower()
            for b in blocks
            if b.get("type") == "section"
        )

    def test_button_value_carries_file_id(self, coa_xlsx):
        preview = preview_coa_from_file(coa_xlsx)
        blocks = coa_confirm_blocks(
            preview=preview,
            file_id="F-999",
            channel_state="pending_coa",
        )
        actions = [b for b in blocks if b.get("type") == "actions"][0]
        for e in actions["elements"]:
            assert e.get("value") == "F-999"

    def test_disambiguation_card_always_offers_confirm(self):
        blocks = coa_unknown_disambiguation_blocks(
            file_id="F-321", filename="mystery.xlsx"
        )
        actions = [b for b in blocks if b.get("type") == "actions"][0]
        action_ids = [e.get("action_id") for e in actions["elements"]]
        assert action_ids == [
            "ledgr_coa_confirm",
            "ledgr_coa_as_document",
            "ledgr_coa_cancel",
        ]
