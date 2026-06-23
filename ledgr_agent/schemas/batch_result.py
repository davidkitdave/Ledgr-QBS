from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ledgr_agent.schemas.credit import CreditSummary
from ledgr_agent.schemas.review import ReviewRequest, SoftWarning


BatchStatus = Literal[
    "success",
    "partial",
    "needs_review",
    "blocked",
    "error",
]


class BatchResult(BaseModel):
    """Structured result shared by Slack, eval, and logs."""

    status: BatchStatus
    client_id: str
    firm_id: str | None = None
    source_files: list[str] = Field(default_factory=list)
    per_file: list[dict[str, object]] = Field(default_factory=list)
    posted_documents: list[dict[str, object]] = Field(default_factory=list)
    skipped_documents: list[dict[str, object]] = Field(default_factory=list)
    review_requests: list[ReviewRequest] = Field(default_factory=list)
    soft_warnings: list[SoftWarning] = Field(default_factory=list)
    erp_exports: list[dict[str, object]] = Field(default_factory=list)
    credits: CreditSummary = Field(default_factory=CreditSummary)
    models_used: list[str] = Field(default_factory=list)
    validation_summary: dict[str, object] = Field(default_factory=dict)
    audit_refs: list[str] = Field(default_factory=list)
    llm_call_count: int = 0
    strong_model_used: bool = False
    fallback_reason: str | None = None
    elapsed_ms: int | None = None
    documents_requested: int = 0
    documents_processed: int = 0
    documents_skipped_before_llm: int = 0
    estimated_cost: float | None = None
