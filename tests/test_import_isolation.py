"""Import-isolation gates: live packages stay self-contained."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _py_files_under(*rel_dirs: str) -> list[Path]:
    out: list[Path] = []
    for rel in rel_dirs:
        base = ROOT / rel
        if not base.is_dir():
            continue
        out.extend(sorted(base.rglob("*.py")))
    return out


def _imports_forbidden_module(tree: ast.AST, forbidden: str) -> list[str]:
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if forbidden in alias.name:
                    hits.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and forbidden in node.module:
            hits.append(node.module)
    return hits


def test_live_packages_do_not_import_removed_legacy_code() -> None:
    """ledgr_agent, ledgr_slack, and app/ must not import legacy or invoice_processing."""
    forbidden = ("invoice_processing", "legacy.", "accounting_agents")
    violations: list[str] = []
    for path in _py_files_under("ledgr_agent", "ledgr_slack", "app"):
        rel = path.relative_to(ROOT)
        if rel.name == "billing.py" and rel.parent.name == "ledgr_agent":
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        for token in forbidden:
                            if token in alias.name:
                                violations.append(f"{rel}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    for token in forbidden:
                        if token in node.module:
                            violations.append(f"{rel}: from {node.module}")
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for token in forbidden:
            for hit in _imports_forbidden_module(tree, token):
                violations.append(f"{rel}: {hit}")
    assert not violations, "live packages import removed legacy code:\n" + "\n".join(violations)
