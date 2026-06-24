from __future__ import annotations

from pathlib import Path

from invoice_processing.pipeline import BatchResult as EngineBatchResult
from invoice_processing.pipeline import ProcessedDoc
from ledgr_agent.schemas.batch_result import BatchResult, BatchStatus
from ledgr_agent.schemas.credit import CreditSummary
from ledgr_agent.schemas.review import ReviewRequest, SoftWarning
from ledgr_agent.review.grouping import partition_and_group_reasons


def per_file_summary(doc: ProcessedDoc) -> dict[str, object]:
    file_name = Path(doc.path).name
    workbook = getattr(doc.route, "workbook", None) if doc.route else None
    sheet = getattr(doc.route, "sheet", None) if doc.route else None
    return {
        "path": doc.path,
        "file_name": file_name,
        "doc_type": doc.doc_type,
        "direction": doc.direction,
        "reconciled": doc.reconciled,
        "workbook": workbook,
        "sheet": sheet,
        "note": doc.note,
    }


def review_from_note(
    note: str | None,
    file_name: str,
) -> tuple[list[ReviewRequest], list[SoftWarning]]:
    """Parse legacy notes and group them into hard review requests and soft warnings."""
    if not note:
        return [], []

    # Case-insensitive prefix check and strip
    if note.lower().startswith("needs review:"):
        note = note[len("needs review:"):]

    note = note.strip()
    if not note:
        return [], []

    reasons = [r.strip() for r in note.split(";") if r.strip()]
    return partition_and_group_reasons(reasons, file_name=file_name)


def posted_document_summary(doc: ProcessedDoc) -> dict[str, object]:
    summary = per_file_summary(doc)
    if doc.normalized is not None:
        summary["invoice_number"] = doc.normalized.invoice_number
        summary["invoice_date"] = (
            doc.normalized.invoice_date.isoformat()
            if doc.normalized.invoice_date is not None
            else None
        )
        summary["total"] = doc.normalized.doc_total
    return summary


def erp_export_summaries(workbooks: dict[str, bytes]) -> list[dict[str, object]]:
    return [
        {
            "file_name": file_name,
            "byte_size": len(data),
            "format": "xlsx",
        }
        for file_name, data in sorted(workbooks.items())
    ]


def determine_batch_status(
    *,
    blocked_reason: str | None,
    processed_docs: list[ProcessedDoc],
    missing_files: list[str],
) -> BatchStatus:
    if blocked_reason:
        return "blocked"
    if not processed_docs and missing_files:
        return "error"
    error_count = sum(1 for doc in processed_docs if doc.note.startswith("ERROR"))
    review_count = sum(
        1
        for doc in processed_docs
        if not doc.reconciled and not doc.note.startswith("ERROR")
    )
    if processed_docs and error_count == len(processed_docs):
        return "error"
    if review_count and error_count:
        return "partial"
    if review_count:
        return "needs_review"
    if error_count or missing_files:
        return "partial"
    return "success"


def map_engine_batch_to_contract(
    engine_result: EngineBatchResult,
    *,
    client: object,
    source_files: list[str],
    missing_files: list[str],
    blocked_reason: str | None = None,
    llm_call_count: int = 0,
    models_used: list[str] | None = None,
    strong_model_used: bool = False,
    documents_skipped_before_llm: int = 0,
    elapsed_ms: int | None = None,
    tax_policy_version: str | None = None,
) -> BatchResult:
    """Convert the engine harness result into the shared ``BatchResult`` contract."""

    review_requests: list[ReviewRequest] = []
    soft_warnings: list[SoftWarning] = []
    per_file: list[dict[str, object]] = []
    posted_documents: list[dict[str, object]] = []
    skipped_documents: list[dict[str, object]] = []

    for doc in engine_result.docs:
        per_file.append(per_file_summary(doc))
        file_name = Path(doc.path).name
        hard_reqs, soft_warns = review_from_note(doc.note, file_name)
        review_requests.extend(hard_reqs)
        soft_warnings.extend(soft_warns)

        if doc.note.startswith("ERROR"):
            skipped_documents.append(per_file_summary(doc))
        elif doc.reconciled:
            posted_documents.append(posted_document_summary(doc))

    for missing in missing_files:
        skipped_documents.append(
            {
                "path": missing,
                "file_name": Path(missing).name,
                "note": "ERROR: file not found",
            }
        )

    status = determine_batch_status(
        blocked_reason=blocked_reason,
        processed_docs=engine_result.docs,
        missing_files=missing_files,
    )

    validation_summary: dict[str, object] = {}
    if blocked_reason:
        validation_summary["block_reason"] = blocked_reason
    if missing_files:
        validation_summary["missing_files"] = list(missing_files)
    if engine_result.errors:
        validation_summary["engine_errors"] = list(engine_result.errors)
    if tax_policy_version is not None:
        validation_summary["tax_policy_version"] = tax_policy_version

    client_id = getattr(client, "client_id", None) or "unknown"
    firm_id = getattr(client, "firm_id", None)

    return BatchResult(
        status=status,
        client_id=str(client_id),
        firm_id=firm_id,
        source_files=list(source_files),
        per_file=per_file,
        posted_documents=posted_documents,
        skipped_documents=skipped_documents,
        review_requests=review_requests,
        soft_warnings=soft_warnings,
        erp_exports=erp_export_summaries(engine_result.workbooks),
        credits=CreditSummary(credit_status="not_checked"),
        models_used=list(models_used or []),
        validation_summary=validation_summary,
        llm_call_count=llm_call_count,
        strong_model_used=strong_model_used,
        elapsed_ms=elapsed_ms,
        documents_requested=len(source_files),
        documents_processed=len(posted_documents),
        documents_skipped_before_llm=documents_skipped_before_llm,
    )
