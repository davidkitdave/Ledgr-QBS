from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import time
from typing import Any

from invoice_processing.pipeline import BatchResult as EngineBatchResult
from invoice_processing.pipeline import process_batch as engine_process_batch
from invoice_processing.shared_libraries.model_config import lite_model, resolve_model
from invoice_processing.export.client_context import client_context_from_state
from ledgr_agent.tools.batch_mapper import map_engine_batch_to_contract

PipelineInject = dict[str, Callable[..., Any]]


def _resolve_existing_paths(source_files: list[str]) -> tuple[list[Path], list[str]]:
    existing: list[Path] = []
    missing: list[str] = []
    for raw in source_files:
        path = Path(raw)
        if path.is_file():
            existing.append(path)
        else:
            missing.append(raw)
    return existing, missing


def _build_pipeline_inject(overrides: dict[str, Any]) -> tuple[PipelineInject, dict[str, Any]]:
    """Build engine inject kwargs and LLM telemetry for the current call."""

    telemetry: dict[str, Any] = {
        "llm_call_count": 0,
        "models_used": [],
        "strong_model_used": False,
    }

    from invoice_processing.classify.document_classifier import classify_file
    from invoice_processing.extract.bank_statement_extractor import extract_bank_file
    from invoice_processing.extract.invoice_extractor import extract_file
    from invoice_processing.export.categorizer import categorize_invoice

    lite_name = lite_model()
    std_name = resolve_model("std")

    def _track(fn: Callable[..., Any], *, model_name: str, strong: bool = False) -> Callable[..., Any]:
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            telemetry["llm_call_count"] += 1
            if model_name not in telemetry["models_used"]:
                telemetry["models_used"].append(model_name)
            if strong:
                telemetry["strong_model_used"] = True
            return fn(*args, **kwargs)

        return wrapped

    base_inject: PipelineInject = {
        "classify_fn": classify_file,
        "extract_fn": extract_file,
        "bank_fn": extract_bank_file,
        "categorize_fn": categorize_invoice,
    }

    # Apply overrides first
    for k, v in overrides.items():
        if v is not None:
            base_inject[k] = v

    # Wrap the active callables to track their telemetry
    if "classify_fn" in base_inject:
        base_inject["classify_fn"] = _track(base_inject["classify_fn"], model_name=lite_name)
    if "extract_fn" in base_inject:
        base_inject["extract_fn"] = _track(base_inject["extract_fn"], model_name=lite_name)
    if "bank_fn" in base_inject:
        base_inject["bank_fn"] = _track(base_inject["bank_fn"], model_name=std_name, strong=True)
    if "categorize_fn" in base_inject:
        base_inject["categorize_fn"] = _track(base_inject["categorize_fn"], model_name=lite_name)

    return base_inject, telemetry


def _empty_engine_result() -> EngineBatchResult:
    return EngineBatchResult(workbooks={}, docs=[], errors=[])


def process_document_batch(tool_context: Any, paths: list[str], **inject: Any) -> dict[str, Any]:
    """Process a batch of document file paths (invoices, receipts, bank statements) for the active client.

    Args:
        paths: List of absolute file paths to the documents to be processed.
        tool_context: Context injected by ADK providing access to the current session state.
        **inject: Seam for dependency injection in testing (e.g. classify_fn).
    """
    start_time = time.perf_counter()

    # 1. Resolve the client context state
    if tool_context is not None and getattr(tool_context, "state", None) is not None:
        state = tool_context.state
    else:
        # Fallback to playground context for local testing/eval
        from accounting_agents.agent import _playground_default_context
        state = _playground_default_context().to_state()

    client = client_context_from_state(state)

    if not paths:
        batch = map_engine_batch_to_contract(
            _empty_engine_result(),
            client=client,
            source_files=[],
            missing_files=[],
            blocked_reason="no_source_files",
        )
        return batch.model_dump()

    existing_paths, missing_files = _resolve_existing_paths(paths)
    documents_skipped_before_llm = len(missing_files)

    if not existing_paths:
        batch = map_engine_batch_to_contract(
            _empty_engine_result(),
            client=client,
            source_files=[str(p) for p in paths],
            missing_files=missing_files,
            blocked_reason="no_readable_files",
            documents_skipped_before_llm=documents_skipped_before_llm,
        )
        return batch.model_dump()

    # 2. Call the underlying procedual engine with dynamic telemetry
    pipeline_inject, telemetry = _build_pipeline_inject(inject)
    engine_result = engine_process_batch([str(p) for p in existing_paths], client, **pipeline_inject)

    elapsed_ms = int((time.perf_counter() - start_time) * 1000)

    # 4. Map engine result to posted / skipped documents and extract review requests
    batch_result = map_engine_batch_to_contract(
        engine_result,
        client=client,
        source_files=[str(p) for p in paths],
        missing_files=missing_files,
        llm_call_count=int(telemetry["llm_call_count"]),
        models_used=list(telemetry["models_used"]),
        strong_model_used=bool(telemetry["strong_model_used"]),
        elapsed_ms=elapsed_ms,
        documents_skipped_before_llm=documents_skipped_before_llm,
    )

    return batch_result.model_dump()
