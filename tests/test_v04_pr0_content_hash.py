"""v0.4 PR-0 (#76): full_content_hash in router result envelope + jobs.record() persistence.

All tests offline: monkeypatched fetch_http.fetch + fake jobs clock.
"""
from __future__ import annotations

import hashlib
import json
from unittest.mock import patch

import pytest

import lightcrawl.content as content_mod
from lightcrawl import jobs
from lightcrawl.fetch_http import HttpResult
from lightcrawl.router import FetchRequest, Router


# ---------------------------------------------------------------------------
# Fixtures shared with test_router.py pattern
# ---------------------------------------------------------------------------

@pytest.fixture
def router():
    return Router()


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    monkeypatch.setattr("lightcrawl.paths.ROOT", tmp_path)
    monkeypatch.setattr("lightcrawl.paths.DUMPS", tmp_path / "dumps")
    monkeypatch.setattr("lightcrawl.paths.PROFILES", tmp_path / "profiles")
    monkeypatch.setattr("lightcrawl.paths.LOGS", tmp_path / "logs")
    monkeypatch.setattr("lightcrawl.content.DUMPS", tmp_path / "dumps")
    monkeypatch.setattr("lightcrawl.auth.PROFILES", tmp_path / "profiles")
    (tmp_path / "dumps").mkdir(parents=True)
    (tmp_path / "profiles").mkdir(parents=True)


_SIMPLE_HTML = (
    "<html><head><title>Test Page</title></head><body>"
    "<article><h1>Hello World</h1>"
    "<p>Body text long enough to avoid the tiny-body browser-escalation "
    "heuristic in _should_escalate_to_browser (len >= 200 chars).</p>"
    "</article></body></html>"
)

_FAKE_HTTP = HttpResult(
    final_url="https://example.com/",
    status_code=200,
    text=_SIMPLE_HTML,
    content_type="text/html",
    elapsed_ms=10,
)

_PATCH_DNS = patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34")


# ---------------------------------------------------------------------------
# Router: full_content_hash must appear in result envelope
# ---------------------------------------------------------------------------

async def test_http_success_exposes_full_content_hash(router):
    """router.fetch() via HTTP must include full_content_hash in the result."""
    with _PATCH_DNS, patch("lightcrawl.fetch_http.fetch", return_value=_FAKE_HTTP):
        result = await router.fetch(FetchRequest(url="https://example.com/"))

    assert result["ok"] is True
    assert "full_content_hash" in result
    # Content is short → not truncated; full body == inline so hashes agree.
    assert result["content_truncated"] is False
    expected = hashlib.sha1(result["content"].encode("utf-8")).hexdigest()
    assert result["full_content_hash"] == expected


async def test_full_content_hash_covers_pre_truncation_body(router, monkeypatch):
    """full_content_hash must be sha1(full_body), not sha1(truncated_inline_head)."""
    captured: list[str] = []
    original_maybe_dump = content_mod.maybe_dump

    def spy(url, body, max_inline_tokens, **kwargs):
        captured.append(body)
        return original_maybe_dump(url, body, max_inline_tokens, **kwargs)

    monkeypatch.setattr("lightcrawl.content.maybe_dump", spy)

    with _PATCH_DNS, patch("lightcrawl.fetch_http.fetch", return_value=_FAKE_HTTP):
        # max_inline_tokens=1 forces truncation — inline will be much shorter than body
        result = await router.fetch(FetchRequest(url="https://example.com/", max_inline_tokens=1))

    assert result["content_truncated"] is True
    assert len(captured) == 1
    full_body = captured[0]
    assert len(result["content"]) < len(full_body)

    # full_content_hash must match the FULL pre-truncation body
    assert result["full_content_hash"] == hashlib.sha1(full_body.encode("utf-8")).hexdigest()
    # and must differ from sha1(inline_head)
    assert result["full_content_hash"] != hashlib.sha1(result["content"].encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# jobs.record(): persist content fields to results.jsonl
# ---------------------------------------------------------------------------

def test_record_persists_content_and_hash(tmp_path, monkeypatch):
    """record() must write content, content_hash, content_truncated, dump_path, headings."""
    monkeypatch.setattr(jobs, "time_ms", lambda: 5)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)

    job.record({
        "ok": True,
        "url": "https://ex.com/a",
        "final_url": "https://ex.com/a",
        "content": "# Hello\n\nSome text.",
        "full_content_hash": "deadbeef01234567",
        "content_truncated": False,
        "dump_path": None,
        "headings": [{"level": 1, "text": "Hello", "line": 1}],
        "metadata": {"status_code": 200},
        "cache_hit": False,
    })

    line = json.loads(job.results_path.read_text().splitlines()[0])
    assert line["content"] == "# Hello\n\nSome text."
    assert line["content_hash"] == "deadbeef01234567"
    assert line["content_truncated"] is False
    assert line["dump_path"] is None
    assert line["headings"] == [{"level": 1, "text": "Hello", "line": 1}]


def test_record_uses_full_content_hash_key_not_recomputed(tmp_path, monkeypatch):
    """record() must take content_hash from result['full_content_hash'], not sha1(content)."""
    monkeypatch.setattr(jobs, "time_ms", lambda: 5)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)

    full_hash = hashlib.sha1(b"full body text before truncation").hexdigest()
    truncated_head = "full body text"  # shorter than original

    job.record({
        "ok": True,
        "url": "https://ex.com/b",
        "final_url": "https://ex.com/b",
        "content": truncated_head,
        "full_content_hash": full_hash,
        "content_truncated": True,
        "dump_path": "/dumps/abc.md",
        "headings": [],
        "metadata": {"status_code": 200},
        "cache_hit": False,
    })

    line = json.loads(job.results_path.read_text().splitlines()[0])
    # Must be the full-body hash, not sha1(truncated_head)
    assert line["content_hash"] == full_hash
    assert line["content_hash"] != hashlib.sha1(truncated_head.encode()).hexdigest()
    assert line["content_truncated"] is True
    assert line["dump_path"] == "/dumps/abc.md"


async def test_cache_hit_exposes_full_content_hash(router, tmp_path, monkeypatch):
    """_success_from_cache must emit full_content_hash so jobs.record() never writes null."""
    from lightcrawl.cache import Cache

    monkeypatch.setattr("lightcrawl.paths.CACHE_ROOT", tmp_path / "cache")
    (tmp_path / "cache").mkdir()

    with _PATCH_DNS, patch("lightcrawl.fetch_http.fetch", return_value=_FAKE_HTTP):
        live = await router.fetch(FetchRequest(url="https://example.com/", store_in_cache=True))

    assert live["ok"] is True
    assert live["full_content_hash"]

    with _PATCH_DNS:
        hit = await router.fetch(FetchRequest(url="https://example.com/", max_age_ms=3600_000))

    assert hit["ok"] is True
    assert hit.get("cache_hit") is True
    assert hit["full_content_hash"] == live["full_content_hash"]
    assert hit["full_content_hash"]  # not empty string / None


def test_record_failure_omits_content_fields(tmp_path, monkeypatch):
    """On fetch failure, record() must not emit content, content_hash, headings, dump_path."""
    monkeypatch.setattr(jobs, "time_ms", lambda: 5)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)

    job.record({"ok": False, "url": "https://ex.com/fail", "error_code": "TIMEOUT"})

    line = json.loads(job.results_path.read_text().splitlines()[0])
    assert line["ok"] is False
    assert "content" not in line
    assert "content_hash" not in line
    assert "headings" not in line
    assert "dump_path" not in line
