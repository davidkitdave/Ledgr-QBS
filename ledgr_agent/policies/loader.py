from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_POLICY_DIR = Path(__file__).parent / "jurisdictions"
_POLICY_FILES = {
    "SG": "sg.yaml",
    "MY": "my.yaml",
}
_REGION_ALIASES = {
    "SINGAPORE": "SG",
    "SG": "SG",
    "SGP": "SG",
    "MALAYSIA": "MY",
    "MY": "MY",
    "MYS": "MY",
}


@lru_cache(maxsize=8)
def load_jurisdiction_policy(market: str) -> dict[str, Any]:
    """Load a versioned jurisdiction policy YAML by market code."""

    key = _REGION_ALIASES.get(market.strip().upper(), market.strip().upper())
    file_name = _POLICY_FILES.get(key)
    if file_name is None:
        supported = ", ".join(sorted(_POLICY_FILES))
        raise ValueError(f"unsupported jurisdiction {market!r}; supported: {supported}")

    path = _POLICY_DIR / file_name
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"jurisdiction policy {path} must be a mapping")
    if data.get("market") != key:
        raise ValueError(f"jurisdiction policy {path} market mismatch: {data.get('market')!r}")
    if not data.get("policy_version"):
        raise ValueError(f"jurisdiction policy {path} is missing policy_version")
    return data
