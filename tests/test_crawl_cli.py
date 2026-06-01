"""Crawl CLI tests (v0.3 PR 6.3). Fully offline: the same canned-page Router
harness as test_crawl.py drives the ``crawl`` / ``crawl-status`` / ``crawl-resume``
/ ``crawl-cancel`` / ``jobs`` subcommands end to end, plus startup reconcile and
the signal handler. ``jobs.paths.JOBS`` is redirected to a tmp dir so no test
touches ~/.lightcrawl."""

from __future__ import annotations

import argparse
import dataclasses
import json
import signal
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from lightcrawl import cli, crawl, jobs
from lightcrawl.errors import ErrorCode, FetchError
from lightcrawl.fetch_http import HttpResult
from lightcrawl.jobs import FrontierItem, Job, JobStatus


# -- harness (mirrors tests/test_crawl.py) ---------------------------------


def _page(links_html: str = "", title: str = "Page") -> str:
    filler = "<h1>" + title + "</h1><p>" + ("Lorem ipsum dolor sit amet. " * 8) + "</p>"
    return f"<html><head><title>{title}</title></head><body>{filler}{links_html}</body></html>"


def _http(url: str, *, status: int = 200, text: str = "", ctype: str = "text/html") -> HttpResult:
    return HttpResult(final_url=url, status_code=status, text=text, content_type=ctype, elapsed_ms=1)


@contextmanager
def _serve(pages: dict[str, str], *, robots: str | None = None, fail: set[str] | None = None):
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


@pytest.fixture(autouse=True)
def _tmp_jobs(tmp_path, monkeypatch):
    """Redirect every job file under a per-test tmp dir."""
    monkeypatch.setattr(jobs.paths, "JOBS", tmp_path)
    return tmp_path


