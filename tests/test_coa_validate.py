"""Tests for COA upload validation (ADR-0006)."""

from __future__ import annotations

from app.coa_validate import validate_coa


def _row(**kwargs) -> dict:
    base = {
        "code": "",
        "description": "Office Supplies",
        "account_type": "Expense",
        "financial_statement": "Profit and Loss",
        "nature": "",
        "keywords": "",
    }
    base.update(kwargs)
    return base


class TestValidateCoaRequiredFields:
    def test_valid_minimal_qbs_style_passes(self):
        rows = [
            _row(description="Sales", account_type="Revenue", financial_statement="Profit and Loss"),
            _row(description="Rent", account_type="Expense", financial_statement="Profit and Loss"),
        ]
        result = validate_coa(rows)
        assert result.ok
        assert not result.errors

    def test_missing_description_fails(self):
        rows = [
            _row(description="", account_type="Expense"),
            _row(description="Sales", account_type="Revenue"),
        ]
        result = validate_coa(rows)
        assert not result.ok
        assert any("description" in e.lower() for e in result.errors)

    def test_missing_account_type_fails(self):
        rows = [
            _row(description="Rent", account_type=""),
            _row(description="Sales", account_type="Revenue"),
        ]
        result = validate_coa(rows)
        assert not result.ok
        assert any("account_type" in e.lower() or "type" in e.lower() for e in result.errors)


class TestValidateCoaCodes:
    def test_blank_codes_allowed(self):
        rows = [
            _row(code="", description="Sales", account_type="Revenue"),
            _row(code="", description="Rent", account_type="Expense"),
        ]
        assert validate_coa(rows).ok

    def test_duplicate_codes_fail(self):
        rows = [
            _row(code="6100", description="Sales", account_type="Revenue"),
            _row(code="6100", description="Rent", account_type="Expense"),
        ]
        result = validate_coa(rows)
        assert not result.ok
        assert any("duplicate" in e.lower() for e in result.errors)

    def test_code_too_long_fails(self):
        rows = [
            _row(code="12345678901", description="Sales", account_type="Revenue"),
            _row(code="", description="Rent", account_type="Expense"),
        ]
        result = validate_coa(rows)
        assert not result.ok
        assert any("10" in e for e in result.errors)

    def test_hyphenated_codes_allowed(self):
        rows = [
            _row(code="4-1000", description="Sales", account_type="Income"),
            _row(code="6-1000", description="Rent", account_type="Expense"),
        ]
        assert validate_coa(rows).ok


class TestValidateCoaCoverage:
    def test_expense_only_fails(self):
        rows = [
            _row(description="Rent", account_type="Expense"),
            _row(description="Utilities", account_type="Expense"),
        ]
        result = validate_coa(rows)
        assert not result.ok
        assert any("income" in e.lower() or "revenue" in e.lower() for e in result.errors)

    def test_revenue_only_fails(self):
        rows = [
            _row(description="Sales", account_type="Revenue"),
            _row(description="Services", account_type="Income"),
        ]
        result = validate_coa(rows)
        assert not result.ok
        assert any("expense" in e.lower() for e in result.errors)


class TestValidateCoaEmpty:
    def test_empty_list_fails(self):
        result = validate_coa([])
        assert not result.ok
