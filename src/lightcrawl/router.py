from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlparse

from . import auth, content as content_mod, fetch_browser, fetch_http, fetch_pdf
from .cache import Cache, CacheHit
from .errors import ErrorCode, FetchError
from .url_safety import domain_matches, validate_url

Strategy = Literal["auto", "http", "browser", "authed"]


@dataclass
class Attempt:
    strategy: str
    result: str

    def to_dict(self) -> dict:
        return {"strategy": self.strategy, "result": self.result}


@dataclass
class WaitForArg:
    selector: str | None = None
    network_idle: bool = False
    timeout_ms: int = 10_000


@dataclass
class FetchRequest:
    url: str
    strategy: Strategy = "auto"
    profile: str | None = None
    output_format: Literal[
        "markdown", "html", "text", "screenshot", "markdown+screenshot", "links", "images"
    ] = "markdown"
    selector: str | None = None
    wait_for: WaitForArg | None = None
    max_inline_tokens: int = 8000
    timeout_ms: int = 30_000
    # v0.2 PR 1a — HTTP/DOM layer params (L1+L2 compatible)
    headers: dict[str, str] = field(default_factory=dict)
    include_tags: list[str] = field(default_factory=list)
    exclude_tags: list[str] = field(default_factory=list)
    # v0.2 PR 1b — mobile emulation + base64 image stripping
    # mobile=True switches both layers: L1 uses curl_cffi's iOS Safari
    # impersonate profile (UA + TLS fingerprint together — flipping UA
    # alone is itself a bot signal); L2 uses Playwright's "iPhone 13"
    # device descriptor.
    mobile: bool = False
    # v0.3 default flip: was False in v0.2 (byte-identical to v0.1).
    # Now True because base64 data: URIs explode token cost and cache
    # size while contributing nothing to LLM consumption. External <img>
    # tags survive into markdown. See v0.3-design.md §6.
    remove_base64_images: bool = True
    # v0.2 PR 5 — declarative browser actions. Non-empty forces L2 (browser).
    # Parse from JSON dicts via actions.parse_actions() before setting.
    actions: list = field(default_factory=list)
    # v0.3 PR 2 — local cache controls. See docs/v0.3/design.md §3 (flag
    # truth table) and §6 (FetchRequest changes). All four defaults match
    # v0.2 behaviour (no read, no write), so existing callers keep working
    # byte-identically until they opt into cache.
    #
    # max_age_ms: None  → cache lookup is skipped entirely (no read).
    #              int  → return cache record if its age ≤ max_age_ms,
    #                     else go to network. Always-skip is the default
    #                     because v0.2 callers never set a max age.
    # cache_only: True → cache_only mode. Never hit the network; on miss
    #              return ok=false / error_code=CACHE_MISS. Caller is
    #              expected to have set max_age_ms or to want any cache.
    # store_in_cache: True → on a successful response, persist to cache.
    #              Crawl / batch-fetch will flip this on by default.
    # no_cache: True → explicitly bypass cache entirely (no read, no
    #              write). Used by argparse to override defaults that
    #              crawl/batch-fetch enable. Mutually exclusive with the
    #              three flags above — enforced in cli.py argparse.
    max_age_ms: int | None = None
    cache_only: bool = False
    store_in_cache: bool = False
    no_cache: bool = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# -- Bug 9: binary file extensions that browsers download instead of render.
# An early reject avoids the "Download is starting" Playwright exception and
# the wasted L1→L2 escalation cycle.
_BINARY_EXTS = (
    ".zip", ".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".rar", ".7z",
    ".dmg", ".exe", ".msi", ".pkg", ".deb", ".rpm",
    ".docx", ".xlsx", ".pptx", ".odt", ".ods",
    ".mp3", ".mp4", ".mov", ".avi", ".mkv", ".webm",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
)


def _looks_like_binary_url(url: str) -> bool:
    """True if the URL's path ends with a known non-HTML extension.

    We inspect `parsed.path` only, which already excludes the query string and
    fragment — so `https://x.com/search?q=foo.pdf` is not rejected (its path
    is `/search`). Signed-URL params on real PDFs like
    `https://x.com/doc.pdf?Signature=…` are still rejected, because those
    paths really are PDFs regardless of query.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    path = parsed.path.lower()
    return any(path.endswith(ext) for ext in _BINARY_EXTS)


def _looks_like_pdf_url(url: str) -> bool:
    """True if the URL path ends with `.pdf` (case-insensitive).
    Query-string PDFs (`?file=foo.pdf`) are not matched — only path-based."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.path.lower().endswith(".pdf")


def _domain_hint_for(url: str) -> str | None:
    """Return a per-domain hint string (from `content.DOMAIN_HINTS`) suitable
    for surfacing in failure suggestions. Best-effort; None if no entry."""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return None
    if not host:
        return None
    hints = content_mod.DOMAIN_HINTS
    if host in hints:
        return hints[host]
    # Fall back to eTLD+1 match (e.g. m.reddit.com → reddit.com)
    parts = host.split(".")
    for i in range(1, len(parts) - 1):
        suffix = ".".join(parts[i:])
        if suffix in hints:
            return hints[suffix]
    return None