def _out(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def _crawl_ns(url: str, **kw) -> argparse.Namespace:
    base = dict(
        url=url, max_depth=3, max_pages=100, include_paths=[], exclude_paths=[],
        allow_subdomains=False, crawl_entire_domain=False, ignore_robots=True,
        ignore_query_parameters=False, concurrency=4, user_agent="*",
        output_format="markdown", profile=None,
        # cache off by default so tests touch no disk cache.
        max_age_ms=None, cache_only=False, no_cache=True, no_store=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


# -- crawl ------------------------------------------------------------------


async def test_crawl_runs_and_prints_summary(capsys):
    pages = {
        "https://ex.com/": _page('<a href="https://ex.com/a">a</a>'),
        "https://ex.com/a": _page(),
    }
    with _serve(pages):
        rc = await cli._run_crawl(_crawl_ns("https://ex.com/"))
    out = _out(capsys)
    assert rc == 0
    assert out["ok"] is True
    assert out["status"] == "completed"
    assert out["progress"]["pages_fetched"] == 2
    assert out["job_id"].startswith("crawl-")


async def test_crawl_max_pages_surfaces_note(capsys):
    links = "".join(f'<a href="https://ex.com/p{i}">{i}</a>' for i in range(10))
    pages = {"https://ex.com/": _page(links)}
    pages.update({f"https://ex.com/p{i}": _page() for i in range(10)})
    with _serve(pages):
        await cli._run_crawl(_crawl_ns("https://ex.com/", max_pages=3, concurrency=1))
    out = _out(capsys)
    assert out["ok"] is True
    assert out["error_code"] == ErrorCode.CRAWL_MAX_PAGES.value
    assert "max-pages" in out["note"]


async def test_crawl_cache_flag_conflict(capsys):
    # _cmd_crawl validates before any async work; safe to call synchronously.
    rc = cli._cmd_crawl(_crawl_ns("https://ex.com/", no_cache=True, cache_only=True))
    out = _out(capsys)
    assert rc == 1
    assert out["ok"] is False
    assert out["error_code"] == ErrorCode.CACHE_FLAG_CONFLICT.value


# -- crawl-status -----------------------------------------------------------


async def test_crawl_status_reports_progress(capsys):
    with _serve({"https://ex.com/": _page()}):
        await cli._run_crawl(_crawl_ns("https://ex.com/"))
    job_id = _out(capsys)["job_id"]
    await cli._run_crawl_status(argparse.Namespace(job_id=job_id))
    out = _out(capsys)
    assert out["ok"] is True
    assert out["status"] == "completed"
    assert out["type"] == "crawl"
    assert out["progress"]["pages_fetched"] == 1


def test_crawl_status_not_found(capsys):
    rc = cli._cmd_crawl_status(argparse.Namespace(job_id="crawl-nope"))
    out = _out(capsys)
    assert rc == 1
    assert out["ok"] is False
    assert out["error_code"] == ErrorCode.JOB_NOT_FOUND.value


# -- jobs -------------------------------------------------------------------


async def test_jobs_lists_all(capsys):
    ids = []
    with _serve({"https://ex.com/": _page()}):
        for _ in range(2):
            await cli._run_crawl(_crawl_ns("https://ex.com/"))
            ids.append(_out(capsys)["job_id"])
    await cli._run_jobs(argparse.Namespace())
    out = _out(capsys)
    assert out["ok"] is True
    listed = {j["job_id"] for j in out["jobs"]}
    assert set(ids) <= listed
    assert len(out["jobs"]) >= 2


# -- crawl-cancel -----------------------------------------------------------


async def test_crawl_cancel_idle_job_finalizes_cancelled(capsys, monkeypatch):
    job = Job.create("crawl", {})
    job.status = JobStatus.INTERRUPTED
    job.flush(force=True)
    monkeypatch.setattr(jobs, "is_owner_alive", lambda p: False)
    await cli._run_crawl_cancel(argparse.Namespace(job_id=job.job_id))
    out = _out(capsys)
    assert out["ok"] is True
    assert out["status"] == "cancelled"
    assert jobs.read_status(job.job_id)["status"] == "cancelled"


async def test_crawl_cancel_running_only_requests(capsys, monkeypatch):
    job = Job.create("crawl", {})
    job.status = JobStatus.RUNNING
    job.flush(force=True)
    monkeypatch.setattr(jobs, "is_owner_alive", lambda p: True)
    await cli._run_crawl_cancel(argparse.Namespace(job_id=job.job_id))
    out = _out(capsys)
    assert out["status"] == "cancelling"
    assert job.cancel_path.exists()
    # The live process owns finalization — status not flipped here.
    assert jobs.read_status(job.job_id)["status"] == "running"


async def test_crawl_cancel_terminal_is_noop(capsys):
    job = Job.create("crawl", {})
    job.finalize()  # completed
    await cli._run_crawl_cancel(argparse.Namespace(job_id=job.job_id))
    out = _out(capsys)
    assert out["ok"] is True
    assert out["status"] == "completed"
    assert "nothing to cancel" in out["note"]
    assert not job.cancel_path.exists()


# -- crawl-resume -----------------------------------------------------------


async def test_crawl_resume_guards_live_job(monkeypatch):
    job = Job.create("crawl", {})
    job.status = JobStatus.RUNNING
    job.flush(force=True)
    monkeypatch.setattr(jobs, "is_owner_alive", lambda p: True)
    with pytest.raises(FetchError) as ei:
        await cli._run_crawl_resume(argparse.Namespace(job_id=job.job_id))
    assert ei.value.code is ErrorCode.JOB_ALREADY_RUNNING


async def test_crawl_resume_completes_interrupted(capsys, monkeypatch):
    pages = {
        "https://ex.com/": _page('<a href="https://ex.com/a">a</a>'),
        "https://ex.com/a": _page(),
    }
    params = crawl.CrawlParams(
        seed="https://ex.com/", ignore_robots=True,
        no_cache=True, max_age_ms=None, store_in_cache=False,
    )
    job = Job.create("crawl", dataclasses.asdict(params))
    job.push_frontier(FrontierItem("https://ex.com/", 0))  # work left to do
    job.request_shutdown("interrupted")
    job.finalize()
    assert job.status == JobStatus.INTERRUPTED

    monkeypatch.setattr(jobs, "is_owner_alive", lambda p: False)
    with _serve(pages):
        rc = await cli._run_crawl_resume(argparse.Namespace(job_id=job.job_id))
    out = _out(capsys)
    assert rc == 0
    assert out["status"] == "completed"
    assert out["progress"]["pages_fetched"] == 2


# -- startup reconcile + signals -------------------------------------------


def test_main_reconciles_dead_running_job(capsys, monkeypatch):
    monkeypatch.setattr(cli, "ensure_dirs", lambda: None)
    job = Job.create("crawl", {})
    job.status = JobStatus.RUNNING
    job.flush(force=True)
    monkeypatch.setattr(jobs, "is_owner_alive", lambda p: False)  # crashed owner
    rc = cli.main(["jobs"])
    out = _out(capsys)
    assert rc == 0
    statuses = {j["job_id"]: j["status"] for j in out["jobs"]}
    assert statuses[job.job_id] == "interrupted"


def test_install_signal_handlers_flags_interrupted():
    job = Job.create("crawl", {})
    registered: dict = {}

    class _FakeLoop:
        def add_signal_handler(self, sig, cb):
            registered[sig] = cb

    crawl.install_signal_handlers(_FakeLoop(), job)
    assert signal.SIGINT in registered and signal.SIGTERM in registered
    registered[signal.SIGINT]()  # simulate Ctrl-C
    assert job.should_stop() is True
    job.finalize()
    assert job.status == JobStatus.INTERRUPTED
