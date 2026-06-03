from __future__ import annotations

from unittest.mock import patch

import pytest

from lightcrawl import auth as auth_mod
from lightcrawl.router import Router
from lightcrawl.search.backends.base import BackendError
from lightcrawl.search.service import (
    SearchAndReadRequest,
    SearchRequest,
    SearchService,
)
from lightcrawl.search.types import SearchResult


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr("lightcrawl.paths.ROOT", tmp_path)
    monkeypatch.setattr("lightcrawl.paths.DUMPS", tmp_path / "dumps")
    monkeypatch.setattr("lightcrawl.paths.PROFILES", tmp_path / "profiles")
    monkeypatch.setattr("lightcrawl.paths.LOGS", tmp_path / "logs")
    monkeypatch.setattr("lightcrawl.content.DUMPS", tmp_path / "dumps")
    monkeypatch.setattr("lightcrawl.search.service.DUMPS", tmp_path / "dumps")
    monkeypatch.setattr("lightcrawl.auth.PROFILES", tmp_path / "profiles")
    (tmp_path / "dumps").mkdir(parents=True)
    (tmp_path / "profiles").mkdir(parents=True)


class FakeBackend:
    cost_per_call_usd = 0.001

    def __init__(self, results=None, raise_=None, configured=True, name="fake"):
        self._results = results or []
        self._raise = raise_
        self._configured = configured
        self.name = name
        self.calls = 0

    def configured(self):
        return self._configured

    async def search(self, query, *, max_results, time_range=(None, None), timeout=10.0):
        self.calls += 1
        if self._raise:
            raise self._raise
        return self._results[:max_results]


def _svc(*backends, router: Router | None = None) -> SearchService:
    return SearchService(router=router or Router(), backends=list(backends))


async def test_search_no_backend_configured():
    svc = _svc(FakeBackend(configured=False))
    out = await svc.search(SearchRequest(query="x"))
    assert out["ok"] is False
    assert out["error_code"] == "NO_BACKEND_CONFIGURED"
    await svc.close()


async def test_search_empty_results():
    svc = _svc(FakeBackend(results=[]))
    out = await svc.search(SearchRequest(query="x"))
    assert out["ok"] is False
    assert out["error_code"] == "EMPTY_RESULTS"
    await svc.close()


async def test_search_rate_limited_passes_through():
    svc = _svc(FakeBackend(raise_=BackendError("RATE_LIMITED", "fake: 429")))
    out = await svc.search(SearchRequest(query="x"))
    assert out["ok"] is False
    assert out["error_code"] == "RATE_LIMITED"
    assert any("retry" in s for s in out["suggestions"])
    await svc.close()


async def test_search_returns_annotated_results():
    fake = FakeBackend(results=[
        SearchResult(rank=1, title="A", url="https://a.example/1",
                     snippet="snippet A " * 30, page_age_days=3),
        SearchResult(rank=2, title="B", url="https://b.example/2",
                     snippet="short", page_age_days=None),
    ])
    svc = _svc(fake)
    out = await svc.search(SearchRequest(query="x", depth="quick"))
    assert out["ok"] is True
    assert out["backend_used"] == "fake"
    assert len(out["results"]) == 2
    r0 = out["results"][0]
    assert r0["fetch_hint"] == {"needs_login": False, "cache_status": "cold"}
    assert "page_age_days" in r0
    await svc.close()


async def test_snippet_enhancement_from_dump(tmp_path):
    import hashlib
    url = "https://a.example/1"
    digest = hashlib.sha1(url.encode()).hexdigest()[:16]
    (tmp_path / "dumps" / f"{digest}.md").write_text(
        "First paragraph with substantial content describing the article.\n"
        "It is more than two hundred characters long so the enhancer can\n"
        "use it to extend the original short snippet returned by the backend.\n"
        "This is the body."
    )
    fake = FakeBackend(results=[
        SearchResult(rank=1, title="A", url=url, snippet="too short")
    ])
    svc = _svc(fake)
    out = await svc.search(SearchRequest(query="x"))
    assert out["results"][0]["fetch_hint"]["cache_status"] == "warm"
    assert len(out["results"][0]["snippet"]) > 50
    await svc.close()