def _binary_url_suggestions(url: str) -> list[str]:
    return [
        "this URL points to a binary file the browser would download, not a "
        "renderable HTML page",
        f"download with shell tools instead, e.g. `curl -L -o file '{url}'`",
    ]


# Bug 11: empirically ~15-20% of L1 requests to docs.python.org / GitHub
# trending time out on the first attempt but succeed on a quick retry. A
# 4-second second attempt avoids the L2 browser-launch penalty (~10s) on the
# happy retry path, with a small worst-case cost (~4s) on truly broken sites.
_L1_RETRY_TIMEOUT_S = 4.0


async def _l1_with_one_retry(
    url: str,
    timeout_s: float,
    *,
    headers: dict[str, str] | None = None,
    impersonate: str = fetch_http.DEFAULT_IMPERSONATE,
) -> fetch_http.HttpResult:
    """Run L1 once; on `asyncio.TimeoutError` only, try one short retry.

    `FetchError`s (SSL/DNS/etc.) are deterministic — no point retrying.
    Re-raises `asyncio.TimeoutError` if the retry also times out, so callers
    can fall back to L2 the same way they did before this helper existed.
    """
    def _call(t: float) -> fetch_http.HttpResult:
        return fetch_http.fetch(url, timeout=t, headers=headers, impersonate=impersonate)

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_call, timeout_s),
            timeout=timeout_s + 1.0,
        )
    except asyncio.TimeoutError:
        retry_to = min(_L1_RETRY_TIMEOUT_S, timeout_s)
        return await asyncio.wait_for(
            asyncio.to_thread(_call, retry_to),
            timeout=retry_to + 1.0,
        )


# -- CF challenge detection (PR-4) -------------------------------------------------

# Signal strength levels:
#   Level 1 (assert): a single match on a Cloudflare-private token that never
#       appears in normal content. `/cdn-cgi/challenge-platform/` is CF's own
#       path; `cf-chl-bypass` / `cf_chl_opt` are internal CF challenge vars.
#   Level 2 (n≥2): individual words that can appear in legitimate discussions
#       of Cloudflare (e.g. a SO answer about CF) but together are a strong
#       indicator. Two or more hits → likely a real challenge page.
_CF_CHALLENGE_STRONG = (
    "/cdn-cgi/challenge-platform/",
    "cf-chl-bypass",
    "cf_chl_opt",
    "<title>just a moment...</title>",
    "performing security verification",
)
_CF_CHALLENGE_WEAK = (
    "checking your browser",
    "ddos protection by cloudflare",
    "attention required",
    "just a moment",
    "verifying you are human",
    "cf-mitigated",
    "this website uses a security service",
)


def _looks_like_challenge(text: str) -> bool:
    if not text:
        return True
    lo = text.lower()
    for kw in _CF_CHALLENGE_STRONG:
        if kw in lo:
            return True
    n = sum(1 for kw in _CF_CHALLENGE_WEAK if kw in lo)
    return n >= 2


# -- escalation heuristic (PR-3) ---------------------------------------------------

# Output formats that physically require the L2 browser. Centralised so a
# future feature that needs Playwright (`block_ads`, `actions`, …) just
# adds itself to `_l1_incapable` rather than scattering early-bypass
# branches through `Router.fetch`.
_SCREENSHOT_FORMATS = frozenset({"screenshot", "markdown+screenshot"})


def _l1_incapable(req: FetchRequest) -> bool:
    """True when the request asks for something the L1 (curl_cffi) layer
    physically cannot produce. Callers MUST run this AFTER
    `_looks_like_binary_url(req.url)` and `validate_url(req.url)` so the
    SSRF / binary-content guards are not bypassed by the presence of an
    L2-only field."""
    # PR 2: screenshot output formats require Playwright rendering
    # PR 5: declarative actions require Playwright DOM interaction
    # v0.3: `block_ads` will also require L2
    return req.output_format in _SCREENSHOT_FORMATS or bool(req.actions)


# PR 2.3 — ``cache_only=True`` with no explicit ``max_age_ms`` means
# "accept whatever's cached regardless of age". Pass this sentinel into
# Cache.lookup's ``max_age_ms`` so the ≤ check always passes; sys.maxsize
# would work too but a defined constant reads better at the call site.
_MAX_AGE_SENTINEL = 10**18  # ~31 billion years; effectively infinite


def _should_escalate_to_browser(http_status: int, html: str) -> bool:
    if http_status in (403, 429, 503):
        return True
    if _looks_like_challenge(html):
        return True
    if content_mod.detect_spa_shell(html):
        return True
    # Tiny body without recognizable content → likely needs JS
    if len(html) < 200 and "<" in html:
        return True
    # Large HTML with nearly zero visible text → SPA / login wall shell
    # (x.com ~75k html 0.5% text; www.reddit shell ~3k html 1% text).
    # Normal article pages are ≥4-5%.
    if len(html) > 2000 and content_mod.visible_text_ratio(html) < 0.02:
        return True
    return False


