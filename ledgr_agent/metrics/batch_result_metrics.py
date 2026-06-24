from __future__ import annotations

from typing import Any


def _function_responses(instance: dict[str, Any], name: str) -> list[dict[str, Any]]:
    responses: list[dict[str, Any]] = []
    agent_data = instance.get("agent_data") or {}
    for turn in agent_data.get("turns", []):
        for event in turn.get("events", []):
            content = event.get("content") or {}
            for part in content.get("parts", []):
                response = part.get("function_response")
                if isinstance(response, dict) and response.get("name") == name:
                    payload = response.get("response")
                    if isinstance(payload, dict):
                        responses.append(payload)
    return responses


def _latest_batch_result(instance: dict[str, Any]) -> dict[str, Any] | None:
    responses = _function_responses(instance, "process_document_batch")
    return responses[-1] if responses else None


def cost_efficiency_code(instance: dict[str, Any]) -> dict[str, Any]:
    """Score normal batch traces for limited LLM calls and no stronger fallback."""

    batch = _latest_batch_result(instance)
    if batch is None:
        return {"score": 1.0, "explanation": "no document batch result in trace"}

    llm_call_count = int(batch.get("llm_call_count") or 0)
    strong_model_used = bool(batch.get("strong_model_used"))
    if llm_call_count <= 2 and not strong_model_used:
        return {"score": 1.0, "explanation": "normal batch stayed within cost budget"}
    return {
        "score": 0.0,
        "explanation": f"llm_call_count={llm_call_count}, strong_model_used={strong_model_used}",
    }


def no_unneeded_llm_code(instance: dict[str, Any]) -> dict[str, Any]:
    """Fail when deterministic gates spend Gemini calls before they should."""

    batch = _latest_batch_result(instance)
    if batch is None:
        return {"score": 1.0, "explanation": "no document batch result in trace"}

    validation = batch.get("validation_summary") or {}
    block_reason = validation.get("block_reason")
    llm_call_count = int(batch.get("llm_call_count") or 0)
    deterministic_gate = block_reason in {"zero_credit", "duplicate", "unsupported_file"}
    if deterministic_gate and llm_call_count > 0:
        return {
            "score": 0.0,
            "explanation": f"{block_reason} gate spent {llm_call_count} LLM calls",
        }
    return {"score": 1.0, "explanation": "no unneeded LLM calls detected"}


def tax_validity_code(instance: dict[str, Any]) -> dict[str, Any]:
    batch = _latest_batch_result(instance)
    if batch is None:
        return {"score": 1.0, "explanation": "no batch to grade"}
    version = (batch.get("validation_summary") or {}).get("tax_policy_version")
    hard_ids = {item.get("id") for item in batch.get("review_requests") or []}
    if version and "gst_claimed_by_non_registered_client" in hard_ids:
        return {"score": 1.0, "explanation": "policy violation correctly flagged"}
    if version:
        return {"score": 1.0, "explanation": f"policy {version} applied"}
    return {"score": 0.0, "explanation": "missing tax_policy_version"}


def credit_charge_code(instance: dict[str, Any]) -> dict[str, Any]:
    batch = _latest_batch_result(instance)
    if batch is None:
        return {"score": 1.0, "explanation": "no batch"}
    credits = batch.get("credits") or {}
    status = credits.get("credit_status")
    block = (batch.get("validation_summary") or {}).get("block_reason")
    if block == "zero_credit" and int(batch.get("llm_call_count") or 0) == 0:
        return {"score": 1.0, "explanation": "zero credit blocked before LLM"}
    if status in {"charged", "not_billable", "estimated"}:
        return {"score": 1.0, "explanation": f"credit_status={status}"}
    return {"score": 0.0, "explanation": "unexpected credit state"}


def accounting_task_success_code(instance: dict[str, Any]) -> dict[str, Any]:
    """Score 1.0 when the batch terminal status is 'success'."""

    batch = _latest_batch_result(instance)
    if batch is None:
        return {"score": 1.0, "explanation": "no document batch result in trace"}

    status = batch.get("status")
    if status == "success":
        return {"score": 1.0, "explanation": "batch status=success"}
    return {"score": 0.0, "explanation": f"batch status={status!r}"}


_VALID_DOC_TYPES = {"invoice", "receipt", "credit_note", "bank_statement", "mixed"}


def doc_type_code(instance: dict[str, Any]) -> dict[str, Any]:
    """Score 1.0 when the batch has a recognised non-empty doc_type label."""

    batch = _latest_batch_result(instance)
    if batch is None:
        return {"score": 1.0, "explanation": "no document batch result in trace"}

    doc_type = batch.get("doc_type")
    if isinstance(doc_type, str) and doc_type in _VALID_DOC_TYPES:
        return {"score": 1.0, "explanation": f"doc_type={doc_type}"}
    return {"score": 0.0, "explanation": f"unrecognised doc_type={doc_type!r}"}


def erp_export_shape_code(instance: dict[str, Any]) -> dict[str, Any]:
    """Score 1.0 when the batch carries a list of export_rows."""

    batch = _latest_batch_result(instance)
    if batch is None:
        return {"score": 1.0, "explanation": "no document batch result in trace"}

    export_rows = batch.get("export_rows")
    if isinstance(export_rows, list):
        return {"score": 1.0, "explanation": f"export_rows list len={len(export_rows)}"}
    return {"score": 0.0, "explanation": "export_rows missing or not a list"}


def hitl_noise_score(instance: dict[str, Any]) -> dict[str, Any]:
    batch = _latest_batch_result(instance)
    if batch is None:
        return {"score": 1.0, "explanation": "no document batch result in trace"}

    review_requests = batch.get("review_requests") or []
    soft_warnings = batch.get("soft_warnings") or []
    soft_review_count = sum(
        1 for item in review_requests if item.get("severity") == "review"
    )
    grouped_account = any(
        item.get("id") == "low_coa_confidence_group" for item in soft_warnings
    )
    if soft_review_count >= 5 and not grouped_account:
        return {
            "score": 0.0,
            "explanation": f"{soft_review_count} ungrouped soft review bullets",
        }
    return {"score": 1.0, "explanation": "review output is grouped or small"}
