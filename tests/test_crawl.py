"""Crawl engine tests (v0.3 PR 6.2). Fully offline: a canned-page routing
table is served through the real Router + content pipeline (fetch_http patched),
so link extraction, domain/path filters, robots, dedup, and cache accounting are
all exercised end to end against a real Job. No network, no browser."""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from lightcrawl import crawl, jobs
from lightcrawl.cache import Cache
from lightcrawl.fetch_http import HttpResult
from lightcrawl.router import Router


def _page(links_html: str = "", title: str = "Page") -> str:
    # Pad past the ~200-byte / low-text browser-escalation threshold so the
    # fetch stays on L1 (mocked) instead of escalating to a real browser.
    filler = "<h1>" + title + "</h1><p>" + ("Lorem ipsum dolor sit amet. " * 8) + "</p>"
    return f"<html><head><title>{title}</title></head><body>{filler}{links_html}</body></html>"


def _http(url: str, *, status: int = 200, text: str = "", ctype: str = "text/html") -> HttpResult:
    return HttpResult(final_url=url, status_code=status, text=text, content_type=ctype, elapsed_ms=1)


@contextmanager
def _serve(pages: dict[str, str], *, robots: str | None = None, fail: set[str] | None = None):
    """Serve canned HTML per URL. ``robots`` text is served at /robots.txt for
    every host; ``fail`` URLs raise to exercise fault isolation."""
    fail = fail or set()

    def _fetch(url, **_kw):
        if url in fail:
            raise RuntimeError("boom")
        if url.endswith("/robots.txt"):
            if robots is None:
                return _http(url, status=404, text="nope", ctype="text/plain")
            return _http(url, text=robots, ctype="text/plain")
        body = pages.get(url) or pages.get(url.rstrip("/"))
        if body is None:
            return _http(url, status=404, text=_page(title="Not Found"))
        return _http(url, text=body)

    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"), \
         patch("lightcrawl.fetch_http.fetch", side_effect=_fetch):
        yield


def _params(seed: str, **kw) -> crawl.CrawlParams:
    # Default to cache off so tests touch no disk; cache tests opt back in.
    kw.setdefault("no_cache", True)
    kw.setdefault("max_age_ms", None)
    kw.setdefault("store_in_cache", False)
    return crawl.CrawlParams(seed=seed, **kw)


def _fetched_urls(job: jobs.Job) -> list[str]:
    if not job.results_path.exists():
        return []
    return [json.loads(line)["url"] for line in job.results_path.read_text().splitlines()]


@pytest.fixture
def router() -> Router:
    return Router()


async def _run(job, params, router):
    await crawl.run_crawl(params, job, router)


# -- BFS / depth / pages ---------------------------------------------------


async def test_bfs_visits_all_linked_internal_pages(tmp_path, router):
    pages = {
        "https://ex.com/": _page('<a href="https://ex.com/a">a</a><a href="https://ex.com/b">b</a>'),
        "https://ex.com/a": _page('<a href="https://ex.com/c">c</a>'),
        "https://ex.com/b": _page(),
        "https://ex.com/c": _page(),
    }
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    with _serve(pages):
        await _run(job, _params("https://ex.com/", max_depth=3, max_pages=100), router)
    fetched = set(_fetched_urls(job))
    assert fetched == {"https://ex.com/", "https://ex.com/a", "https://ex.com/b", "https://ex.com/c"}
    assert job.status == jobs.JobStatus.COMPLETED


async def test_max_depth_stops_expansion(tmp_path, router):
    pages = {
        "https://ex.com/": _page('<a href="https://ex.com/a">a</a>'),
        "https://ex.com/a": _page('<a href="https://ex.com/deep">deep</a>'),
        "https://ex.com/deep": _page(),
    }
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    with _serve(pages):
        await _run(job, _params("https://ex.com/", max_depth=1, max_pages=100), router)
    fetched = set(_fetched_urls(job))
    assert fetched == {"https://ex.com/", "https://ex.com/a"}  # /deep is depth 2, skipped


async def test_max_pages_soft_cap(tmp_path, router):
    links = "".join(f'<a href="https://ex.com/p{i}">{i}</a>' for i in range(20))
    pages = {"https://ex.com/": _page(links)}
    pages.update({f"https://ex.com/p{i}": _page() for i in range(20)})
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    with _serve(pages):
        await _run(job, _params("https://ex.com/", max_depth=3, max_pages=5, concurrency=2), router)
    # Soft cap: stops at >= max_pages, may overshoot by < concurrency.
    assert 5 <= job.progress.pages_fetched <= 6


# -- domain boundary -------------------------------------------------------


async def test_external_links_not_followed(tmp_path, router):
    pages = {
        "https://ex.com/": _page('<a href="https://ex.com/a">a</a><a href="https://other.com/x">x</a>'),
        "https://ex.com/a": _page(),
    }
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    with _serve(pages):
        await _run(job, _params("https://ex.com/"), router)
    assert set(_fetched_urls(job)) == {"https://ex.com/", "https://ex.com/a"}


