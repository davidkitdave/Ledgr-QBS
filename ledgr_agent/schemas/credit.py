from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


CreditStatus = Literal["not_checked", "estimated", "charged", "blocked", "not_billable"]


class CreditSummary(BaseModel):
    """Billing summary attached to every batch result."""

    credits_estimated: int = 0
    credits_used: int = 0
    credits_remaining: int | None = None
    credit_status: CreditStatus = "not_checked"
    credit_ledger_refs: list[str] = Field(default_factory=list)
