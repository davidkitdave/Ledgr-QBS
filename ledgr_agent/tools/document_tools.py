from __future__ import annotations

import time
from typing import Any

from invoice_processing.export.client_context import client_context_from_state
from invoice_processing.pipeline import process_batch


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

    # 2. Call the underlying procedual engine
    engine_result = process_batch(paths, client, **inject)

    # 3. Estimate LLM call counts
    llm_call_count = 0
    for doc in engine_result.docs:
        if not doc.note.startswith("ERROR"):
            if doc.doc_type == "bank_statement":
                llm_call_count += 1
            elif doc.doc_type in ("invoice", "receipt"):
                llm_call_count += 3

    elapsed_ms = int((time.perf_counter() - start_time) * 1000)

    # 4. Map engine result to posted / skipped documents and extract review requests
    from ledgr_agent.tools.batch_mapper import map_engine_batch_to_contract
    batch_result = map_engine_batch_to_contract(
        engine_result,
        client=client,
        source_files=[str(p) for p in paths],
        missing_files=[],
        llm_call_count=llm_call_count,
        models_used=["gemini-2.5-flash-lite"],
        elapsed_ms=elapsed_ms,
    )

    return batch_result.model_dump()