@dataclass
class Router:
    pool: fetch_browser.BrowserPool = field(default_factory=fetch_browser.BrowserPool)
    # v0.3 PR 2.3 — cache aspect. ``Cache(...)`` is constructed lazily so
    # tests that monkeypatch ``lightcrawl.paths.CACHE_ROOT`` before
    # building the Router see the patched root. Pass an explicit
    # ``cache=Cache(root=tmp)`` to skip the lazy default in tests.
    cache: Cache | None = None

    def _get_cache(self) -> Cache:
        if self.cache is None:
            self.cache = Cache()
        return self.cache

    async def close(self) -> None:
        await self.pool.close()

    # -- cache aspect (PR 2.3 — design §2 / §5.2) ----------------------------

    def _cache_lookup(self, req: FetchRequest) -> CacheHit | None:
        """Try the cache before any network work.

        Returns the hit, or ``None`` if the request explicitly opted out
        (``no_cache``) or simply didn't ask to read (no ``max_age_ms``
        and no ``cache_only``). The caller treats ``None`` as "go to
        network" — except when ``cache_only=True``, where a miss should
        surface as ``CACHE_MISS``; that branch lives in ``fetch()``.
        """
        if req.no_cache:
            return None
        if not req.cache_only and req.max_age_ms is None:
            return None
        # ``cache_only`` with no explicit max age means "use whatever's
        # cached regardless of staleness". Pass a sentinel max-age that
        # always passes the ≤ check; ``lookup`` will still touch
        # accessed_at, which is the right LRU signal because the caller
        # is committing to the cached body.
        max_age = req.max_age_ms if req.max_age_ms is not None else _MAX_AGE_SENTINEL
        return self._get_cache().lookup(req.url, profile=req.profile, max_age_ms=max_age)

    def _revalidation(self, req: FetchRequest) -> tuple[CacheHit | None, dict[str, str]]:
        """PR 3 — conditional-request setup for the L1 path.

        Reached only after ``_cache_lookup`` missed (stale or no max_age).
        When cache is *opted in* (a cache flag is present — the bare-fetch
        default is excluded so v0.2 behaviour is untouched) and a stored
        entry exposes an ETag / Last-Modified, return that entry plus the
        conditional headers to send. ``cache_only`` never reaches here (it
        either hit or already returned ``CACHE_MISS`` in ``fetch``).
        """
        if req.no_cache or req.cache_only:
            return None, {}
        if not req.store_in_cache and req.max_age_ms is None:
            return None, {}
        hit = self._get_cache().lookup_for_revalidation(req.url, profile=req.profile)
        if hit is None:
            return None, {}
        cond: dict[str, str] = {}
        etag = hit.headers.get("etag")
        last_modified = hit.headers.get("last-modified")
        if etag:
            cond["If-None-Match"] = etag
        if last_modified:
            cond["If-Modified-Since"] = last_modified
        if not cond:
            return None, {}
        return hit, cond

    def _cache_store_if_requested(self, req: FetchRequest, response: dict) -> dict:
        """Write to cache after a successful fetch if the request asked
        for it. Returns the response unchanged so call sites can chain.

        Failure to write is swallowed: the response goes out the door
        either way. Cache is a best-effort optimisation, not a hard
        dependency — surfacing a cache write error to the user would
        make the contract worse, not better.
        """
        if req.no_cache or not req.store_in_cache:
            return response
        if not response.get("ok"):
            return response
        # Never re-store a cache-sourced response (e.g. a 304-revalidated
        # body): it carries no top-level ``headers``, so storing it would
        # blank the entry's etag / last_modified columns.
        if response.get("cache_hit"):
            return response
        try:
            self._get_cache().store(req.url, profile=req.profile, response=response)
        except (OSError, sqlite3.Error):
            # Best-effort: a disk-full / WAL-contention / permission error
            # shouldn't fail an otherwise-successful fetch. Narrowed from
            # bare ``Exception`` so programming errors (TypeError from a
            # bad response shape, etc.) still surface. PR 2.5 adds a
            # multiprocess-WAL contention test as the main shake-out for
            # write reliability.
            pass
        return response

    async def fetch(self, req: FetchRequest) -> dict:
        # Bug 9: reject obvious binary URLs before any network call so the
        # caller gets a clear error code instead of an opaque
        # "Download is starting" Playwright exception.
        if _looks_like_binary_url(req.url):
            return _failure(
                req.url,
                ErrorCode.UNSUPPORTED_CONTENT_TYPE,
                "URL points to a binary file (archive/media/executable); "
                "the fetcher only handles HTML and PDF",
                attempts=[],
                suggestions=_binary_url_suggestions(req.url),
            )

        try:
            resolved = validate_url(req.url)
        except FetchError as e:
            return _failure(req.url, e.code, e.detail, attempts=[])

        attempts: list[Attempt] = []

        # PR 2.3 — cache entry hook. SSRF and binary checks above run
        # unconditionally; only after we know the URL is safe do we
        # consult cache. Profile checks (L3 path below) happen AFTER
        # cache because url_hash is profile-dimensional — a no-profile
        # caller can't accidentally read a profile-bound entry.
        cache_hit = self._cache_lookup(req)
        if cache_hit is not None:
            return _success_from_cache(req, cache_hit, attempts)
        if req.cache_only:
            return _failure(
                req.url, ErrorCode.CACHE_MISS,
                "cache_only=True and no entry within max_age_ms",
                attempts=[],
            )

        # PDF dispatch: detect .pdf path, download + extract text via pypdf.
        # Must come AFTER SSRF check above. L1-only in v0.2 (no L2 fallback).
        if _looks_like_pdf_url(req.url):
            return self._cache_store_if_requested(
                req, await self._fetch_pdf_route(req, attempts),
            )

        # L3 path: explicit profile
        if req.profile or req.strategy == "authed":
            return self._cache_store_if_requested(
                req, await self._fetch_authed(req, resolved.etld1, attempts),
            )

        # PR 2: physical L1 incapability (screenshot today; actions / block_ads
        # later). Must come AFTER `_looks_like_binary_url` + `validate_url`
        # above — SSRF and binary guards are unconditional. Honour an
        # explicit `strategy="http"` request as a deliberate override that
        # surfaces as a downstream UNSUPPORTED_CONTENT_TYPE / empty-screenshot
        # response rather than silently re-routing to L2.
        if _l1_incapable(req) and req.strategy != "http":
            return self._cache_store_if_requested(
                req, await self._fetch_browser_only(req, attempts, storage_state=None),
            )

        # Forced strategies
        if req.strategy == "http":
            if req.actions:
                return _failure(
                    req.url,
                    ErrorCode.UNSUPPORTED_CONTENT_TYPE,
                    "actions require a browser; incompatible with --strategy http. "
                    "Remove --strategy http or drop --actions.",
                    attempts,
                )
            return self._cache_store_if_requested(
                req, await self._fetch_http_only(req, attempts),
            )
        if req.strategy == "browser":
            return self._cache_store_if_requested(
                req, await self._fetch_browser_only(req, attempts, storage_state=None),
            )

        # Auto: try L1, escalate to L2 on signals
        l1_timeout = min(8.0, req.timeout_ms / 1000.0)
        # PR 3 — conditional request: if a stale-but-validatable entry exists,
        # send If-None-Match / If-Modified-Since (L1 only). Caller headers win
        # on collision, matching ``fetch_http.fetch``'s merge order.
        reval_hit, cond_headers = self._revalidation(req)
        merged_headers = {**cond_headers, **(req.headers or {})} or None
        try:
            r = await _l1_with_one_retry(
                req.url,
                l1_timeout,
                headers=merged_headers,
                impersonate=fetch_http.MOBILE_IMPERSONATE if req.mobile else fetch_http.DEFAULT_IMPERSONATE,
            )
        except asyncio.TimeoutError:
            attempts.append(Attempt("http", "timeout"))
            return self._cache_store_if_requested(
                req, await self._fetch_browser_only(req, attempts, storage_state=None),
            )
        except FetchError as e:
            attempts.append(Attempt("http", e.code.value.lower()))
            if e.code in (ErrorCode.HTTP_ERROR, ErrorCode.TIMEOUT):
                return self._cache_store_if_requested(
                    req, await self._fetch_browser_only(req, attempts, storage_state=None),
                )
            return _failure(req.url, e.code, e.detail, attempts)

        # 304 Not Modified: the cached body is confirmed current. Refresh the
        # entry's freshness and serve the cached copy — no re-extraction, no
        # L2 escalation. (Defensive: only when we actually hold the entry.)
        if r.status_code == 304 and reval_hit is not None:
            # _success_from_cache records the Attempt("http", "304") itself.
            try:
                revalidated_at = self._get_cache().mark_revalidated(req.url, profile=req.profile)
            except (OSError, sqlite3.Error):
                revalidated_at = None
            return _success_from_cache(
                req, reval_hit, attempts,
                revalidated=True, revalidated_at_ms=revalidated_at,
            )

        # 304 with nothing in hand to serve. Our own validators are sent only
        # when we hold the entry, so this means a caller-supplied conditional
        # header (``--header If-None-Match=...``) drew a 304 we can't satisfy.
        # Surface it rather than fall through — the empty body would otherwise
        # trip ``_should_escalate_to_browser`` into a pointless L2 launch.
        if r.status_code == 304:
            attempts.append(Attempt("http", "304"))
            return _failure(
                req.url, ErrorCode.HTTP_ERROR,
                "304 Not Modified but no cached entry to serve "
                "(conditional header sent without a stored validator)",
                attempts, status_code=304,
            )

        attempts.append(Attempt("http", str(r.status_code)))

        # Login wall detected without profile → stop, don't escalate
        extracted = content_mod.html_to_markdown(
            r.text,
            selector=req.selector,
            url=req.url,
            include_tags=req.include_tags,
            exclude_tags=req.exclude_tags,
            remove_base64_images=req.remove_base64_images,
        )
        if extracted.looks_like_login_wall and r.status_code in (200, 401):
            return _login_required(req.url, attempts)

        if _should_escalate_to_browser(r.status_code, r.text):
            return self._cache_store_if_requested(
                req,
                await self._fetch_browser_only(req, attempts, storage_state=None, prior=r),
            )

        return self._cache_store_if_requested(
            req, _success_from_http(req, r, extracted, attempts, "http"),
        )

    # -- helpers --

    async def _fetch_http_only(self, req: FetchRequest, attempts: list[Attempt]) -> dict:
        l1_timeout = min(8.0, req.timeout_ms / 1000.0)
        # PR 3 — same conditional-request path as the auto branch (L1 only).
        reval_hit, cond_headers = self._revalidation(req)
        merged_headers = {**cond_headers, **(req.headers or {})} or None
        try:
            r = await _l1_with_one_retry(
                req.url,
                l1_timeout,
                headers=merged_headers,
                impersonate=fetch_http.MOBILE_IMPERSONATE if req.mobile else fetch_http.DEFAULT_IMPERSONATE,
            )
        except asyncio.TimeoutError:
            attempts.append(Attempt("http", "timeout"))
            return _failure(req.url, ErrorCode.TIMEOUT, "L1 timed out", attempts)
        except FetchError as e:
            attempts.append(Attempt("http", e.code.value.lower()))
            return _failure(req.url, e.code, e.detail, attempts)
        if r.status_code == 304 and reval_hit is not None:
            try:
                revalidated_at = self._get_cache().mark_revalidated(req.url, profile=req.profile)
            except (OSError, sqlite3.Error):
                revalidated_at = None
            return _success_from_cache(
                req, reval_hit, attempts,
                revalidated=True, revalidated_at_ms=revalidated_at,
            )
        # Orphan 304 (caller-supplied conditional header, nothing cached to
        # serve): surface it instead of returning an empty-bodied success.
        if r.status_code == 304:
            attempts.append(Attempt("http", "304"))
            return _failure(
                req.url, ErrorCode.HTTP_ERROR,
                "304 Not Modified but no cached entry to serve "
                "(conditional header sent without a stored validator)",
                attempts, status_code=304,
            )
        attempts.append(Attempt("http", str(r.status_code)))
        extracted = content_mod.html_to_markdown(
            r.text,
            selector=req.selector,
            url=req.url,
            include_tags=req.include_tags,
            exclude_tags=req.exclude_tags,
            remove_base64_images=req.remove_base64_images,
        )
        return _success_from_http(req, r, extracted, attempts, "http")

    async def _fetch_browser_only(
        self,
        req: FetchRequest,
        attempts: list[Attempt],
        *,
        storage_state: str | dict | None,
        prior: fetch_http.HttpResult | None = None,
    ) -> dict:
        wf = fetch_browser.WaitFor(
            selector=(req.wait_for.selector if req.wait_for else None),
            network_idle=(req.wait_for.network_idle if req.wait_for else False),
            timeout_ms=(req.wait_for.timeout_ms if req.wait_for else 10_000),
        )
        try:
            r = await asyncio.wait_for(
                fetch_browser.fetch(
                    self.pool,
                    req.url,
                    wait_for=wf,
                    timeout=min(15.0, req.timeout_ms / 1000.0),
                    storage_state=storage_state,
                    headers=req.headers or None,
                    mobile=req.mobile,
                    screenshot=req.output_format in _SCREENSHOT_FORMATS,
                    actions=req.actions,
                ),
                timeout=req.timeout_ms / 1000.0,
            )
        except asyncio.TimeoutError:
            attempts.append(Attempt("browser", "timeout"))
            return _failure(req.url, ErrorCode.TIMEOUT, "L2 timed out", attempts)
        except FetchError as e:
            attempts.append(Attempt("browser", e.code.value.lower()))
            suggestions: list[str] = []
            # Bug 8: when the L2 surfaces SPA_NAVIGATION_LOOP, look up a
            # per-domain hint (e.g. "use old.reddit.com") so the agent has a
            # concrete next action instead of just an opaque error code.
            if e.code == ErrorCode.SPA_NAVIGATION_LOOP:
                hint = _domain_hint_for(req.url)
                if hint:
                    suggestions.append(hint)
            return _failure(req.url, e.code, e.detail, attempts, suggestions=suggestions)

        attempts.append(Attempt("browser", str(r.status_code)))
        if _looks_like_challenge(r.text) or r.status_code in (403, 429, 503):
            return _failure(
                req.url,
                ErrorCode.BLOCKED_BY_CLOUDFLARE,
                f"L2 stealth still blocked, last status {r.status_code}",
                attempts,
                suggestions=_blocked_suggestions(req.url, has_profile=storage_state is not None),
                final_url=r.final_url,
                status_code=r.status_code,
            )

        extracted = content_mod.html_to_markdown(
            r.text,
            selector=req.selector,
            url=req.url,
            include_tags=req.include_tags,
            exclude_tags=req.exclude_tags,
            remove_base64_images=req.remove_base64_images,
        )
        if extracted.looks_like_login_wall and storage_state is None:
            return _login_required(req.url, attempts)

        return _success_from_browser(req, r, extracted, attempts, "browser" if storage_state is None else "authed")

    async def _fetch_authed(
        self, req: FetchRequest, target_etld1: str, attempts: list[Attempt]
    ) -> dict:
        if not req.profile:
            return _failure(
                req.url,
                ErrorCode.URL_NOT_ALLOWED,
                "strategy=authed requires `profile` parameter",
                attempts,
            )
        try:
            meta = auth.get_profile(req.profile)
        except FetchError as e:
            return _failure(req.url, e.code, e.detail, attempts)

        if meta.status != "active":
            return _failure(
                req.url,
                ErrorCode.SESSION_EXPIRED,
                f"profile {req.profile!r} status={meta.status}",
                attempts,
                suggestions=[
                    f"re-login: `lightcrawl auth login {req.profile} <login URL>`",
                ],
            )

        if not domain_matches(req.url, meta.bound_domain):
            return _failure(
                req.url,
                ErrorCode.PROFILE_DOMAIN_MISMATCH,
                f"profile {req.profile!r} bound to {meta.bound_domain}, "
                f"target URL is on a different domain",
                attempts,
            )

        state = auth.load_storage_state(req.profile)
        result = await self._fetch_browser_only(req, attempts, storage_state=state)

        # Heuristic re-validation: if response itself looks like a login wall, mark expired
        if not result["ok"] and result["error_code"] == ErrorCode.LOGIN_REQUIRED.value:
            auth.update_profile_status(
                req.profile, status="expired", expired_reason="response_was_login_wall"
            )
            return _failure(
                req.url,
                ErrorCode.SESSION_EXPIRED,
                f"profile {req.profile!r} no longer valid (server returned login wall)",
                attempts,
                suggestions=[
                    f"re-login: `lightcrawl auth login {req.profile} <login URL>`",
                ],
            )

        if result["ok"]:
            auth.update_profile_status(req.profile, status="active")
        return result

    async def _fetch_pdf_route(self, req: FetchRequest, attempts: list[Attempt]) -> dict:
        """Download PDF via curl_cffi and extract text with pypdf. L1-only."""
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    fetch_pdf.fetch_pdf,
                    req.url,
                    timeout=min(15.0, req.timeout_ms / 1000.0),
                    headers=req.headers or None,
                ),
                timeout=req.timeout_ms / 1000.0,
            )
        except asyncio.TimeoutError:
            return _failure(req.url, ErrorCode.TIMEOUT, "PDF download timed out", attempts)
        except FetchError as e:
            attempts.append(Attempt("pdf", e.code.value.lower()))
            # Specific guidance for the SSE / 10jqka / Sina pattern where a
            # .pdf URL redirects to an HTML download landing page. The L1-only
            # PDF route can't follow that intermediary, but L2 can render it
            # so the user (or an agent) can extract the real PDF link. Only
            # attach when the failure mode is content-type mismatch — for
            # PDF_NO_TEXT_LAYER / TIMEOUT / PDF_FETCH_BLOCKED the browser
            # strategy wouldn't help. (#40)
            suggestions: list[str] = []
            if e.code == ErrorCode.UNSUPPORTED_CONTENT_TYPE:
                suggestions.append(
                    "this URL was served as HTML, not PDF — likely a "
                    "download intermediary page. Retry with "
                    "`--strategy browser` to render it and grab the real "
                    "PDF link (or screenshot the inline body)."
                )
            return _failure(req.url, e.code, e.detail, attempts, suggestions=suggestions)

        attempts.append(Attempt("pdf", "200"))
        body = result.markdown
        full_content_hash = hashlib.sha1(body.encode("utf-8")).hexdigest()
        inline, truncated, dump_path = content_mod.maybe_dump(req.url, body, req.max_inline_tokens, content_hash=full_content_hash)
        return {
            "ok": True,
            "url": req.url,
            "final_url": result.final_url,
            "strategy_used": "pdf",
            "fetched_at": _now_iso(),
            "title": result.title,
            "content": inline,
            "content_truncated": truncated,
            "dump_path": dump_path,
            "full_content_hash": full_content_hash,
            "metadata": {
                "status_code": 200,
                "content_type": "application/pdf",
                "elapsed_ms": result.elapsed_ms,
                "needs_js_hint": None,
                "suggested_selectors": [],
                "selector_hint": None,
                "links": [],
                "images": [],
                "num_pages": result.num_pages,
                "content_length": result.content_length,
            },
            "attempts": [a.to_dict() for a in attempts],
            "headings": [],
        }


