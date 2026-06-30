"""Batch/HITL schemas for legacy Slack path (moved out of ledgr_agent)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ledgr_agent.internal.schemas import CreditSummary

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
    export_rows: list[dict[str, object]] = Field(default_factory=list)
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
