"""Make ADK session state safe for Firestore persistence."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any


def find_nested_arrays(value: Any, path: str = "root") -> list[str]:
    """Return paths where a list contains another list (invalid in Firestore)."""
    hits: list[str] = []
    if isinstance(value, list):
        if value and isinstance(value[0], list):
            hits.append(path)
        for idx, item in enumerate(value):
            hits.extend(find_nested_arrays(item, f"{path}[{idx}]"))
    elif isinstance(value, dict):
        for key, item in value.items():
            hits.extend(find_nested_arrays(item, f"{path}.{key}"))
    return hits


def firestore_safe_value(value: Any) -> Any:
    """Recursively coerce state values to Firestore-compatible shapes."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): firestore_safe_value(v) for k, v in value.items()}
    if isinstance(value, list):
        if value and isinstance(value[0], list):
            return [{"cells": firestore_safe_value(row)} for row in value]
        return [firestore_safe_value(item) for item in value]
    return str(value)


def firestore_safe_state(state: dict) -> dict:
    """Return a copy of session state safe to write to Firestore."""
    return firestore_safe_value(dict(state))
