"""Light batch orchestrator: parallel fan-out across files, deterministic fan-in."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ledgr_agent.export.bank_workbook import build_bank_workbook
from ledgr_agent.extract.document_bundle import read_document_bundle
from ledgr_agent.models.client_context import ClientContext
from ledgr_agent.pipeline.batch_result_builder import LightFileResult, build_batch_result
from ledgr_agent.schemas.credit import CreditSummary

_DEFAULT_CONCURRENCY = 4


def _process_file_sync(path: Path) -> LightFileResult:
    payload = read_document_bundle(path)
    if payload.get("status") == "error":
        return LightFileResult(
            path=str(path),
            kind="bill",
            status="error",
            message=str(payload.get("message") or "read failed"),
        )

    meta = dict(payload.get("extraction_meta") or {})
    if payload.get("file_kind") == "bank_statement":
        workbook = build_bank_workbook(
            payload,
            extract_mode=meta.get("extract_mode"),
        )
        return LightFileResult(
            path=str(path),
            kind="bank",
            bank_workbook=workbook,
            extraction_meta=meta,
        )

    return LightFileResult(
        path=str(path),
        kind="bill",
        documents=list(payload.get("documents") or []),
        extraction_meta=meta,
    )


async def _process_file(
    path: Path,
    *,
    semaphore: asyncio.Semaphore,
) -> LightFileResult:
    async with semaphore:
        return await asyncio.to_thread(_process_file_sync, path)


def _telemetry_from_results(results: list[LightFileResult]) -> tuple[int, list[str], bool]:
    llm_calls = 0
    models: list[str] = []
    strong = False
    for result in results:
        meta = result.extraction_meta or {}
        llm_calls += int(meta.get("gemini_call_count") or 0)
        model = meta.get("model")
        if isinstance(model, str) and model and model not in models:
            models.append(model)
        if meta.get("extract_mode") == "vision":
            strong = True
    return llm_calls, models, strong


async def process_batch_light_async(
    paths: list[str | Path],
    client: ClientContext,
    *,
    missing_files: list[str] | None = None,
    source_files: list[str] | None = None,
    blocked_reason: str | None = None,
    documents_skipped_before_llm: int = 0,
    credits: CreditSummary | None = None,
    elapsed_ms: int | None = None,
    max_concurrency: int = _DEFAULT_CONCURRENCY,
) -> dict[str, Any]:
    """Fan-out one light reader per file, fan-in to :class:`BatchResult` JSON."""
    path_objs = [Path(p) for p in paths]
    semaphore = asyncio.Semaphore(max(1, max_concurrency))
    raw_results = await asyncio.gather(
        *[_process_file(path, semaphore=semaphore) for path in path_objs],
        return_exceptions=True,
    )

    file_results: list[LightFileResult] = []
    for path_obj, raw in zip(path_objs, raw_results, strict=True):
        if isinstance(raw, Exception):
            file_results.append(
                LightFileResult(
                    path=str(path_obj),
                    kind="bill",
                    status="error",
                    message=str(raw),
                )
            )
        else:
            file_results.append(raw)

    llm_call_count, models_used, strong_model_used = _telemetry_from_results(file_results)
    batch = build_batch_result(
        file_results,
        client=client,
        source_files=list(source_files or [str(p) for p in path_objs]),
        missing_files=list(missing_files or []),
        blocked_reason=blocked_reason,
        llm_call_count=llm_call_count,
        models_used=models_used,
        strong_model_used=strong_model_used,
        documents_skipped_before_llm=documents_skipped_before_llm,
        elapsed_ms=elapsed_ms,
        credits=credits,
    )
    return batch.model_dump()


def process_batch_light(
    paths: list[str | Path],
    client: ClientContext,
    **kwargs: Any,
) -> dict[str, Any]:
    """Synchronous entry point for the light batch path."""
    return asyncio.run(process_batch_light_async(paths, client, **kwargs))
