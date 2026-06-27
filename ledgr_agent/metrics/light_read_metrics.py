"""Eval metrics for the light read_document + project_to_erp path."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_BASELINE_PATH = (
    Path(__file__).resolve().parents[2] / "tests" / "eval" / "baselines" / "light_read_auditair_iso.json"
)


def _tool_events(agent_data: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    calls: list[str] = []
    responses: dict[str, Any] = {}
    for turn in agent_data.get("turns", []):
        for event in turn.get("events", []):
            content = event.get("content") or {}
            for part in content.get("parts", []):
                fc = part.get("function_call")
                if isinstance(fc, dict) and fc.get("name"):
                    calls.append(str(fc.get("name")))
                fr = part.get("function_response")
                if isinstance(fr, dict) and fr.get("name"):
                    responses[str(fr.get("name"))] = fr.get("response")
    return calls, responses


def _load_baseline(case_id: str) -> dict[str, Any] | None:
    if not _BASELINE_PATH.is_file():
        return None
    try:
        data = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if str(data.get("eval_case_id") or "") != case_id:
        return None
    return data


def light_read_cost_code(instance: dict[str, Any]) -> dict[str, Any]:
    """Score token/call budget for the Auditair light bill path."""
    case_id = str(instance.get("eval_case_id") or "")
    if case_id != "light_read_auditair_iso":
        return {"score": 1.0, "explanation": "no light-read cost requirement for this case"}

    agent_data = instance.get("agent_data") or {}
    calls, responses = _tool_events(agent_data)

    if "process_document_batch" in calls:
        return {
            "score": 0.0,
            "explanation": "factory path used — light read should not call process_document_batch",
        }

    read = responses.get("read_document") or {}
    if read.get("status") == "error":
        return {
            "score": 0.0,
            "explanation": f"read_document error: {read.get('message')}",
        }

    meta = read.get("extraction_meta") or {}
    call_count = int(meta.get("gemini_call_count") or 0)
    if call_count != 1:
        return {
            "score": 0.0,
            "explanation": f"expected gemini_call_count=1, got {call_count}",
        }

    model = str(meta.get("model") or "").lower()
    if "flash" not in model or "pro" in model:
        return {
            "score": 0.0,
            "explanation": f"expected lite-tier flash model, got {meta.get('model')!r}",
        }

    usage = meta.get("usage") or {}
    total_tokens = usage.get("total_token_count")
    if total_tokens is None:
        return {
            "score": 0.0,
            "explanation": "read_document missing extraction_meta.usage.total_token_count",
        }

    total_tokens = int(total_tokens)
    baseline = _load_baseline(case_id)
    ceiling = baseline.get("total_token_count") if baseline else None
    if ceiling is None:
        return {
            "score": 1.0,
            "explanation": (
                f"baseline not set — record total_token_count={total_tokens} in "
                f"{_BASELINE_PATH.name}"
            ),
        }

    ceiling = int(ceiling)
    max_allowed = int(ceiling * 1.10)
    if total_tokens > max_allowed:
        return {
            "score": 0.0,
            "explanation": (
                f"total_token_count={total_tokens} exceeds baseline ceiling "
                f"{max_allowed} (baseline={ceiling} +10%)"
            ),
        }

    if "project_to_erp" not in calls:
        return {
            "score": 0.0,
            "explanation": f"project_to_erp not called; tools={calls}",
        }

    return {
        "score": 1.0,
        "explanation": (
            f"cost ok: gemini_call_count=1, model={meta.get('model')}, "
            f"total_token_count={total_tokens} <= {max_allowed}"
        ),
    }
