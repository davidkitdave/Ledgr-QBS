"""P6–P8: native ADK audit — live paths must not wire prototype tools."""

from __future__ import annotations

from pathlib import Path


def test_tools_py_not_imported_by_production_modules():
    """ADR-0013: prototype ``tools.py`` must not be wired into live paths."""
    root = Path(__file__).resolve().parents[1] / "ledgr_slack"
    paths = [
        root / "app.py",
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "accounting_agents.tools" not in text
        assert "from .tools import" not in text
