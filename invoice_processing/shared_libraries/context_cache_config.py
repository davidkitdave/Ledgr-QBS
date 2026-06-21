"""Central context-cache settings for Ledgr (WS-6.2).

ADK Apps use :func:`ledgr_context_cache_config` for automatic prefix caching on
``static_instruction`` / shared agent instructions.

Direct ``generate_content`` call sites (extraction, COA) split prompts into:
  - **system_instruction** — invariant rules (cacheable prefix)
  - **contents** — per-call dynamic payload (PDF, client context, COA JSON, lines)

Explicit COA caches are keyed by :func:`coa_cache_fingerprint` so a mutated COA
within TTL never reuses a stale cache entry.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

from google.adk.agents.context_cache_config import ContextCacheConfig

logger = logging.getLogger(__name__)

LEDGR_CONTEXT_CACHE_MIN_TOKENS = int(os.getenv("LEDGR_CONTEXT_CACHE_MIN_TOKENS", "2048"))
LEDGR_CONTEXT_CACHE_TTL_SECONDS = int(os.getenv("LEDGR_CONTEXT_CACHE_TTL_SECONDS", "1800"))
LEDGR_CONTEXT_CACHE_INTERVALS = int(os.getenv("LEDGR_CONTEXT_CACHE_INTERVALS", "5"))


def ledgr_context_cache_config() -> ContextCacheConfig:
    """Return ADK ``ContextCacheConfig`` for document and assistant Apps."""
    return ContextCacheConfig(
        min_tokens=LEDGR_CONTEXT_CACHE_MIN_TOKENS,
        ttl_seconds=LEDGR_CONTEXT_CACHE_TTL_SECONDS,
        cache_intervals=LEDGR_CONTEXT_CACHE_INTERVALS,
    )


def coa_cache_fingerprint(coa_json: str, model: str) -> str:
    """Stable cache key for a COA JSON blob + model pair.

    Include the model because cache entries are model-scoped on the API side.
    """
    digest = hashlib.sha256(f"{model}:{coa_json}".encode()).hexdigest()
    return digest


def log_context_cache_usage(resp: Any, *, lane: str) -> None:
    """Log cached vs total prompt tokens from ``usage_metadata`` when present."""
    usage = getattr(resp, "usage_metadata", None)
    if usage is None:
        return
    cached = getattr(usage, "cached_content_token_count", None)
    prompt = getattr(usage, "prompt_token_count", None)
    if cached is None and prompt is None:
        return
    logger.info(
        "context_cache lane=%s cached_content_token_count=%s prompt_token_count=%s",
        lane,
        cached,
        prompt,
    )


@dataclass
class _CoaCacheEntry:
    cache_name: str
    expires_at: float
    fingerprint: str


_coa_inprocess_cache: dict[str, _CoaCacheEntry] = {}


def _coa_explicit_cache_enabled() -> bool:
    raw = os.getenv("LEDGR_COA_EXPLICIT_CACHE", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _prune_expired_coa_cache(now: float) -> None:
    expired = [k for k, v in _coa_inprocess_cache.items() if v.expires_at <= now]
    for key in expired:
        del _coa_inprocess_cache[key]


def resolve_coa_cached_content(
    client: Any,
    *,
    model: str,
    coa_json: str,
    static_instruction: str,
) -> Optional[str]:
    """Return an explicit ``cached_content`` name for a COA prefix, or None.

    Hermetic tests pass fake clients without ``caches`` — this returns None and
    callers fall back to system_instruction-only caching.
    """
    if not _coa_explicit_cache_enabled():
        return None

    fingerprint = coa_cache_fingerprint(coa_json, model)
    now = time.time()
    _prune_expired_coa_cache(now)

    entry = _coa_inprocess_cache.get(fingerprint)
    if entry and entry.expires_at > now and entry.fingerprint == fingerprint:
        return entry.cache_name

    caches_api = getattr(client, "caches", None)
    if caches_api is None:
        return None

    from google.genai import types

    try:
        created = caches_api.create(
            model=model,
            config=types.CreateCachedContentConfig(
                display_name=f"ledgr-coa-{fingerprint[:16]}",
                system_instruction=static_instruction,
                contents=[
                    types.Content(
                        role="user",
                        parts=[types.Part(text=f"COA (client chart of accounts):\n{coa_json}")],
                    )
                ],
                ttl=f"{LEDGR_CONTEXT_CACHE_TTL_SECONDS}s",
            ),
        )
    except Exception:
        logger.debug("COA explicit cache create failed", exc_info=True)
        return None

    cache_name = getattr(created, "name", None)
    if not cache_name:
        return None

    _coa_inprocess_cache[fingerprint] = _CoaCacheEntry(
        cache_name=cache_name,
        expires_at=now + LEDGR_CONTEXT_CACHE_TTL_SECONDS,
        fingerprint=fingerprint,
    )
    return cache_name


def clear_coa_inprocess_cache_for_tests() -> None:
    """Reset in-process COA cache (tests only)."""
    _coa_inprocess_cache.clear()
