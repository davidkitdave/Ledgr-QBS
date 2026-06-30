#!/usr/bin/env python3
"""Remove dead HITL/chat UI from app/blocks.py (Phase A)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TARGET = REPO / "app" / "blocks.py"

REMOVE_NAMES = frozenset({
    "approval_card_blocks",
    "approval_outcome_blocks",
    "review_card_blocks",
    "review_outcome_blocks",
    "review_hint_modal",
    "_humanize_review_reason",
    "proactive_redo_blocks",
    "proactive_redo_modal",
    "invoice_edit_modal",
    "_line_account_select_options",
    "per_doc_card",
    "_per_doc_line",
    "_doc_get",
    "_per_doc_card_native",
    "_per_doc_card_fallback",
    "result_card",
    "feedback_buttons_block",
    "make_feedback_doc_ref",
})

DEF_RE = re.compile(r"^def ([a-zA-Z0-9_]+)\(")


def trim_source(text: str) -> str:
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    removed = 0
    while i < len(lines):
        line = lines[i]
        if DEF_RE.match(line) and not line.startswith(" "):
            name = DEF_RE.match(line).group(1)
            if name in REMOVE_NAMES:
                removed += 1
                i += 1
                while i < len(lines) and not (DEF_RE.match(lines[i]) and not lines[i].startswith(" ")):
                    i += 1
                continue
        out.append(line)
        i += 1
    print(f"Removed {removed} functions", file=sys.stderr)
    return "".join(out)


def main() -> int:
    original = TARGET.read_text(encoding="utf-8")
    trimmed = trim_source(original)
    TARGET.write_text(trimmed, encoding="utf-8")
    print(f"Wrote {TARGET} ({len(original.splitlines())} -> {len(trimmed.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