async def test_allow_subdomains_follows_same_etld1(tmp_path, router):
    pages = {
        "https://ex.com/": _page('<a href="https://docs.ex.com/g">g</a>'),
        "https://docs.ex.com/g": _page(),
    }
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    with _serve(pages):
        await _run(job, _params("https://ex.com/", allow_subdomains=True), router)
    assert "https://docs.ex.com/g" in set(_fetched_urls(job))


# -- path filters ----------------------------------------------------------


async def test_exclude_paths_skip_and_count(tmp_path, router):
    pages = {
        "https://ex.com/": _page('<a href="https://ex.com/keep">k</a><a href="https://ex.com/skip">s</a>'),
        "https://ex.com/keep": _page(),
        "https://ex.com/skip": _page(),
    }
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    with _serve(pages):
        await _run(job, _params("https://ex.com/", exclude_paths=(r"/skip",)), router)
    fetched = set(_fetched_urls(job))
    assert "https://ex.com/keep" in fetched
    assert "https://ex.com/skip" not in fetched
    assert job.progress.pages_skipped_filter == 1


async def test_include_paths_only(tmp_path, router):
    pages = {
        "https://ex.com/": _page('<a href="https://ex.com/docs/a">a</a><a href="https://ex.com/blog/b">b</a>'),
        "https://ex.com/docs/a": _page(),
        "https://ex.com/blog/b": _page(),
    }
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    with _serve(pages):
        await _run(job, _params("https://ex.com/", include_paths=(r"/docs/",)), router)
    fetched = set(_fetched_urls(job))
    assert "https://ex.com/docs/a" in fetched
    assert "https://ex.com/blog/b" not in fetched


# -- robots ----------------------------------------------------------------


async def test_robots_disallow_skips_and_counts(tmp_path, router):
    pages = {
        "https://ex.com/": _page('<a href="https://ex.com/private/x">p</a><a href="https://ex.com/ok">o</a>'),
        "https://ex.com/private/x": _page(),
        "https://ex.com/ok": _page(),
    }
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    with _serve(pages, robots="User-agent: *\nDisallow: /private\n"):
        await _run(job, _params("https://ex.com/", ignore_robots=False), router)
    fetched = set(_fetched_urls(job))
    assert "https://ex.com/ok" in fetched
    assert "https://ex.com/private/x" not in fetched
    assert job.progress.pages_skipped_robots == 1


async def test_ignore_robots_crawls_disallowed(tmp_path, router):
    pages = {
        "https://ex.com/": _page('<a href="https://ex.com/private/x">p</a>'),
        "https://ex.com/private/x": _page(),
    }
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    with _serve(pages, robots="User-agent: *\nDisallow: /private\n"):
        await _run(job, _params("https://ex.com/", ignore_robots=True), router)
    assert "https://ex.com/private/x" in set(_fetched_urls(job))


# -- dedup / fault isolation ----------------------------------------------


async def test_cycle_visits_each_once(tmp_path, router):
    pages = {
        "https://ex.com/": _page('<a href="https://ex.com/a">a</a>'),
        "https://ex.com/a": _page('<a href="https://ex.com/">home</a>'),  # back-link cycle
    }
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    with _serve(pages):
        await _run(job, _params("https://ex.com/"), router)
    assert sorted(_fetched_urls(job)) == ["https://ex.com/", "https://ex.com/a"]


async def test_failure_does_not_block_crawl(tmp_path, router):
    pages = {
        "https://ex.com/": _page('<a href="https://ex.com/bad">bad</a><a href="https://ex.com/good">good</a>'),
        "https://ex.com/good": _page(),
    }
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    with _serve(pages, fail={"https://ex.com/bad"}):
        await _run(job, _params("https://ex.com/"), router)
    assert "https://ex.com/good" in set(_fetched_urls(job))
    assert job.progress.pages_failed >= 1
    assert job.status == jobs.JobStatus.COMPLETED


# -- cache accounting ------------------------------------------------------


async def test_cache_hit_counts_skipped(tmp_path):
    cache = Cache(root=tmp_path / "cache")
    cache.store("https://ex.com/", profile=None, response={
        "ok": True, "content": "# cached", "final_url": "https://ex.com/", "title": "c",
        "metadata": {"status_code": 200, "links": []},
    })
    router = Router(cache=cache)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    params = crawl.CrawlParams(
        seed="https://ex.com/", max_age_ms=10**12, store_in_cache=False, no_cache=False,
        max_pages=10, ignore_robots=True,  # isolate page-cache from a robots.txt fetch
    )
    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"), \
         patch("lightcrawl.fetch_http.fetch", side_effect=AssertionError("cache hit must not hit network")):
        await crawl.run_crawl(params, job, router)
    assert job.progress.pages_skipped_cache == 1
    assert job.progress.pages_fetched == 1


# -- finalize --------------------------------------------------------------


async def test_finalize_drops_pid_on_completion(tmp_path, router):
    pages = {"https://ex.com/": _page()}
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    with _serve(pages):
        await _run(job, _params("https://ex.com/"), router)
    assert job.status == jobs.JobStatus.COMPLETED
    assert not job.pid_path.exists()
