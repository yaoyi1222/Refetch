"""Concurrent fetch of a known URL list for ``lightcrawl batch-fetch`` (v0.3 PR 7).

``batch-fetch`` does NOT use the job framework (no resume/cancel/persistence):
the URL set is known up front, fetched concurrently, and returned in one shot.
Each fetch goes through ``Router.fetch`` so it inherits L1→L2→L3 escalation, the
SSRF guard, and the Router cache aspect (PR 2.3) — like ``crawl.py``, this module
never touches ``fetch_http`` / ``Cache`` directly. Cache defaults to *on, 1h*
(design §3 / §5.7); the CLI resolves flags into the ``CrawlParams``-style cache
fields below.

Top-level ``ok`` means "the batch completed", NOT "every URL succeeded" — callers
must read ``ok_count`` / ``failed_count`` (SKILL.md honesty contract #5).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .errors import ErrorCode
from .router import FetchRequest, Router

_DEFAULT_MAX_AGE_MS = 3_600_000  # 1h — batch's cache-on default (design §3)


@dataclass
class BatchParams:
    concurrency: int = 8
    output_format: str = "markdown"
    profile: str | None = None
    max_inline_tokens: int = 8000
    timeout_ms: int = 30_000
    # Cache controls (passed straight to FetchRequest; the Router aspect does
    # lookup/store). Defaults = batch's "cache on, 1h" behavior.
    max_age_ms: int | None = _DEFAULT_MAX_AGE_MS
    cache_only: bool = False
    store_in_cache: bool = True
    no_cache: bool = False


def _exc_to_failure(url: str, exc: BaseException) -> dict:
    """Belt-and-suspenders: turn an escaped exception into a failed-result dict
    so a single crash can't sink the whole batch (mirrors search/service.py)."""
    return {
        "ok": False, "url": url,
        "error_code": ErrorCode.UNKNOWN.value, "error_detail": f"{type(exc).__name__}: {exc}",
    }


async def run_batch_fetch(urls: list[str], params: BatchParams, router: Router) -> dict:
    """Fetch every URL concurrently (bounded by ``params.concurrency``) and
    return ``{ok, count, ok_count, failed_count, results}``. ``ok`` is always
    true on a completed batch; per-URL outcomes live in ``results``. An empty
    URL list returns ``count: 0`` with a ``notes`` explaining why (honesty
    contract #4)."""
    if not urls:
        return {
            "ok": True, "count": 0, "ok_count": 0, "failed_count": 0,
            "results": [],
            "notes": "no URLs provided; pass URLs or --urls-file",
        }

    sem = asyncio.Semaphore(params.concurrency)

    async def one(url: str) -> dict:
        async with sem:
            try:
                return await router.fetch(FetchRequest(
                    url=url,
                    output_format=params.output_format,
                    profile=params.profile,
                    max_inline_tokens=params.max_inline_tokens,
                    timeout_ms=params.timeout_ms,
                    max_age_ms=params.max_age_ms,
                    cache_only=params.cache_only,
                    store_in_cache=params.store_in_cache,
                    no_cache=params.no_cache,
                ))
            except Exception as e:  # noqa: BLE001 — per-URL isolation; recorded, not swallowed
                return _exc_to_failure(url, e)

    gathered = await asyncio.gather(*(one(u) for u in urls), return_exceptions=True)
    results = [
        r if isinstance(r, dict) else _exc_to_failure(url, r)
        for url, r in zip(urls, gathered)
    ]
    ok_count = sum(1 for r in results if r.get("ok"))
    return {
        "ok": True,
        "count": len(results),
        "ok_count": ok_count,
        "failed_count": len(results) - ok_count,
        "results": results,
    }
