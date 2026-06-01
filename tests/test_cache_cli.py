"""cache stats / cache clear CLI tests (v0.3 PR 7). Offline: cache.paths.CACHE_ROOT
is redirected to a tmp dir, entries are stored directly, then the CLI handlers are
driven. Covers stats output, the explicit-scope guard on clear (option A), and each
clear scope (--all / --host / --older-than) + the mutual-exclusion conflict."""

from __future__ import annotations

import argparse
import json

import pytest

from lightcrawl import cache as cache_mod
from lightcrawl import cli


@pytest.fixture(autouse=True)
def _tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod.paths, "CACHE_ROOT", tmp_path / "cache")
    return tmp_path


def _store(url: str, profile=None) -> None:
    cache_mod.Cache().store(url, profile=profile, response={
        "ok": True, "content": "# x", "final_url": url, "title": "t",
        "metadata": {"status_code": 200, "links": []},
    })


def _clear_ns(**kw) -> argparse.Namespace:
    base = dict(older_than_ms=None, host=None, clear_all=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _out(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


# -- stats ------------------------------------------------------------------


async def test_cache_stats_reports_counts(capsys):
    _store("https://ex.com/a")
    _store("https://ex.com/b")
    rc = await cli._run_cache_stats(argparse.Namespace())
    out = _out(capsys)
    assert rc == 0
    assert out["ok"] is True
    assert out["stats"]["entry_count"] == 2
    assert "legacy_dumps_bytes" in out
    assert set(out["stats"]) >= {"entry_count", "total_bytes", "hosts"}


# -- clear: scope guard -----------------------------------------------------


async def test_cache_clear_no_scope_is_refused(capsys):
    _store("https://ex.com/a")
    rc = await cli._run_cache_clear(_clear_ns())
    out = _out(capsys)
    assert rc == 1
    assert out["ok"] is False
    assert out["error_code"] == "CACHE_CLEAR_NO_SCOPE"
    # nothing deleted
    assert cache_mod.Cache().stats().entry_count == 1


async def test_cache_clear_conflicting_scopes(capsys):
    rc = await cli._run_cache_clear(_clear_ns(clear_all=True, host="ex.com"))
    out = _out(capsys)
    assert rc == 1
    assert out["error_code"] == "CACHE_FLAG_CONFLICT"


# -- clear: scopes ----------------------------------------------------------


async def test_cache_clear_all_wipes_everything(capsys):
    _store("https://ex.com/a")
    _store("https://other.com/b")
    rc = await cli._run_cache_clear(_clear_ns(clear_all=True))
    out = _out(capsys)
    assert rc == 0
    assert out["deleted_entries"] == 2
    assert out["scope"] == {"all": True}
    assert cache_mod.Cache().stats().entry_count == 0


async def test_cache_clear_by_host(capsys):
    _store("https://ex.com/a")
    _store("https://other.com/b")
    await cli._run_cache_clear(_clear_ns(host="ex.com"))
    out = _out(capsys)
    assert out["deleted_entries"] == 1
    # other.com survives
    assert cache_mod.Cache().stats().entry_count == 1


async def test_cache_clear_older_than(capsys, monkeypatch):
    # Store at the real clock, then run the clear with the clock advanced so the
    # entry is "older than 1ms". (Don't use monkeypatch.undo — it would also
    # revert the autouse CACHE_ROOT redirect and clear the real cache.)
    _store("https://ex.com/old")
    future = cache_mod.time_ms() + 10_000
    monkeypatch.setattr(cache_mod, "time_ms", lambda: future)
    rc = await cli._run_cache_clear(_clear_ns(older_than_ms=1))
    out = _out(capsys)
    assert rc == 0
    assert out["deleted_entries"] == 1
    assert cache_mod.Cache().stats().entry_count == 0
