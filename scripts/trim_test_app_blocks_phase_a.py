#!/usr/bin/env python3
"""Remove test classes for archived HITL/chat Block Kit builders."""

from __future__ import annotations

import re
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "tests" / "test_app_blocks.py"

REMOVE_CLASSES = frozenset({
    "TestResultCardPerDoc",
    "TestApprovalCardBlocks",
    "TestInvoiceEditModal",
    "TestProactiveRedoBlocks",
    "TestProactiveRedoModal",
    "TestPerDocCardNative",
    "TestPerDocCardFallback",
    "TestResultCardNativeMode",
    "TestResultCardFallbackMode",
    "TestFeedbackButtonsBlockNative",
    "TestFeedbackButtonsBlockFallback",
    "TestResultCardFeedbackIntegration",
    "TestResultCardFeedbackFallback",
    "TestApprovalCardBlocksNative",
    "TestApprovalCardBlocksFallback",
    "TestReviewCardBlocksNative",
    "TestReviewCardBlocksFallback",
    "TestProactiveRedoBlocksNative",
    "TestProactiveRedoBlocksFallback",
    "TestReviewCardBlocksNeverEmptyBody",
})

CLASS_RE = re.compile(r"^class ([A-Za-z0-9_]+)")


def trim(text: str) -> str:
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    removed = 0
    while i < len(lines):
        line = lines[i]
        m = CLASS_RE.match(line)
        if m and m.group(1) in REMOVE_CLASSES:
            removed += 1
            i += 1
            while i < len(lines) and not CLASS_RE.match(lines[i]):
                i += 1
            continue
        out.append(line)
        i += 1
    print(f"Removed {removed} test classes", file=sys.stderr)
    return "".join(out)


def main() -> int:
    original = TARGET.read_text(encoding="utf-8")
    trimmed = trim(original)
    # Fix imports block
    trimmed = trimmed.replace(
        """from app.blocks import (
    _dedup_value,
    approval_card_blocks,
    coa_prompt_blocks,
    dedup_callout_card,
    feedback_buttons_block,
    invoice_edit_modal,
    job_summary_text,
    ledger_preview_data_table,
    make_feedback_doc_ref,
    onboarding_modal,
    per_doc_card,
    proactive_redo_blocks,
    proactive_redo_modal,
    processing_plan_headline,
    profile_summary_blocks,
    result_card,
    review_card_blocks,
    welcome_blocks,
""",
        """from app.blocks import (
    _dedup_value,
    coa_prompt_blocks,
    dedup_callout_card,
    job_summary_text,
    ledger_preview_data_table,
    onboarding_modal,
    processing_plan_headline,
    profile_summary_blocks,
    welcome_blocks,
""",
    )
    TARGET.write_text(trimmed, encoding="utf-8")
    print(f"Wrote {TARGET} ({len(original.splitlines())} -> {len(trimmed.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