# -- response shapers --


def _success_from_cache(
    req: FetchRequest, hit: CacheHit, attempts: list[Attempt],
    *, revalidated: bool = False, revalidated_at_ms: int | None = None,
) -> dict:
    """Build the same response envelope as a fresh fetch from a CacheHit.

    ``strategy_used="cache"`` and ``cache_hit=True`` give callers a clean
    way to tell the response apart from a live fetch — useful for crawl
    progress counters (design §5.5 ``pages_skipped_cache``) and for
    agents that want to redo a fetch with ``--no-cache`` when they need
    fresh content. Attempts list is updated so the response carries the
    same provenance shape as the live paths.
    """
    # A 304 revalidation reuses the body but the entry was just refreshed,
    # so its effective age is 0; flag it so callers can tell a 304-confirmed
    # hit from a plain age-based one (design §5.2 / acceptance §10.2b).
    cache_attempt = Attempt("http", "304") if revalidated else Attempt("cache", "hit")
    result: dict = {
        "ok": True,
        "url": req.url,
        "final_url": hit.final_url or req.url,
        "strategy_used": "cache",
        "cache_hit": True,
        "revalidated": revalidated,
        "cache_age_ms": 0 if revalidated else hit.age_ms,
        # On a 304 ``mark_revalidated`` reset ``fetched_at`` to now, so report
        # that refreshed value — emitting the stale pre-revalidation stamp
        # would contradict ``cache_age_ms == 0``. (Falls back to the old stamp
        # only if the freshness write was skipped.)
        "cache_fetched_at_ms": (
            revalidated_at_ms if revalidated and revalidated_at_ms is not None
            else hit.fetched_at_ms
        ),
        "fetched_at": _now_iso(),
        "title": hit.title,
        "content": hit.markdown,
        "content_truncated": hit.content_truncated,
        "dump_path": hit.dump_path,
        "full_content_hash": hit.content_hash,
        "metadata": hit.metadata,
        "attempts": [a.to_dict() for a in attempts] + [cache_attempt.to_dict()],
        "headings": hit.headings,
    }
    # A `--screenshot` fetch stores only the final image path; replay it
    # in the same `screenshots` list shape `_success_from_browser` emits
    # so a cache hit doesn't silently drop the screenshot. Intermediate
    # action screenshots aren't cached, so only the `final` stage appears.
    if hit.screenshot_path:
        result["screenshots"] = [{"stage": "final", "path": hit.screenshot_path}]
    return result


