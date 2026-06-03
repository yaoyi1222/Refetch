"""batch-fetch tests (v0.3 PR 7). Fully offline: the canned-page Router harness
(fetch_http patched) drives batch.run_batch_fetch and the CLI handler end to end —
partial failure, the ok=true && ok_count=0 semantics, concurrency bounding, cache
reuse, and urls-file parsing. No network, no browser."""

from __future__ import annotations

import argparse
import json
import threading
import time
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from lightcrawl import batch, cli
from lightcrawl.cache import Cache
from lightcrawl.fetch_http import HttpResult
from lightcrawl.router import Router


def _page(title: str = "Page") -> str:
    filler = "<h1>" + title + "</h1><p>" + ("Lorem ipsum dolor sit amet. " * 8) + "</p>"
    return f"<html><head><title>{title}</title></head><body>{filler}</body></html>"


def _http(url: str, *, status: int = 200, text: str = "", ctype: str = "text/html") -> HttpResult:
    return HttpResult(final_url=url, status_code=status, text=text, content_type=ctype, elapsed_ms=1)


@contextmanager
def _serve(pages: dict[str, str], *, fail: set[str] | None = None):
    fail = fail or set()

    def _fetch(url, **_kw):
        if url in fail:
            raise RuntimeError("boom")
        body = pages.get(url) or pages.get(url.rstrip("/"))
        if body is None:
            return _http(url, status=404, text=_page("Not Found"))
        return _http(url, text=body)

    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"), \
         patch("lightcrawl.fetch_http.fetch", side_effect=_fetch):
        yield


def _bparams(**kw) -> batch.BatchParams:
    kw.setdefault("no_cache", True)
    kw.setdefault("max_age_ms", None)
    kw.setdefault("store_in_cache", False)
    return batch.BatchParams(**kw)


@pytest.fixture
def router() -> Router:
    return Router()


# -- engine -----------------------------------------------------------------


async def test_batch_fetches_all_and_counts(router):
    pages = {f"https://ex.com/p{i}": _page() for i in range(4)}
    with _serve(pages):
        res = await batch.run_batch_fetch(list(pages), _bparams(), router)
    assert res["ok"] is True
    assert res["count"] == 4
    assert res["ok_count"] == 4
    assert res["failed_count"] == 0


async def test_partial_failure_keeps_batch_ok(router):
    pages = {"https://ex.com/a": _page(), "https://ex.com/b": _page()}
    with _serve(pages, fail={"https://ex.com/a"}):
        res = await batch.run_batch_fetch(["https://ex.com/a", "https://ex.com/b"], _bparams(), router)
    assert res["ok"] is True  # batch completed
    assert res["ok_count"] == 1
    assert res["failed_count"] == 1
    by_url = {r["url"]: r for r in res["results"]}
    assert by_url["https://ex.com/a"]["ok"] is False
    assert by_url["https://ex.com/b"]["ok"] is True


async def test_all_fail_is_still_ok_true(router):
    urls = ["https://ex.com/a", "https://ex.com/b"]
    with _serve({}, fail=set(urls)):
        res = await batch.run_batch_fetch(urls, _bparams(), router)
    # honesty contract #5: ok=true means "batch completed", not "all succeeded".
    assert res["ok"] is True
    assert res["ok_count"] == 0
    assert res["failed_count"] == 2


async def test_empty_urls_returns_count_zero_with_notes(router):
    res = await batch.run_batch_fetch([], _bparams(), router)
    assert res["ok"] is True
    assert res["count"] == 0
    assert "notes" in res


async def test_concurrency_is_bounded(router):
    live = 0
    peak = 0
    lock = threading.Lock()

    def _fetch(url, **_kw):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.05)
        with lock:
            live -= 1
        return _http(url, text=_page())

    urls = [f"https://ex.com/p{i}" for i in range(10)]
    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"), \
         patch("lightcrawl.fetch_http.fetch", side_effect=_fetch):
        await batch.run_batch_fetch(urls, _bparams(concurrency=3), router)
    assert peak <= 3  # semaphore cap held
    assert peak >= 2  # and fetches actually overlapped


async def test_cache_hit_skips_network(tmp_path):
    cache = Cache(root=tmp_path / "cache")
    cache.store("https://ex.com/", profile=None, response={
        "ok": True, "content": "# cached", "final_url": "https://ex.com/", "title": "c",
        "metadata": {"status_code": 200, "links": []},
    })
    router = Router(cache=cache)
    params = batch.BatchParams(max_age_ms=10**12, store_in_cache=False, no_cache=False)
    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"), \
         patch("lightcrawl.fetch_http.fetch", side_effect=AssertionError("cache hit must not hit network")):
        res = await batch.run_batch_fetch(["https://ex.com/"], params, router)
    assert res["ok_count"] == 1
    assert res["results"][0].get("cache_hit")


# -- CLI --------------------------------------------------------------------


def _batch_ns(urls, **kw) -> argparse.Namespace:
    base = dict(
        urls=urls, urls_file=None, concurrency=8, output_format="markdown",
        profile=None, max_inline_tokens=8000, timeout_ms=30_000,
        max_age_ms=None, cache_only=False, no_cache=True, no_store=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _out(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


async def test_cli_batch_fetch_positional_urls(capsys):
    pages = {"https://ex.com/a": _page(), "https://ex.com/b": _page()}
    with _serve(pages):
        rc = await cli._run_batch_fetch(_batch_ns(["https://ex.com/a", "https://ex.com/b"]))
    out = _out(capsys)
    assert rc == 0
    assert out["ok"] is True
    assert out["count"] == 2
    assert out["ok_count"] == 2


async def test_cli_batch_fetch_urls_file(capsys, tmp_path):
    pages = {"https://ex.com/a": _page(), "https://ex.com/b": _page()}
    f = tmp_path / "urls.txt"
    f.write_text(
        "# a comment\n"
        "https://ex.com/a\n"
        "\n"
        "  https://ex.com/b  \n"
        "# trailing comment\n",
        encoding="utf-8",
    )
    with _serve(pages):
        await cli._run_batch_fetch(_batch_ns([], urls_file=str(f)))
    out = _out(capsys)
    assert out["count"] == 2
    assert {r["url"] for r in out["results"]} == {"https://ex.com/a", "https://ex.com/b"}


def test_cli_batch_fetch_cache_flag_conflict(capsys):
    rc = cli._cmd_batch_fetch(_batch_ns(["https://ex.com/a"], no_cache=True, cache_only=True))
    out = _out(capsys)
    assert rc == 1
    assert out["error_code"] == "CACHE_FLAG_CONFLICT"
