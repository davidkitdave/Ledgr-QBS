from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import os
import time
from typing import Any

from ledgr_agent.models.client_context import (
    ClientContext,
    client_context_from_state,
    playground_default_context,
)
from ledgr_agent.pipeline.light_batch import process_batch_light_async
from ledgr_agent.schemas.credit import CreditSummary
from ledgr_agent.shared.mime import mime_for
from ledgr_agent.shared.pdf_pages import count_input_pages
from ledgr_agent.shared.playground_seed import seed_playground_profile_if_needed
from ledgr_agent.tools.document_truth import document_truth_report
from ledgr_agent.tools.playground_uploads import resolve_document_paths

# ---------------------------------------------------------------------------
# Credit service singleton — process-persistent in dev/eval
# ---------------------------------------------------------------------------
_credit_service_factory: Callable[[], Any] | None = None
_credit_service_singleton: Any = None


def _get_credit_service() -> Any:
    global _credit_service_singleton

    if _credit_service_factory is not None:
        return _credit_service_factory()

    if _credit_service_singleton is not None:
        return _credit_service_singleton

    try:
        from app.credit_service import CreditService, InMemoryCreditStore
    except ImportError:
        return None

    _credit_service_singleton = CreditService(InMemoryCreditStore())
    return _credit_service_singleton


def _credit_gate(
    *,
    firm_id: str | None,
    paths: list[str],
    required_units: int | None = None,
) -> dict[str, Any]:
    if not firm_id:
        return {"allowed": True, "reason": "ok", "balance": 0}

    service = _get_credit_service()
    if service is None:
        return {"allowed": True, "reason": "ok", "balance": 0}

    required = max(required_units if required_units is not None else len(paths), 0)
    balance = service.read_balance(firm_id)
    allowed = balance >= required
    if allowed:
        reason = "ok"
    elif balance <= 0:
        reason = "zero_credit"
    else:
        reason = "insufficient_credit"
    return {
        "allowed": allowed,
        "reason": reason,
        "balance": balance,
        "required_units": required,
    }


def _estimate_gate_units(paths: list[Any]) -> int:
    total = 0
    for raw in paths:
        path = raw if hasattr(raw, "read_bytes") else Path(str(raw))
        try:
            total += count_input_pages(path.read_bytes(), mime_for(path))
        except Exception:
            total += 1
    return total