async def test_needs_login_hint_when_profile_active(tmp_path):
    auth_mod.save_profile("twitter", {"cookies": [], "origins": []}, "x.com")
    fake = FakeBackend(results=[
        SearchResult(rank=1, title="T", url="https://x.com/foo/status/1", snippet="ok")
    ])
    svc = _svc(fake)
    out = await svc.search(SearchRequest(query="x"))
    assert out["results"][0]["fetch_hint"]["needs_login"] is True
    await svc.close()


async def test_list_backends_reports_configuration():
    svc = _svc(FakeBackend(configured=False), FakeBackend(configured=True))
    backends = svc.list_backends()
    # Both have name="fake"; we just want shape correctness here.
    assert all("name" in b and "configured" in b for b in backends)
    await svc.close()


async def test_list_backends_includes_config_guide_for_real_backends():
    """Closes #36. When a user runs `list-backends` and sees `configured: false`,
    they need to know which env var to set and where to get a key. Surface the
    guide so the answer doesn't depend on remembering the README."""
    # Use the real backends (no constructor args, picks up empty env).
    svc = SearchService()
    backends = svc.list_backends()
    by_name = {b["name"]: b for b in backends}

    for name in ("brave", "serper", "tavily"):
        guide = by_name[name].get("config_guide")
        assert guide is not None, f"{name} missing config_guide"
        assert guide["env_var"]  # non-empty string
        assert guide["signup_url"].startswith("https://")
        assert "config_file" in guide  # mentions the config-file alternative

    # Specific env-var names — guards against silently regressing the
    # resolve_api_key contract documented in CHANGELOG.md.
    assert by_name["brave"]["config_guide"]["env_var"] == "BRAVE_SEARCH_API_KEY"
    assert by_name["serper"]["config_guide"]["env_var"] == "SERPER_API_KEY"
    assert by_name["tavily"]["config_guide"]["env_var"] == "TAVILY_API_KEY"
    await svc.close()


async def test_search_and_read_enforces_url_provenance():
    """search_and_read must only fetch URLs that were in the search results."""
    fake = FakeBackend(results=[
        SearchResult(rank=1, title="A", url="https://a.example/1",
                     snippet="snip"),
    ])
    svc = _svc(fake)

    fake_fetch_calls = []

    async def fake_router_fetch(req):
        fake_fetch_calls.append(req.url)
        return {
            "ok": True, "url": req.url, "final_url": req.url,
            "strategy_used": "http", "title": "A", "content": "body",
            "content_truncated": False, "dump_path": None,
            "metadata": {"status_code": 200, "elapsed_ms": 10,
                         "needs_js_hint": False, "suggested_selectors": []},
            "attempts": [],
        }

    with patch.object(svc.router, "fetch", new=fake_router_fetch):
        out = await svc.search_and_read(
            SearchAndReadRequest(query="x", read_top_n=3)
        )

    assert out["ok"] is True
    assert len(fake_fetch_calls) == 1
    assert fake_fetch_calls[0] == "https://a.example/1"
    assert len(out["fetched_pages"]) == 1
    assert out["fetched_pages"][0]["content_markdown"] == "body"
    await svc.close()


async def test_search_falls_over_to_next_backend_on_rate_limit():
    """A rate-limited primary backend must trigger failover to the next
    configured backend (the README advertises this behavior)."""
    primary = FakeBackend(
        name="brave", raise_=BackendError("RATE_LIMITED", "brave: 429"),
    )
    secondary = FakeBackend(
        name="tavily",
        results=[SearchResult(rank=1, title="T", url="https://t.example/1",
                              snippet="s")],
    )
    svc = _svc(primary, secondary)
    out = await svc.search(SearchRequest(query="x"))
    assert out["ok"] is True
    assert out["backend_used"] == "tavily"
    assert primary.calls == 1
    assert secondary.calls == 1
    await svc.close()


async def test_search_failover_skips_unconfigured_backends():
    primary = FakeBackend(
        name="brave", raise_=BackendError("TIMEOUT", "brave timeout"),
    )
    unconfigured = FakeBackend(name="serper", configured=False)
    fallback = FakeBackend(
        name="tavily",
        results=[SearchResult(rank=1, title="T", url="https://t.example/1",
                              snippet="s")],
    )
    svc = _svc(primary, unconfigured, fallback)
    out = await svc.search(SearchRequest(query="x"))
    assert out["ok"] is True
    assert out["backend_used"] == "tavily"
    assert unconfigured.calls == 0
    await svc.close()