def _success_from_http(
    req: FetchRequest,
    r: fetch_http.HttpResult,
    extracted: content_mod.ExtractedContent,
    attempts: list[Attempt],
    strategy_used: str,
) -> dict:
    body = _format_body(req.output_format, extracted, r.text)
    full_content_hash = hashlib.sha1(body.encode("utf-8")).hexdigest()
    inline, truncated, dump_path = content_mod.maybe_dump(req.url, body, req.max_inline_tokens, content_hash=full_content_hash)
    # PR 3 — surface the response validators top-level so ``Cache.store``
    # persists them (it reads ``response["headers"]``) and the next fetch can
    # send a conditional GET. Lower-cased keys match what ``store`` reads.
    # Only emit ``headers`` when a validator is actually present. Pages
    # that expose ETag / Last-Modified carry the key so ``Cache.store``
    # persists them for the next conditional request.
    headers: dict[str, str] = {}
    if r.etag:
        headers["etag"] = r.etag
    if r.last_modified:
        headers["last-modified"] = r.last_modified
    result = {
        "ok": True,
        "url": req.url,
        "final_url": r.final_url,
        "strategy_used": strategy_used,
        "fetched_at": _now_iso(),
        "title": extracted.title,
        "content": inline,
        "content_truncated": truncated,
        "dump_path": dump_path,
        "full_content_hash": full_content_hash,
        "metadata": {
            "status_code": r.status_code,
            "content_type": r.content_type,
            "elapsed_ms": r.elapsed_ms,
            "needs_js_hint": extracted.needs_js_hint,
            "suggested_selectors": extracted.suggested_selectors,
            "selector_hint": extracted.selector_hint,
            "links": extracted.links,
            "images": extracted.images,
        },
        "attempts": [a.to_dict() for a in attempts],
        "headings": [{"level": h.level, "text": h.text, "line": h.line} for h in extracted.headings],
    }
    if headers:
        result["headers"] = headers
    return result