def _charge_credits_in_tool() -> bool:
    raw = os.environ.get("LEDGR_CHARGE_CREDITS_IN_TOOL", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _light_batch_enabled() -> bool:
    raw = os.environ.get("LEDGR_LIGHT_BATCH", "1")
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _merge_validation(payload: dict[str, Any], extra: dict[str, object]) -> None:
    if extra:
        payload["validation_summary"] = {
            **payload.get("validation_summary", {}),
            **extra,
        }


def _derive_fallback_reason(payload: dict[str, Any]) -> str | None:
    summary = payload.get("validation_summary") or {}
    block_reason = summary.get("block_reason")
    if block_reason:
        return str(block_reason)
    engine_errors = summary.get("engine_errors") or []
    if engine_errors:
        return str(engine_errors[0])
    for skipped in payload.get("skipped_documents") or []:
        note = str(skipped.get("note") or "")
        if note.startswith("ERROR"):
            return note
    return None


def _finalize_batch_payload(payload: dict[str, Any]) -> dict[str, Any]:
    reason = _derive_fallback_reason(payload)
    if reason:
        payload["fallback_reason"] = reason
    return payload


def _empty_blocked_batch(
    *,
    client: ClientContext,
    source_files: list[str],
    missing_files: list[str],
    blocked_reason: str,
    gate_units: int,
    credit_remaining: int | None,
    documents_skipped_before_llm: int = 0,
    path_resolution: dict[str, object] | None = None,
) -> dict[str, Any]:
    from ledgr_agent.pipeline.batch_result_builder import build_batch_result

    batch = build_batch_result(
        [],
        client=client,
        source_files=source_files,
        missing_files=missing_files,
        blocked_reason=blocked_reason,
        documents_skipped_before_llm=documents_skipped_before_llm,
        credits=CreditSummary(
            credits_estimated=gate_units,
            credits_used=0,
            credits_remaining=credit_remaining,
            credit_status="blocked",
        ),
    )
    payload = batch.model_dump()
    _merge_validation(
        payload,
        {
            **(path_resolution or {}),
            "credit_estimate": {
                "gate_units": gate_units,
                "gate_reason": blocked_reason,
                "charged_in_playground": False,
            },
        },
    )
    return _finalize_batch_payload(payload)


def process_document_batch(tool_context: Any, paths: list[str], **inject: Any) -> dict[str, Any]:
    """Process a batch of documents using the light read + merge path.

    One Gemini call per file (whole PDF). Bills/SOA/multi-receipt return a list
    of documents; bank statements return account lists. Files are read in parallel
    (fan-out across files only — no page chunking).

    Args:
        paths: Absolute file paths on disk. In ADK playground, pass ``[]`` when
            the user attached files; uploads are recovered from session state.
        tool_context: ADK session context.
        **inject: Reserved for tests (``read_bundle_fn``, ``read_bank_fn``).
    """
    if not _light_batch_enabled():
        raise RuntimeError(
            "LEDGR_LIGHT_BATCH is disabled; the invoice_processing factory spine "
            "was removed. Set LEDGR_LIGHT_BATCH=1."
        )

    start_time = time.perf_counter()

    if tool_context is not None and getattr(tool_context, "state", None) is not None:
        state = tool_context.state
    else:
        state = playground_default_context().to_state()

    if isinstance(state, dict):
        seed_playground_profile_if_needed(state)

    client = client_context_from_state(state if isinstance(state, dict) else playground_default_context().to_state())

    existing_paths, missing_files, path_resolution = resolve_document_paths(
        tool_context,
        paths,
    )
    documents_skipped_before_llm = len(missing_files)
    source_files = [str(p) for p in existing_paths] if existing_paths else [str(p) for p in paths]
    gate_units = _estimate_gate_units(existing_paths) if existing_paths else len(source_files)

    firm_id = client.firm_id or client.slack_team_id
    credit_decision = _credit_gate(
        firm_id=firm_id,
        paths=source_files,
        required_units=gate_units,
    )
    credit_balance = credit_decision.get("balance")
    credit_remaining = int(credit_balance) if firm_id and isinstance(credit_balance, int) else None

    if not credit_decision.get("allowed", True):
        return _empty_blocked_batch(
            client=client,
            source_files=source_files,
            missing_files=missing_files,
            blocked_reason=str(credit_decision.get("reason", "zero_credit")),
            gate_units=gate_units,
            credit_remaining=credit_remaining,
            path_resolution=path_resolution,
        )

    if not existing_paths:
        blocked_reason = "no_source_files" if not paths else "no_readable_files"
        return _empty_blocked_batch(
            client=client,
            source_files=source_files,
            missing_files=missing_files,
            blocked_reason=blocked_reason,
            gate_units=gate_units,
            credit_remaining=credit_remaining,
            documents_skipped_before_llm=documents_skipped_before_llm,
            path_resolution=path_resolution,
        )

    read_bundle_fn = inject.get("read_bundle_fn")
    read_bank_fn = inject.get("read_bank_fn")
    if read_bundle_fn is not None or read_bank_fn is not None:
        payload = _process_batch_light_injected(
            existing_paths,
            client=client,
            source_files=source_files,
            missing_files=missing_files,
            read_bundle_fn=read_bundle_fn,
            read_bank_fn=read_bank_fn,
            documents_skipped_before_llm=documents_skipped_before_llm,
            elapsed_ms=int((time.perf_counter() - start_time) * 1000),
        )
    else:
        import asyncio

        payload = asyncio.run(
            process_batch_light_async(
                [str(p) for p in existing_paths],
                client,
                missing_files=missing_files,
                source_files=source_files,
                documents_skipped_before_llm=documents_skipped_before_llm,
                elapsed_ms=int((time.perf_counter() - start_time) * 1000),
            )
        )

    posted_count = len(payload.get("posted_documents") or [])
    credits_summary = _charge_credits_summary(
        firm_id=firm_id,
        posted_count=posted_count,
        gate_units=gate_units,
        source_files=source_files,
        credit_remaining=credit_remaining,
    )
    payload["credits"] = credits_summary.model_dump()

    truth_report = document_truth_report(existing_paths, payload.get("export_rows") or [])
    expected_invoice_count = truth_report.get("expected_invoice_count")
    delivery_units = (
        int(expected_invoice_count)
        if isinstance(expected_invoice_count, int) and expected_invoice_count > 0
        else payload.get("documents_processed", 0)
    )
    _merge_validation(
        payload,
        {
            **path_resolution,
            "document_truth": truth_report,
            "credit_estimate": {
                "gate_units": gate_units,
                "estimated_delivery_units": delivery_units,
                "charged_in_playground": False,
                "rule": "gate by source page count; charge delivered docs in live Slack",
            },
        },
    )
    return _finalize_batch_payload(payload)


def _process_batch_light_injected(
    paths: list[Path],
    *,
    client: ClientContext,
    source_files: list[str],
    missing_files: list[str],
    read_bundle_fn: Callable[..., Any] | None,
    read_bank_fn: Callable[..., Any] | None,
    documents_skipped_before_llm: int,
    elapsed_ms: int,
) -> dict[str, Any]:
    from ledgr_agent.extract.document_bundle import read_document_bundle
    from ledgr_agent.export.bank_workbook import build_bank_workbook
    from ledgr_agent.pipeline.batch_result_builder import LightFileResult, build_batch_result

    bundle_reader = read_bundle_fn or read_document_bundle

    file_results: list[LightFileResult] = []
    llm_models: list[str] = []
    llm_calls = 0
    strong_model_used = False

    for path in paths:
        payload = bundle_reader(path)
        if payload.get("status") == "error":
            file_results.append(
                LightFileResult(
                    path=str(path),
                    kind="bill",
                    status="error",
                    message=str(payload.get("message")),
                )
            )
            continue

        meta = dict(payload.get("extraction_meta") or {})
        if payload.get("file_kind") == "bank_statement":
            workbook = build_bank_workbook(
                payload,
                extract_mode=meta.get("extract_mode"),
            )
            file_results.append(
                LightFileResult(
                    path=str(path),
                    kind="bank",
                    bank_workbook=workbook,
                    extraction_meta=meta,
                )
            )
        else:
            file_results.append(
                LightFileResult(
                    path=str(path),
                    kind="bill",
                    documents=list(payload.get("documents") or []),
                    extraction_meta=meta,
                )
            )
        meta = file_results[-1].extraction_meta
        llm_calls += int(meta.get("gemini_call_count") or 0)
        model = meta.get("model")
        if isinstance(model, str) and model and model not in llm_models:
            llm_models.append(model)
        if meta.get("extract_mode") == "vision":
            strong_model_used = True

    batch = build_batch_result(
        file_results,
        client=client,
        source_files=source_files,
        missing_files=missing_files,
        llm_call_count=llm_calls,
        models_used=llm_models,
        strong_model_used=strong_model_used,
        documents_skipped_before_llm=documents_skipped_before_llm,
        elapsed_ms=elapsed_ms,
    )
    return batch.model_dump()


def _charge_credits_summary(
    *,
    firm_id: str | None,
    posted_count: int,
    gate_units: int,
    source_files: list[str],
    credit_remaining: int | None,
) -> CreditSummary:
    if firm_id and posted_count > 0 and _charge_credits_in_tool():
        try:
            service = _get_credit_service()
            if service is not None:
                sorted_basenames = sorted(Path(f).name for f in source_files)
                idem_key = f"{firm_id}:{'|'.join(sorted_basenames)}"
                new_balance = service.deduct(
                    firm_id,
                    amount=posted_count,
                    reason="delivery",
                    idempotency_key=idem_key,
                )
                return CreditSummary(
                    credits_estimated=gate_units,
                    credits_used=posted_count,
                    credits_remaining=int(new_balance),
                    credit_status="charged",
                )
        except Exception as exc:  # noqa: BLE001
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "Credit deduct failed (billing skipped, processing continues): %s", exc
            )
        return CreditSummary(
            credits_estimated=gate_units,
            credits_used=0,
            credits_remaining=credit_remaining,
            credit_status="not_checked",
        )
    if not firm_id:
        return CreditSummary(
            credits_estimated=gate_units,
            credits_used=0,
            credits_remaining=None,
            credit_status="not_billable",
        )
    return CreditSummary(
        credits_estimated=gate_units,
        credits_used=0,
        credits_remaining=credit_remaining,
        credit_status="estimated" if posted_count == 0 else "estimated",
    )
