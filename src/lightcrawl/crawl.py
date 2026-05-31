"""BFS crawl engine for ``lightcrawl crawl`` (v0.3 PR 6.2).

``run_crawl`` is an orchestration layer *above* the Router: every page is
fetched via ``Router.fetch``, so it inherits the L1→L2→L3 escalation, the SSRF
guard, and the Router cache aspect (PR 2.3) for free. The engine never touches
``fetch_http`` / ``fetch_browser`` / ``Cache`` directly.

Frontier is the ``Job``'s own persistent queue (``job.push_frontier`` /
``job.pop_frontier``) rather than a separate ``asyncio.Queue``: the main loop is
single-coroutine so the queue is never accessed concurrently, and using the Job's
frontier means it is crash-safe and ``resume`` works with no extra bookkeeping.
robots checks run serially in this loop (never inside the gathered fetch tasks),
so ``RobotsCache`` needs no concurrency guard.

CLI subcommands, signal handlers, ``--async``/``--wait``, and flag parsing live
in PR 6.3; this module is driven directly with a ``CrawlParams`` + ``Job``.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .canonical import canonicalize_url
from .jobs import FrontierItem, Job
from .robots import RobotsCache
from .router import FetchRequest, Router
from .url_safety import etld1

# crawl/batch default cache window — design §3 / §5.5 #9. The CLI (6.3) resolves
# the cache flags into these fields; --no-cache is an explicit authoritative
# override (no_cache=True).
_DEFAULT_MAX_AGE_MS = 3_600_000  # 1h


@dataclass
class CrawlParams:
    seed: str
    max_depth: int = 3
    max_pages: int = 100
    include_paths: tuple[str, ...] = ()
    exclude_paths: tuple[str, ...] = ()
    allow_subdomains: bool = False
    crawl_entire_domain: bool = False
    ignore_robots: bool = False
    ignore_query_parameters: bool = False
    concurrency: int = 4
    user_agent: str = "*"
    output_format: str = "markdown"
    profile: str | None = None
    # Cache controls (passed straight through to FetchRequest; the Router aspect
    # does the lookup/store). Defaults = crawl's "cache on, 1h" behavior.
    max_age_ms: int | None = _DEFAULT_MAX_AGE_MS
    cache_only: bool = False
    store_in_cache: bool = True
    no_cache: bool = False


def _domain_allows(url: str, params: CrawlParams) -> bool:
    """Domain boundary: host-equal by default; eTLD+1 when ``--allow-subdomains``
    or ``--crawl-entire-domain`` is set."""
    if params.allow_subdomains or params.crawl_entire_domain:
        return etld1(url) == etld1(params.seed)
    return (urlsplit(url).hostname or "") == (urlsplit(params.seed).hostname or "")


def _path_allows(url: str, params: CrawlParams) -> bool:
    """include/exclude on the RAW path+query (before canonicalization, design
    §5.5 #4). Exclude wins over include (fail-closed)."""
    parts = urlsplit(url)
    pq = parts.path or "/"
    if parts.query:
        pq += "?" + parts.query
    for pat in params.exclude_paths:
        if re.search(pat, pq):
            return False
    if params.include_paths:
        return any(re.search(pat, pq) for pat in params.include_paths)
    return True


def _outlinks(result: dict, params: CrawlParams) -> list[str]:
    """In-domain links to enqueue. Domain filter only — the path filter is
    applied at pop time so excluded URLs are counted (pages_skipped_filter)."""
    links = (result.get("metadata") or {}).get("links") or []
    out: list[str] = []
    for link in links:
        url = link.get("url")
        if url and _domain_allows(url, params):
            out.append(url)
    return out


async def fetch_one(item: FrontierItem, params: CrawlParams, router: Router,
                    sem: asyncio.Semaphore) -> dict:
    """Fetch one page via Router (cache handled by the Router aspect). Per-page
    fault isolation: any exception becomes a failed-page result so one crash
    can't abort the crawl. Carries ``depth`` through for expansion."""
    async with sem:
        try:
            result = await router.fetch(FetchRequest(
                url=item.url,
                output_format=params.output_format,
                profile=params.profile,
                max_age_ms=params.max_age_ms,
                cache_only=params.cache_only,
                store_in_cache=params.store_in_cache,
                no_cache=params.no_cache,
            ))
        except Exception as e:  # noqa: BLE001 — per-page isolation; recorded, not swallowed
            result = {"ok": False, "url": item.url,
                      "error_code": "UNKNOWN", "error_detail": str(e)}
    result["depth"] = item.depth
    return result


async def run_crawl(params: CrawlParams, job: Job, router: Router) -> None:
    """BFS over in-domain URLs, fetching each via Router and recording results on
    ``job``. Honors ``job.should_stop()`` (signals/cancel wired in 6.3). On a
    fresh job the seed is enqueued at depth 0; on resume the restored frontier is
    used as-is. Always ``job.finalize()`` at the end."""
    robots = RobotsCache(router=router, ignore=params.ignore_robots,
                         user_agent=params.user_agent)
    if not job._frontier:  # fresh crawl (resume already restored the frontier)
        job.push_frontier(FrontierItem(params.seed, 0))

    sem = asyncio.Semaphore(params.concurrency)
    in_flight: set[asyncio.Task] = set()

    try:
        while not job.should_stop():
            if not job._frontier and not in_flight:
                break
            while job._frontier and len(in_flight) < params.concurrency:
                item = job.pop_frontier()
                canon = canonicalize_url(item.url, ignore_query=params.ignore_query_parameters)
                if canon in job.claimed:
                    continue  # dedup / cycle guard
                if not _domain_allows(item.url, params):
                    continue  # out of scope — not counted
                # The seed (depth 0) is the entry point and is always fetched;
                # include/exclude filters apply only to discovered links.
                if item.depth > 0 and not _path_allows(item.url, params):
                    job.progress.pages_skipped_filter += 1
                    continue
                if not await robots.allows(item.url):
                    job.progress.pages_skipped_robots += 1
                    continue
                job.mark_claimed(canon, item.depth)
                in_flight.add(asyncio.create_task(fetch_one(item, params, router, sem)))

            if not in_flight:
                break  # nothing claimable left this pass
            done, in_flight = await asyncio.wait(in_flight, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                result = task.result()
                job.record(result)
                if result.get("cache_hit"):
                    job.progress.pages_skipped_cache += 1
                if result.get("ok") and result.get("depth", 0) < params.max_depth:
                    for link in _outlinks(result, params):
                        job.push_frontier(FrontierItem(link, result["depth"] + 1))
            if job.progress.pages_fetched >= params.max_pages:
                break
    finally:
        # Stop/cap may leave fetches in flight: cancel + drain them so they
        # don't warn ("Task was destroyed but it is pending") or keep hitting
        # the network for results we won't count. No-op on normal exhaustion.
        for task in in_flight:
            task.cancel()
        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)
        job.finalize()
