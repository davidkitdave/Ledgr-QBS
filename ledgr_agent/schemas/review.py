from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


ReviewSeverity = Literal["hard_review", "review"]


class ReviewRequest(BaseModel):
    """A material issue that should pause or block delivery."""

    id: str
    severity: ReviewSeverity
    message: str
    file_name: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)


class SoftWarning(BaseModel):
    """A grouped warning that should be visible but not spam HITL."""

    id: str
    message: str
    count: int = 1
    file_name: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)
