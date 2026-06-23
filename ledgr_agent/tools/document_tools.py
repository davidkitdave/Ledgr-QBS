from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from invoice_processing.pipeline import BatchResult as EngineBatchResult
from invoice_processing.pipeline import process_batch as engine_process_batch
from invoice_processing.shared_libraries.model_config import lite_model, resolve_model
from ledgr_agent.client_registry import resolve_client
from ledgr_agent.tools.batch_mapper import map_engine_batch_to_contract

PipelineInject = dict[str, Callable[..., Any]]
_pipeline_overrides: PipelineInject = {}


def configure_document_batch_pipeline(**inject: Callable[..., Any]) -> None:
    """Replace engine callables for hermetic tests (same pattern as ``process_batch`` inject)."""

    _pipeline_overrides.clear()
    _pipeline_overrides.update(inject)


def reset_document_batch_pipeline() -> None:
    """Clear injected engine callables after tests."""

    _pipeline_overrides.clear()


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


def _build_pipeline_inject() -> tuple[PipelineInject, dict[str, Any]]:
    """Build engine inject kwargs and LLM telemetry for the current call."""

    telemetry: dict[str, Any] = {
        "llm_call_count": 0,
        "models_used": [],
        "strong_model_used": False,
    }

    if _pipeline_overrides:
        return dict(_pipeline_overrides), telemetry

    from invoice_processing.classify.document_classifier import classify_file
    from invoice_processing.extract.bank_statement_extractor import extract_bank_file
    from invoice_processing.extract.invoice_extractor import extract_file

    lite_name = lite_model()

    def _track(fn: Callable[..., Any], *, model_name: str, strong: bool = False) -> Callable[..., Any]:
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            telemetry["llm_call_count"] += 1
            if model_name not in telemetry["models_used"]:
                telemetry["models_used"].append(model_name)
            if strong:
                telemetry["strong_model_used"] = True
            return fn(*args, **kwargs)

        return wrapped

    inject: PipelineInject = {
        "classify_fn": _track(classify_file, model_name=lite_name),
        "extract_fn": _track(extract_file, model_name=lite_name),
        "bank_fn": _track(
            extract_bank_file,
            model_name=resolve_model("std"),
            strong=True,
        ),
    }
    return inject, telemetry


def _empty_engine_result() -> EngineBatchResult:
    return EngineBatchResult(workbooks={}, docs=[], errors=[])


def process_document_batch(client_id: str, source_files: list[str]) -> dict[str, Any]:
    """Process uploaded documents through the accounting engine and return ``BatchResult``."""

    if not source_files:
        batch = map_engine_batch_to_contract(
            _empty_engine_result(),
            client=type("MissingClient", (), {"client_id": client_id, "firm_id": None})(),
            source_files=[],
            missing_files=[],
            blocked_reason="no_source_files",
        )
        return batch.model_dump()

    client = resolve_client(client_id)
    if client is None:
        batch = map_engine_batch_to_contract(
            _empty_engine_result(),
            client=type("MissingClient", (), {"client_id": client_id, "firm_id": None})(),
            source_files=list(source_files),
            missing_files=[],
            blocked_reason="client_not_found",
        )
        return batch.model_dump()

    existing_paths, missing_files = _resolve_existing_paths(source_files)
    documents_skipped_before_llm = len(missing_files)

    if not existing_paths:
        batch = map_engine_batch_to_contract(
            _empty_engine_result(),
            client=client,
            source_files=list(source_files),
            missing_files=missing_files,
            blocked_reason="no_readable_files",
            documents_skipped_before_llm=documents_skipped_before_llm,
        )
        return batch.model_dump()

    inject, telemetry = _build_pipeline_inject()
    engine_result = engine_process_batch(existing_paths, client, **inject)
    batch = map_engine_batch_to_contract(
        engine_result,
        client=client,
        source_files=list(source_files),
        missing_files=missing_files,
        llm_call_count=int(telemetry["llm_call_count"]),
        models_used=list(telemetry["models_used"]),
        strong_model_used=bool(telemetry["strong_model_used"]),
        documents_skipped_before_llm=documents_skipped_before_llm,
    )
    return batch.model_dump()