def _success_from_browser(
    req: FetchRequest,
    r: fetch_browser.BrowserResult,
    extracted: content_mod.ExtractedContent,
    attempts: list[Attempt],
    strategy_used: str,
) -> dict:
    body = _format_body(req.output_format, extracted, r.text)
    full_content_hash = hashlib.sha1(body.encode("utf-8")).hexdigest()
    inline, truncated, dump_path = content_mod.maybe_dump(req.url, body, req.max_inline_tokens, content_hash=full_content_hash)
    result: dict = {
        "ok": True,
        "url": req.url,
        "final_url": r.final_url,
        "strategy_used": strategy_used,
        "fetched_at": _now_iso(),
        "title": extracted.title,
        "content": inline,
        "content_truncated": truncated,
        "dump_path": dump_path,
        "full_content_hash": full_content_hash,
        "metadata": {
            "status_code": r.status_code,
            "content_type": r.content_type,
            "elapsed_ms": r.elapsed_ms,
            "needs_js_hint": extracted.needs_js_hint,
            "suggested_selectors": extracted.suggested_selectors,
            "selector_hint": extracted.selector_hint,
            "links": extracted.links,
            "images": extracted.images,
        },
        "attempts": [a.to_dict() for a in attempts],
        "headings": [{"level": h.level, "text": h.text, "line": h.line} for h in extracted.headings],
    }
    # PR 2: surface screenshots only when present, keeping the v0.1 / PR 1a
    # / PR 1b default-call key set byte-identical. PR 5 extends this same
    # array with intermediate `{stage: "action", index, label, path}`
    # entries — that's why it's a list of objects, not a flat path string.
    shots: list[dict] = []
    # PR 5: intermediate screenshots come first, in action-execution order.
    if r.action_screenshots:
        for shot in r.action_screenshots:
            path = _write_action_screenshot(req.url, shot["index"], shot["png_bytes"])
            shots.append({
                "stage": "action",
                "index": shot["index"],
                "label": shot["label"],
                "path": path,
            })
    if r.screenshot_png is not None:
        shots.append({
            "stage": "final", "path": _write_screenshot(req.url, r.screenshot_png)
        })
    if shots:
        result["screenshots"] = shots
    return result


