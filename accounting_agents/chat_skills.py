"""Chat playbooks as ADK Skills (P8).

Skills are optional load-on-demand instruction bundles. They complement the
slim ``_BASE_INSTRUCTION`` routing tree — they do **not** replace
``FunctionTool``s. Wire via ``SkillToolset`` when ``LEDGR_CHAT_SKILLS=1`` (see
ADR-0013); default chat lane keeps the 22-tool surface unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path

from google.adk.skills import load_skill_from_dir

_SKILLS_ROOT = Path(__file__).resolve().parent / "skills"

_SKILL_DIRS = (
    "ledger-read",
    "extraction-introspect",
    "write-gated",
)


def chat_skills_enabled() -> bool:
    """Return True when ADK SkillToolset should be wired on the chat agent."""
    return os.environ.get("LEDGR_CHAT_SKILLS", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def load_chat_skills():
    """Load bundled chat playbooks from ``accounting_agents/skills/``."""
    return [
        load_skill_from_dir(_SKILLS_ROOT / name)
        for name in _SKILL_DIRS
    ]