async def test_search_failover_stops_on_non_retryable_error():
    """A pinned-backend or auth-style error should not trigger failover."""
    primary = FakeBackend(
        name="brave", raise_=BackendError("PROVIDER_AUTH", "bad key"),
    )
    secondary = FakeBackend(
        name="tavily",
        results=[SearchResult(rank=1, title="T", url="https://t.example/1",
                              snippet="s")],
    )
    svc = _svc(primary, secondary)
    out = await svc.search(SearchRequest(query="x"))
    assert out["ok"] is False
    assert out["error_code"] == "PROVIDER_AUTH"
    assert secondary.calls == 0  # never tried
    await svc.close()


async def test_search_explicit_backend_does_not_failover():
    """When the caller pins a backend, honor it — don't auto-failover."""
    primary = FakeBackend(
        name="brave", raise_=BackendError("RATE_LIMITED", "brave: 429"),
    )
    secondary = FakeBackend(
        name="tavily",
        results=[SearchResult(rank=1, title="T", url="https://t.example/1",
                              snippet="s")],
    )
    svc = _svc(primary, secondary)
    out = await svc.search(SearchRequest(query="x", backend="brave"))
    assert out["ok"] is False
    assert out["error_code"] == "RATE_LIMITED"
    assert secondary.calls == 0
    await svc.close()


async def test_search_all_backends_fail_returns_last_failure():
    primary = FakeBackend(
        name="brave", raise_=BackendError("RATE_LIMITED", "brave: 429"),
    )
    secondary = FakeBackend(
        name="tavily", raise_=BackendError("TIMEOUT", "tavily timeout"),
    )
    svc = _svc(primary, secondary)
    out = await svc.search(SearchRequest(query="x"))
    assert out["ok"] is False
    assert out["error_code"] == "TIMEOUT"
    await svc.close()


async def test_search_profile_param_scopes_needs_login_annotation(tmp_path):
    """Passing profile='twitter' should only mark x.com results as needs_login,
    even if other active profiles exist for different domains."""
    auth_mod.save_profile("twitter", {"cookies": [], "origins": []}, "x.com")
    auth_mod.save_profile("linkedin", {"cookies": [], "origins": []}, "linkedin.com")
    fake = FakeBackend(results=[
        SearchResult(rank=1, title="T", url="https://x.com/foo", snippet="s"),
        SearchResult(rank=2, title="L", url="https://linkedin.com/in/bar", snippet="s"),
    ])
    svc = _svc(fake)
    out = await svc.search(SearchRequest(query="x", profile="twitter"))
    by_url = {r["url"]: r for r in out["results"]}
    assert by_url["https://x.com/foo"]["fetch_hint"]["needs_login"] is True
    assert by_url["https://linkedin.com/in/bar"]["fetch_hint"]["needs_login"] is False
    await svc.close()


async def test_search_no_profile_marks_any_active_domain(tmp_path):
    """With profile unset, the legacy behavior — flag any active profile's
    domain — must still hold so existing callers don't regress."""
    auth_mod.save_profile("twitter", {"cookies": [], "origins": []}, "x.com")
    fake = FakeBackend(results=[
        SearchResult(rank=1, title="T", url="https://x.com/foo", snippet="s"),
    ])
    svc = _svc(fake)
    out = await svc.search(SearchRequest(query="x"))
    assert out["results"][0]["fetch_hint"]["needs_login"] is True
    await svc.close()


async def test_search_and_read_fetch_timeout_floor_is_15s(monkeypatch):
    """Even when the search ate most of the budget, fetches must get
    at least MIN_FETCH_TIMEOUT_MS so L2 has room to launch a browser."""
    from lightcrawl.search.service import MIN_FETCH_TIMEOUT_MS

    fake = FakeBackend(results=[
        SearchResult(rank=1, title="A", url="https://a.example/1", snippet="s"),
    ])
    svc = _svc(fake)

    seen_timeouts: list[int] = []

    async def fake_router_fetch(req):
        seen_timeouts.append(req.timeout_ms)
        return {
            "ok": True, "url": req.url, "final_url": req.url,
            "strategy_used": "http", "title": "A", "content": "ok",
            "content_truncated": False, "dump_path": None,
            "metadata": {"status_code": 200, "elapsed_ms": 1,
                         "needs_js_hint": False, "suggested_selectors": []},
            "attempts": [],
        }

    with patch.object(svc.router, "fetch", new=fake_router_fetch):
        # Total budget 10s — less than the floor — so the fetch must still
        # get MIN_FETCH_TIMEOUT_MS rather than a sub-second leftover.
        await svc.search_and_read(
            SearchAndReadRequest(query="x", read_top_n=1, timeout_ms=10_000)
        )

    assert seen_timeouts == [MIN_FETCH_TIMEOUT_MS]
    await svc.close()


