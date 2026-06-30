"""SG/MY tax reference YAML for TaxClassifier and jurisdiction unit tests."""

from __future__ import annotations

from pathlib import Path

import yaml

_TAX_REF_DIR = Path(__file__).resolve().parent / "tax_reference"

_TAXONOMY_CACHE: dict[str, dict] = {}
_KEYWORD_ALIASES_CACHE: list[tuple[str, str, str]] | None = None


def load_taxonomy(yaml_name: str) -> dict:
    """Load and cache a taxonomy YAML from ``ledgr_slack/tax_reference/``."""
    if yaml_name not in _TAXONOMY_CACHE:
        path = _TAX_REF_DIR / yaml_name
        with open(path, encoding="utf-8") as f:
            _TAXONOMY_CACHE[yaml_name] = yaml.safe_load(f) or {}
    return _TAXONOMY_CACHE[yaml_name]


def load_keyword_aliases() -> list[tuple[str, str, str]]:
    """Load+cache the ordered keyword alias ladder as (treatment, match, pattern)."""
    global _KEYWORD_ALIASES_CACHE
    if _KEYWORD_ALIASES_CACHE is None:
        data = load_taxonomy("tax_aliases.yaml")
        rows = data.get("keyword_aliases") or []
        _KEYWORD_ALIASES_CACHE = [
            (str(r["treatment"]), str(r["match"]), str(r["pattern"]).lower())
            for r in rows
        ]
    return _KEYWORD_ALIASES_CACHE


def clear_tax_reference_cache() -> None:
    """Reset cached loads. Test helper."""
    global _KEYWORD_ALIASES_CACHE
    _TAXONOMY_CACHE.clear()
    _KEYWORD_ALIASES_CACHE = None