def _format_body(fmt: str, extracted: content_mod.ExtractedContent, raw_html: str) -> str:
    if fmt == "html":
        return raw_html
    if fmt == "text":
        return extracted.plain_text
    if fmt == "screenshot":
        # PR 2: caller asked for the PNG only. The image bytes go in the
        # `screenshots` array, the text body is intentionally empty.
        return ""
    if fmt == "links":
        return json.dumps(extracted.links, ensure_ascii=False)
    if fmt == "images":
        return json.dumps(extracted.images, ensure_ascii=False)
    # "markdown" or "markdown+screenshot" — both yield markdown text
    return extracted.markdown


def _write_screenshot(url: str, png: bytes) -> str:
    """Persist a screenshot under `~/.lightcrawl/screenshots/{sha1(url)}.png`
    and return its absolute path.

    Overwrite semantics — same URL re-screenshotted clobbers the prior
    file. PR 2 doesn't ship GC; that's deferred to v0.3's cache layer
    which will fold this directory into the same TTL/LRU policy as the
    markdown cache. Until then SKILL.md / README must document
    "two fetches of the same URL clobber each other's screenshot."
    """
    from .paths import SCREENSHOTS
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    path = SCREENSHOTS / f"{digest}.png"
    path.write_bytes(png)
    return str(path)


