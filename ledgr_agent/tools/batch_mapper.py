from __future__ import annotations

from pathlib import Path

from invoice_processing.pipeline import BatchResult as EngineBatchResult
from invoice_processing.pipeline import ProcessedDoc
from ledgr_agent.schemas.batch_result import BatchResult, BatchStatus
from ledgr_agent.schemas.credit import CreditSummary
from ledgr_agent.schemas.review import ReviewRequest


def per_file_summary(doc: ProcessedDoc) -> dict[str, object]:
    file_name = Path(doc.path).name
    return {
        "path": doc.path,
        "file_name": file_name,
        "doc_type": doc.doc_type,
        "direction": doc.direction,
        "reconciled": doc.reconciled,
        "workbook": doc.route.workbook,
        "sheet": doc.route.sheet,
        "note": doc.note,
    }


def review_requests_for_doc(doc: ProcessedDoc) -> list[ReviewRequest]:
    file_name = Path(doc.path).name
    if doc.note.startswith("ERROR"):
        return [
            ReviewRequest(
                id="processing_error",
                severity="hard_review",
                message=doc.note,
                file_name=file_name,
            )
        ]
    if not doc.reconciled and "needs review" in doc.note.lower():
        return [
            ReviewRequest(
                id="document_needs_review",
                severity="hard_review",
                message=doc.note,
                file_name=file_name,
            )
        ]
    return []


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
) -> BatchResult:
    """Convert the engine harness result into the shared ``BatchResult`` contract."""

    review_requests: list[ReviewRequest] = []
    per_file: list[dict[str, object]] = []
    posted_documents: list[dict[str, object]] = []
    skipped_documents: list[dict[str, object]] = []

    for doc in engine_result.docs:
        per_file.append(per_file_summary(doc))
        review_requests.extend(review_requests_for_doc(doc))
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
        erp_exports=erp_export_summaries(engine_result.workbooks),
        credits=CreditSummary(credit_status="not_checked"),
        models_used=list(models_used or []),
        validation_summary=validation_summary,
        llm_call_count=llm_call_count,
        strong_model_used=strong_model_used,
        documents_requested=len(source_files),
        documents_processed=len(posted_documents),
        documents_skipped_before_llm=documents_skipped_before_llm,
    )
