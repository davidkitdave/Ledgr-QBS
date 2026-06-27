"""Canonical bank-statement JSON for the light Ledgr path."""

from __future__ import annotations

from pydantic import BaseModel, Field


class BankTxn(BaseModel):
    date: str | None = Field(
        default=None,
        description="ISO date YYYY-MM-DD when visible on the statement row.",
    )
    description: str = Field(default="", description="Transaction narrative as printed.")
    bank_ref: str | None = Field(default=None, description="Cheque or bank reference if shown.")
    withdrawal: float | None = Field(default=None, description="Debit / paid out (positive).")
    deposit: float | None = Field(default=None, description="Credit / received (positive).")
    balance: float | None = Field(default=None, description="Running balance after this row.")


class BankAccount(BaseModel):
    bank_name: str = Field(
        default="",
        description=(
            "Bank label only (e.g. 'OCBC' or 'DBS Bank Ltd'). "
            "Do NOT embed account digits in this field."
        ),
    )
    account_number: str | None = Field(default=None, description="Account number as printed.")
    currency: str | None = Field(default=None, description="ISO currency code (default SGD).")
    statement_period: str | None = Field(
        default=None,
        description="Printed period, e.g. '01 DEC 2024 - 31 DEC 2024'.",
    )
    opening_balance: float | None = Field(
        default=None,
        description="Brought-forward / opening balance (not a transaction row).",
    )
    closing_balance: float | None = Field(default=None, description="Final balance on the statement.")
    transactions: list[BankTxn] = Field(default_factory=list)


class ReadBankStatement(BaseModel):
    accounts: list[BankAccount] = Field(
        default_factory=list,
        description="One entry per distinct (account_number, currency).",
    )