def _write_action_screenshot(url: str, index: int, png: bytes) -> str:
    """Persist an intermediate ScreenshotAction PNG.
    Naming: SCREENSHOTS/{sha1(url)[:16]}_act{index}.png
    Index is the action's position in the `actions` list — gaps mean
    non-screenshot actions occupy that slot."""
    from .paths import SCREENSHOTS
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    path = SCREENSHOTS / f"{digest}_act{index}.png"
    path.write_bytes(png)
    return str(path)


def _failure(
    url: str,
    code: ErrorCode,
    detail: str,
    attempts: list[Attempt],
    suggestions: list[str] | None = None,
    *,
    final_url: str | None = None,
    status_code: int | None = None,
) -> dict:
    """Failure response. Intentionally omits content-only fields
    (`full_content_hash`, `headings` content) that have no meaning when the
    fetch did not produce a body."""
    strategy_used = attempts[-1].strategy if attempts else None
    return {
        "ok": False,
        "url": url,
        "final_url": final_url,
        "strategy_used": strategy_used,
        "fetched_at": _now_iso(),
        "title": "",
        "content": "",
        "content_truncated": False,
        "dump_path": None,
        "metadata": {
            "status_code": status_code,
            "content_type": None,
            "elapsed_ms": None,
            "needs_js_hint": None,
            "suggested_selectors": [],
            "selector_hint": None,
            "links": [],
            "images": [],
        },
        "error_code": code.value,
        "error_detail": detail,
        "attempts": [a.to_dict() for a in attempts],
        "suggestions": suggestions or [],
        "headings": [],
    }


def _login_required(url: str, attempts: list[Attempt]) -> dict:
    return _failure(
        url,
        ErrorCode.LOGIN_REQUIRED,
        "the page requires login; no profile was supplied",
        attempts,
        suggestions=[
            "create a profile: `lightcrawl auth login <short-site-name> <login URL>`",
            "if you already have a profile, retry with `lightcrawl fetch <url> --profile <name>`",
        ],
    )


def _blocked_suggestions(url: str, *, has_profile: bool) -> list[str]:
    out = []
    if not has_profile:
        out.append(
            "the site may need login: `lightcrawl auth login <name> <login URL>` "
            "then `lightcrawl fetch <url> --profile <name>`"
        )
    out.append(f"try archive: `lightcrawl fetch https://web.archive.org/web/{url}`")
    return out
