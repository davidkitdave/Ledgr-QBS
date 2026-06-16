"""Optional Document AI Layout Parser preprocessor (P3 conditional).

Enabled only when ``LEDGR_LAYOUT_PARSER=1`` and structure heuristics request it.
Default: no-op so Phase 1 runs on raw bytes without extra infrastructure.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def layout_parser_enabled() -> bool:
    return os.environ.get("LEDGR_LAYOUT_PARSER", "").strip().lower() in ("1", "true", "yes")


def needs_layout_parser_heuristic(data: bytes, mime_type: str) -> bool:
    """Return True when table-heavy structure suggests Layout Parser may help."""
    if not layout_parser_enabled():
        return False
    # Lightweight heuristic: large PDFs often carry multi-page tables.
    return mime_type == "application/pdf" and len(data) > 500_000


def maybe_preprocess_tables(
    data: bytes,
    mime_type: str,
) -> Optional[tuple[bytes, str]]:
    """Return preprocessed (bytes, mime) when Layout Parser is configured, else None."""
    if not needs_layout_parser_heuristic(data, mime_type):
        return None
    logger.info(
        "layout_parser: heuristic matched but Document AI preprocessor is not "
        "configured — pass through raw document (set LEDGR_LAYOUT_PARSER=1 and "
        "wire Document AI credentials to enable)"
    )
    return None
