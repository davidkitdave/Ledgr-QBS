"""Load ERP export profiles from ADK-style skill directories."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills"

_SYSTEM_DIRS: dict[str, str] = {
    "qbs": "erp-qbs",
    "xero": "erp-xero",
    "autocount": "erp-autocount",
    "sql_account": "erp-sql-account",
}

DEFAULT_SYSTEMS: list[str] = list(_SYSTEM_DIRS)

_SKILL_REQUIRED_KEYS = ("software_name", "system", "purchase_cols", "sales_cols")

_SKILL_CACHE: dict[str, dict[str, Any]] = {}


class ExportSkillError(RuntimeError):
    """Raised when an ERP skill YAML is missing or malformed."""


def normalize_system_key(system: str) -> str:
    """Return canonical exporter key for *system*."""
    key = (system or "").strip().lower().replace(" ", "_")
    aliases = {
        "qbs_ledger": "qbs",
        "sqlaccount": "sql_account",
    }
    return aliases.get(key, key)


def skill_asset_path(system: str) -> Path:
    """Return ``skills/erp-<system>/assets/profile.yaml`` for canonical *system*."""
    key = normalize_system_key(system)
    dir_name = _SYSTEM_DIRS.get(key)
    if dir_name is None:
        raise ExportSkillError(
            f"no export skill registered for system '{system}'; have {sorted(_SYSTEM_DIRS)}"
        )
    return _SKILL_ROOT / dir_name / "assets" / "profile.yaml"


def load_export_skill(system: str) -> dict[str, Any]:
    """Load (cached) the export skill for canonical *system*."""
    key = normalize_system_key(system)
    cached = _SKILL_CACHE.get(key)
    if cached is not None:
        return cached

    path = skill_asset_path(key)
    if not path.is_file():
        raise ExportSkillError(f"export skill file missing: {path}")
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ExportSkillError(f"export skill '{path}' is not valid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ExportSkillError(
            f"export skill '{path}' must be a YAML mapping, got {type(data).__name__}"
        )
    for req in _SKILL_REQUIRED_KEYS:
        if req not in data:
            raise ExportSkillError(f"export skill '{path}' is missing required key '{req}'")
    if not isinstance(data["purchase_cols"], list) or not isinstance(data["sales_cols"], list):
        raise ExportSkillError(f"export skill '{path}' purchase_cols/sales_cols must be lists")
    if data["system"] != key:
        raise ExportSkillError(
            f"export skill '{path}' declares system '{data['system']}' "
            f"but is registered as '{key}'"
        )

    _SKILL_CACHE[key] = data
    return data
