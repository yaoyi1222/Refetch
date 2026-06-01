"""Conditional-request (ETag / Last-Modified) tests — v0.3 PR 3.

Offline coverage of the 304 revalidation path on the L1 (curl_cffi) route.
Same monkeypatch house style as ``tests/test_router_cache.py``: the cache is
pinned to ``tmp_path`` and ``fetch_http.fetch`` is replaced by a canned origin
stub. The stub *inspects the incoming headers* so it can answer a conditional
GET with a real 304 (empty body) — that's how we exercise revalidation without
a network.

See ``docs/superpowers/specs/2026-06-01-conditional-requests-design.md``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from lightcrawl import cache as cache_mod
from lightcrawl.cache import Cache
from lightcrawl.fetch_http import HttpResult
from lightcrawl.router import FetchRequest, Router


@pytest.fixture
def fake_clock(monkeypatch):
    clock = [1_000_000]
    monkeypatch.setattr(cache_mod, "time_ms", lambda: clock[0])
    return clock


@pytest.fixture
def router(tmp_path, monkeypatch) -> Router:
    monkeypatch.setattr("lightcrawl.paths.ROOT", tmp_path)
    monkeypatch.setattr("lightcrawl.paths.DUMPS", tmp_path / "dumps")
    monkeypatch.setattr("lightcrawl.paths.PROFILES", tmp_path / "profiles")
    monkeypatch.setattr("lightcrawl.paths.LOGS", tmp_path / "logs")
    monkeypatch.setattr("lightcrawl.content.DUMPS", tmp_path / "dumps")
    monkeypatch.setattr("lightcrawl.auth.PROFILES", tmp_path / "profiles")
    (tmp_path / "dumps").mkdir(parents=True, exist_ok=True)
    (tmp_path / "profiles").mkdir(parents=True, exist_ok=True)
    return Router(cache=Cache(root=tmp_path / "cache"))


def _html(marker: str) -> str:
    return (
        f"<html><head><title>{marker}</title></head><body>"
        f"<article><h1>{marker}</h1>"
        "<p>body text long enough to extract properly with readability "
        "and then markdownify it nicely. The router's escalation gate "
        "treats html shorter than 200 bytes as a JS shell, so we pad here "
        "to comfortably exceed that threshold and stay on the L1 path.</p>"
        "</article></body></html>"
    )


_BODY_V1 = _html("V1")
_BODY_V2 = _html("V2")
_ETAG = '"abc-123"'
_LASTMOD = "Wed, 21 Oct 2025 07:28:00 GMT"


class Origin:
    """Canned origin: records every call's headers and answers conditional
    GETs with 304. ``validators`` controls which response headers it sends."""

    def __init__(self, *, body=_BODY_V1, etag=_ETAG, last_modified=_LASTMOD):
        self.body = body
        self.etag = etag
        self.last_modified = last_modified
        self.calls: list[dict] = []

    def __call__(self, url, *, timeout=5.0, headers=None, impersonate="x"):
        headers = headers or {}
        self.calls.append(dict(headers))
        conditional = (
            (self.etag and headers.get("If-None-Match") == self.etag)
            or (self.last_modified and headers.get("If-Modified-Since") == self.last_modified)
        )
        if conditional:
            return HttpResult(
                final_url=url, status_code=304, text="",
                content_type="text/html", elapsed_ms=2,
                etag=self.etag, last_modified=self.last_modified,
            )
        return HttpResult(
            final_url=url, status_code=200, text=self.body,
            content_type="text/html", elapsed_ms=10,
            etag=self.etag, last_modified=self.last_modified,
        )

    @property
    def last_headers(self) -> dict:
        return self.calls[-1]


def _patches(origin: Origin):
    # Two context managers the tests enter together.
    return (
        patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"),
        patch("lightcrawl.fetch_http.fetch", side_effect=origin),
    )


async def _fetch(router: Router, origin: Origin, req: FetchRequest) -> dict:
    dns, http = _patches(origin)
    with dns, http:
        return await router.fetch(req)


_HOUR = 3_600_000


# 1 — a cache-on fetch persists the validators for next time.
async def test_store_captures_validators(router: Router, fake_clock):
    origin = Origin()
    await _fetch(router, origin, FetchRequest(
        url="https://example.com/", max_age_ms=_HOUR, store_in_cache=True,
    ))
    hit = router._get_cache().lookup_for_revalidation("https://example.com/", profile=None)
    assert hit is not None
    assert hit.headers.get("etag") == _ETAG
    assert hit.headers.get("last-modified") == _LASTMOD


# 2 — stale entry → conditional GET → 304 → cached body reused.
async def test_stale_entry_revalidates_304(router: Router, fake_clock):
    origin = Origin()
    await _fetch(router, origin, FetchRequest(
        url="https://example.com/", max_age_ms=_HOUR, store_in_cache=True,
    ))
    fake_clock[0] += 2 * _HOUR  # entry is now stale for a 1h max_age

    out = await _fetch(router, origin, FetchRequest(
        url="https://example.com/", max_age_ms=_HOUR, store_in_cache=True,
    ))
    assert out["ok"] is True
    assert out["cache_hit"] is True
    assert out.get("revalidated") is True
    assert "V1" in out["content"]
    # second call carried the conditional header and the origin 304'd
    assert origin.last_headers.get("If-None-Match") == _ETAG


# 3 — after a 304, the entry is fresh again for a max_age lookup.
async def test_304_refreshes_freshness(router: Router, fake_clock):
    origin = Origin()
    await _fetch(router, origin, FetchRequest(
        url="https://example.com/", max_age_ms=_HOUR, store_in_cache=True,
    ))
    fake_clock[0] += 2 * _HOUR
    await _fetch(router, origin, FetchRequest(  # 304, refreshes fetched_at
        url="https://example.com/", max_age_ms=_HOUR, store_in_cache=True,
    ))
    calls_before = len(origin.calls)
    # immediate re-fetch within max_age → plain cache hit, no network
    out = await _fetch(router, origin, FetchRequest(
        url="https://example.com/", max_age_ms=_HOUR, store_in_cache=True,
    ))
    assert out["cache_hit"] is True
    assert out.get("revalidated") is not True  # age-based hit, not a 304
    assert len(origin.calls) == calls_before  # no extra network call


# 4 — origin returns a fresh 200 → overwrite, live fetch (not cache hit).
async def test_changed_resource_overwrites(router: Router, fake_clock):
    origin = Origin()
    await _fetch(router, origin, FetchRequest(
        url="https://example.com/", max_age_ms=_HOUR, store_in_cache=True,
    ))
    fake_clock[0] += 2 * _HOUR
    # origin changed: new body + new validators, so neither the old
    # If-None-Match nor the old If-Modified-Since matches → 200.
    origin.body = _BODY_V2
    origin.etag = '"def-456"'
    origin.last_modified = "Thu, 22 Oct 2026 07:28:00 GMT"

    out = await _fetch(router, origin, FetchRequest(
        url="https://example.com/", max_age_ms=_HOUR, store_in_cache=True,
    ))
    assert out.get("cache_hit") is not True
    assert "V2" in out["content"]
    hit = router._get_cache().lookup_for_revalidation("https://example.com/", profile=None)
    assert hit.headers.get("etag") == '"def-456"'


# 5 — an entry without validators sends a plain GET (no conditional header).
async def test_no_validator_no_conditional(router: Router, fake_clock):
    origin = Origin(etag=None, last_modified=None)
    await _fetch(router, origin, FetchRequest(
        url="https://example.com/", max_age_ms=_HOUR, store_in_cache=True,
    ))
    fake_clock[0] += 2 * _HOUR
    await _fetch(router, origin, FetchRequest(
        url="https://example.com/", max_age_ms=_HOUR, store_in_cache=True,
    ))
    assert "If-None-Match" not in origin.last_headers
    assert "If-Modified-Since" not in origin.last_headers


# 6 — the bare-fetch default never revalidates (v0.2 regression guard).
async def test_bare_fetch_no_conditional(router: Router, fake_clock):
    origin = Origin()
    # prime the cache with a cache-on fetch
    await _fetch(router, origin, FetchRequest(
        url="https://example.com/", max_age_ms=_HOUR, store_in_cache=True,
    ))
    # now a default fetch (no cache flags) must not read/revalidate
    out = await _fetch(router, origin, FetchRequest(url="https://example.com/"))
    assert out.get("cache_hit") is not True
    assert "If-None-Match" not in origin.last_headers


# 7 — explicit no_cache bypasses revalidation entirely.
async def test_no_cache_skips_revalidation(router: Router, fake_clock):
    origin = Origin()
    await _fetch(router, origin, FetchRequest(
        url="https://example.com/", max_age_ms=_HOUR, store_in_cache=True,
    ))
    fake_clock[0] += 2 * _HOUR
    out = await _fetch(router, origin, FetchRequest(
        url="https://example.com/", no_cache=True,
    ))
    assert out.get("cache_hit") is not True
    assert "If-None-Match" not in origin.last_headers


# 8 — Last-Modified-only entry uses If-Modified-Since.
async def test_last_modified_only(router: Router, fake_clock):
    origin = Origin(etag=None)  # only Last-Modified
    await _fetch(router, origin, FetchRequest(
        url="https://example.com/", max_age_ms=_HOUR, store_in_cache=True,
    ))
    fake_clock[0] += 2 * _HOUR
    out = await _fetch(router, origin, FetchRequest(
        url="https://example.com/", max_age_ms=_HOUR, store_in_cache=True,
    ))
    assert out.get("revalidated") is True
    assert origin.last_headers.get("If-Modified-Since") == _LASTMOD
    assert "If-None-Match" not in origin.last_headers


# 9 — HttpResult carries the validator fields.
def test_httpresult_has_validator_fields():
    r = HttpResult(
        final_url="https://x/", status_code=200, text="", content_type="",
        elapsed_ms=0, etag='"e"', last_modified="lm",
    )
    assert r.etag == '"e"'
    assert r.last_modified == "lm"