async def test_search_and_read_records_fetch_failures():
    fake = FakeBackend(results=[
        SearchResult(rank=1, title="A", url="https://a.example/1", snippet="snip"),
        SearchResult(rank=2, title="B", url="https://b.example/2", snippet="snip"),
    ])
    svc = _svc(fake)

    async def fake_router_fetch(req):
        if "a.example" in req.url:
            return {
                "ok": True, "url": req.url, "final_url": req.url,
                "strategy_used": "http", "title": "A", "content": "ok",
                "content_truncated": False, "dump_path": None,
                "metadata": {"status_code": 200, "elapsed_ms": 1,
                             "needs_js_hint": False, "suggested_selectors": []},
                "attempts": [],
            }
        return {
            "ok": False, "url": req.url,
            "error_code": "BLOCKED_BY_CLOUDFLARE",
            "error_detail": "blocked",
            "attempts": [], "suggestions": [],
        }

    with patch.object(svc.router, "fetch", new=fake_router_fetch):
        out = await svc.search_and_read(SearchAndReadRequest(query="x", read_top_n=2))

    assert len(out["fetched_pages"]) == 1
    assert len(out["fetch_failures"]) == 1
    assert out["fetch_failures"][0]["error_code"] == "BLOCKED_BY_CLOUDFLARE"
    await svc.close()


async def test_search_and_read_emits_truncation_warning_when_any_page_truncated():
    """Closes #43. Per-page `content_truncated` is easy to miss when an agent
    is consuming many pages; surface a top-level warning that names the count
    and points to the dump_path field so the agent can re-fetch full content."""
    fake = FakeBackend(results=[
        SearchResult(rank=1, title="A", url="https://a.example/1", snippet="s"),
        SearchResult(rank=2, title="B", url="https://b.example/2", snippet="s"),
        SearchResult(rank=3, title="C", url="https://c.example/3", snippet="s"),
    ])
    svc = _svc(fake)

    async def fake_router_fetch(req):
        truncated = "a.example" in req.url or "b.example" in req.url
        return {
            "ok": True, "url": req.url, "final_url": req.url,
            "strategy_used": "http", "title": req.url, "content": "body",
            "content_truncated": truncated,
            "dump_path": "/tmp/d.md" if truncated else None,
            "metadata": {"status_code": 200, "elapsed_ms": 1,
                         "needs_js_hint": False, "suggested_selectors": []},
            "attempts": [],
        }

    with patch.object(svc.router, "fetch", new=fake_router_fetch):
        out = await svc.search_and_read(SearchAndReadRequest(query="x", read_top_n=3))

    assert "truncation_warning" in out
    assert "2" in out["truncation_warning"]
    assert "3" in out["truncation_warning"]
    assert "dump_path" in out["truncation_warning"]
    await svc.close()


async def test_search_and_read_no_truncation_warning_when_no_pages_truncated():
    """Don't emit the warning when nothing was truncated — keep the response
    shape minimal in the happy path."""
    fake = FakeBackend(results=[
        SearchResult(rank=1, title="A", url="https://a.example/1", snippet="s"),
    ])
    svc = _svc(fake)

    async def fake_router_fetch(req):
        return {
            "ok": True, "url": req.url, "final_url": req.url,
            "strategy_used": "http", "title": "A", "content": "body",
            "content_truncated": False, "dump_path": None,
            "metadata": {"status_code": 200, "elapsed_ms": 1,
                         "needs_js_hint": False, "suggested_selectors": []},
            "attempts": [],
        }

    with patch.object(svc.router, "fetch", new=fake_router_fetch):
        out = await svc.search_and_read(SearchAndReadRequest(query="x", read_top_n=1))

    assert "truncation_warning" not in out
    await svc.close()
