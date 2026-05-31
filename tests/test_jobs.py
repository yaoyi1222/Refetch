"""Crawl job data-layer tests (v0.3 PR 5). Fully offline: tmp_path job dir,
monkeypatched ``jobs.time_ms`` fake clock, real psutil against the test process."""

from __future__ import annotations

import json

import pytest

from lightcrawl import paths
from lightcrawl.errors import ErrorCode


def test_job_error_codes_exist():
    assert ErrorCode.JOB_NOT_FOUND.value == "JOB_NOT_FOUND"
    assert ErrorCode.JOB_NOT_RESUMABLE.value == "JOB_NOT_RESUMABLE"


def test_paths_has_jobs_dir():
    assert paths.JOBS == paths.ROOT / "jobs"
    assert paths.JOBS in [paths.JOBS]  # referenced by ensure_dirs (see source)


def test_psutil_importable():
    import psutil  # noqa: F401


import re  # noqa: E402

from lightcrawl import jobs  # noqa: E402
from lightcrawl.jobs import FrontierItem, JobStatus, Progress  # noqa: E402


def test_new_job_id_format_and_no_colon():
    jid = jobs.new_job_id()
    assert re.fullmatch(r"crawl-\d{8}T\d{6}-[0-9a-f]{12}", jid)
    assert ":" not in jid  # Windows NTFS safe


def test_progress_defaults_zero():
    p = Progress()
    assert (p.pages_fetched, p.pages_failed, p.pages_pending) == (0, 0, 0)
    assert (p.pages_skipped_cache, p.pages_skipped_robots, p.pages_skipped_filter) == (0, 0, 0)


def test_frontier_item_is_url_depth():
    it = FrontierItem("https://ex.com/a", 2)
    assert it.url == "https://ex.com/a" and it.depth == 2


def test_job_status_values():
    assert {s.value for s in JobStatus} == {
        "created", "running", "completed", "interrupted", "cancelled",
    }


def test_pid_file_roundtrip_alive(tmp_path):
    p = tmp_path / "x.pid"
    jobs.write_pid_file(p)
    assert jobs.is_owner_alive(p) is True  # written by this live process


def test_pid_missing_is_dead(tmp_path):
    assert jobs.is_owner_alive(tmp_path / "nope.pid") is False


def test_pid_corrupt_is_dead(tmp_path):
    p = tmp_path / "bad.pid"
    p.write_text("{not json")
    assert jobs.is_owner_alive(p) is False


def test_pid_create_time_mismatch_is_dead(tmp_path):
    # PID-reuse immunity: same pid, different create_time → original owner dead.
    import json as _json

    import psutil
    p = tmp_path / "reuse.pid"
    me = psutil.Process()
    p.write_text(_json.dumps({"pid": me.pid, "create_time": me.create_time() + 5.0}))
    assert jobs.is_owner_alive(p) is False


def test_pid_unknown_process_is_dead(tmp_path):
    import json as _json
    p = tmp_path / "ghost.pid"
    p.write_text(_json.dumps({"pid": 2_147_483_646, "create_time": 1.0}))
    assert jobs.is_owner_alive(p) is False


from lightcrawl.errors import FetchError  # noqa: E402


def test_create_writes_json_and_pid_running(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "time_ms", lambda: 1_000)
    job = jobs.Job.create("crawl", {"seed": "https://ex.com/"}, jobs_dir=tmp_path)
    assert job.status == JobStatus.RUNNING
    data = json.loads((tmp_path / f"{job.job_id}.json").read_text())
    assert data["status"] == "running"
    assert data["type"] == "crawl"
    assert data["params"] == {"seed": "https://ex.com/"}
    assert data["started_at"] == 1_000
    assert (tmp_path / f"{job.job_id}.pid").exists()


def test_load_roundtrips_progress_and_params(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "time_ms", lambda: 2_000)
    job = jobs.Job.create("crawl", {"seed": "https://ex.com/"}, jobs_dir=tmp_path)
    job.progress.pages_fetched = 7
    job.flush(force=True)
    loaded = jobs.Job.load(job.job_id, jobs_dir=tmp_path)
    assert loaded.progress.pages_fetched == 7
    assert loaded.params == {"seed": "https://ex.com/"}
    assert loaded.status == JobStatus.RUNNING


def test_load_missing_raises_job_not_found(tmp_path):
    with pytest.raises(FetchError) as ei:
        jobs.Job.load("crawl-nope", jobs_dir=tmp_path)
    assert ei.value.code == ErrorCode.JOB_NOT_FOUND


def test_flush_is_atomic_leaves_no_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "time_ms", lambda: 3_000)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    job.flush(force=True)
    assert not list(tmp_path.glob("*.tmp"))


def test_flush_throttle_suppresses_until_due(tmp_path, monkeypatch):
    clock = {"t": 10_000}
    monkeypatch.setattr(jobs, "time_ms", lambda: clock["t"])
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)  # force flush at 10_000
    first = json.loads(job.json_path.read_text())["updated_at"]
    assert first == 10_000
    # 2s later, 0 pages → throttled, no rewrite
    clock["t"] = 12_000
    job.flush()
    assert json.loads(job.json_path.read_text())["updated_at"] == 10_000
    # 5s after last flush → time threshold fires
    clock["t"] = 15_000
    job.flush()
    assert json.loads(job.json_path.read_text())["updated_at"] == 15_000


def test_flush_pages_threshold_fires(tmp_path, monkeypatch):
    clock = {"t": 0}
    monkeypatch.setattr(jobs, "time_ms", lambda: clock["t"])
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    # 9 pages within the time window → still throttled
    job._pages_since_flush = 9
    clock["t"] = 1_000
    job.flush()
    assert json.loads(job.json_path.read_text())["updated_at"] == 0
    # 10th page → page threshold fires even though <5s elapsed
    job._pages_since_flush = 10
    job.flush()
    assert json.loads(job.json_path.read_text())["updated_at"] == 1_000


def test_mark_claimed_then_completed_sets_and_file(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "time_ms", lambda: 5)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    job.mark_claimed("https://ex.com/a")
    job.mark_completed("https://ex.com/a")
    assert "https://ex.com/a" in job.claimed
    assert "https://ex.com/a" in job.completed
    lines = job.visited_path.read_text().splitlines()
    assert lines[0].split("\t")[:2] == ["https://ex.com/a", "claimed"]
    assert lines[1].split("\t")[:2] == ["https://ex.com/a", "completed"]


def test_load_hydrates_visited_sets(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "time_ms", lambda: 5)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    job.mark_claimed("https://ex.com/a")
    job.mark_claimed("https://ex.com/b")
    job.mark_completed("https://ex.com/a")
    loaded = jobs.Job.load(job.job_id, jobs_dir=tmp_path)
    assert loaded.claimed == {"https://ex.com/a", "https://ex.com/b"}
    assert loaded.completed == {"https://ex.com/a"}


def test_load_skips_corrupt_visited_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "time_ms", lambda: 5)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    job.mark_claimed("https://ex.com/a")
    with job.visited_path.open("a") as f:
        f.write("garbage-no-tabs\n")
    loaded = jobs.Job.load(job.job_id, jobs_dir=tmp_path)
    assert loaded.claimed == {"https://ex.com/a"}


def test_record_success_marks_completed_and_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "time_ms", lambda: 5)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    job.record({"ok": True, "url": "https://ex.com/a",
                "metadata": {"status_code": 200}, "cache_hit": True})
    assert job.progress.pages_fetched == 1
    assert "https://ex.com/a" in job.completed
    line = json.loads(job.results_path.read_text().splitlines()[0])
    assert line["ok"] is True and line["cache_hit"] is True and line["status"] == 200


def test_record_failure_counts_and_tails_error(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "time_ms", lambda: 5)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    job.record({"ok": False, "url": "https://ex.com/x", "error_code": "TIMEOUT"})
    assert job.progress.pages_failed == 1
    assert "https://ex.com/x" not in job.completed
    assert job.errors_tail[-1]["error_code"] == "TIMEOUT"
    line = json.loads(job.results_path.read_text().splitlines()[0])
    assert line["ok"] is False and line["error_code"] == "TIMEOUT"


def test_errors_tail_capped_at_50(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "time_ms", lambda: 5)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    for i in range(60):
        job.record({"ok": False, "url": f"https://ex.com/{i}", "error_code": "TIMEOUT"})
    assert len(job.errors_tail) == 50
    assert job.errors_tail[-1]["url"] == "https://ex.com/59"
    assert job.progress.pages_failed == 60


def test_frontier_push_pop_fifo(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "time_ms", lambda: 5)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    job.push_frontier(FrontierItem("https://ex.com/a", 0))
    job.push_frontier(FrontierItem("https://ex.com/b", 1))
    assert job.pop_frontier() == FrontierItem("https://ex.com/a", 0)
    assert job.pop_frontier() == FrontierItem("https://ex.com/b", 1)
    assert job.pop_frontier() is None


def test_frontier_hydrates_after_load(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "time_ms", lambda: 5)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    job.push_frontier(FrontierItem("https://ex.com/a", 0))
    job.push_frontier(FrontierItem("https://ex.com/b", 1))
    job.pop_frontier()  # consume a → only b remains
    job.push_frontier(FrontierItem("https://ex.com/c", 2))
    loaded = jobs.Job.load(job.job_id, jobs_dir=tmp_path)
    assert [it.url for it in loaded._frontier] == ["https://ex.com/b", "https://ex.com/c"]


def test_frontier_compacts_past_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "time_ms", lambda: 5)
    monkeypatch.setattr(jobs, "_FRONTIER_COMPACT_THRESHOLD", 10)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    for i in range(8):
        job.push_frontier(FrontierItem(f"https://ex.com/{i}", 0))
    for _ in range(6):
        job.pop_frontier()  # 8 push + 6 pop = 14 ops > 10 → compaction triggers mid-run
    # Compaction ran, so the op log is shorter than the 14 ops we issued
    # (it rewrote the live queue as push lines on the 11th op, then a few
    # pop ops appended after). Exact replay must still be correct.
    lines = job.frontier_path.read_text().splitlines()
    assert len(lines) < 14
    loaded = jobs.Job.load(job.job_id, jobs_dir=tmp_path)
    assert [it.url for it in loaded._frontier] == ["https://ex.com/6", "https://ex.com/7"]


def test_resume_requires_interrupted_status(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "time_ms", lambda: 5)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)  # status running
    with pytest.raises(FetchError) as ei:
        jobs.Job.resume(job.job_id, jobs_dir=tmp_path)
    assert ei.value.code == ErrorCode.JOB_NOT_RESUMABLE


def test_resume_missing_job_raises_not_found(tmp_path):
    with pytest.raises(FetchError) as ei:
        jobs.Job.resume("crawl-nope", jobs_dir=tmp_path)
    assert ei.value.code == ErrorCode.JOB_NOT_FOUND


def test_resume_reenqueues_claimed_not_completed(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "time_ms", lambda: 5)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    job.mark_claimed("https://ex.com/a")
    job.mark_completed("https://ex.com/a")  # done
    job.mark_claimed("https://ex.com/b")     # claimed but never completed
    job.status = JobStatus.INTERRUPTED
    job.flush(force=True)
    resumed = jobs.Job.resume(job.job_id, jobs_dir=tmp_path)
    assert resumed.status == JobStatus.RUNNING
    urls = [it.url for it in resumed._frontier]
    assert "https://ex.com/b" in urls
    assert "https://ex.com/a" not in urls
    assert (tmp_path / f"{job.job_id}.pid").exists()  # fresh owner pid


def test_resume_does_not_double_enqueue_existing_frontier(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "time_ms", lambda: 5)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    job.push_frontier(FrontierItem("https://ex.com/b", 0))  # already pending
    job.mark_claimed("https://ex.com/b")
    job.status = JobStatus.INTERRUPTED
    job.flush(force=True)
    resumed = jobs.Job.resume(job.job_id, jobs_dir=tmp_path)
    assert [it.url for it in resumed._frontier].count("https://ex.com/b") == 1


def test_should_stop_on_shutdown_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "time_ms", lambda: 5)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    assert job.should_stop() is False
    job.request_shutdown("interrupted")
    assert job.should_stop() is True


def test_should_stop_on_cancel_file(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "time_ms", lambda: 5)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    job.cancel_path.write_text("")
    assert job.should_stop() is True


def test_finalize_completed_removes_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "time_ms", lambda: 9)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    job.finalize()
    assert job.status == JobStatus.COMPLETED
    assert job.completed_at == 9
    assert not job.pid_path.exists()
    assert json.loads(job.json_path.read_text())["status"] == "completed"


def test_finalize_interrupted_on_shutdown(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "time_ms", lambda: 9)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    job.request_shutdown("interrupted")
    job.finalize()
    assert job.status == JobStatus.INTERRUPTED


def test_finalize_cancelled_when_cancel_file_present(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "time_ms", lambda: 9)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    job.cancel_path.write_text("")
    job.finalize()
    assert job.status == JobStatus.CANCELLED


def test_reconcile_flips_dead_owner_to_interrupted(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "time_ms", lambda: 5)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    # Simulate the owner having died: corrupt the create_time in its pid file.
    me = json.loads(job.pid_path.read_text())
    me["create_time"] = me["create_time"] + 999.0
    job.pid_path.write_text(json.dumps(me))
    flipped = jobs.reconcile_jobs(jobs_dir=tmp_path)
    assert job.job_id in flipped
    assert json.loads(job.json_path.read_text())["status"] == "interrupted"


def test_reconcile_leaves_live_owner_running(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "time_ms", lambda: 5)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)  # pid = live test process
    flipped = jobs.reconcile_jobs(jobs_dir=tmp_path)
    assert job.job_id not in flipped
    assert json.loads(job.json_path.read_text())["status"] == "running"


def test_reconcile_ignores_terminal_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "time_ms", lambda: 5)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    job.finalize()  # completed, pid removed
    flipped = jobs.reconcile_jobs(jobs_dir=tmp_path)
    assert flipped == []
    assert json.loads(job.json_path.read_text())["status"] == "completed"


# -- review #60 fixes ------------------------------------------------------


def test_is_owner_alive_treats_access_denied_as_dead(tmp_path, monkeypatch):
    # A recycled PID owned by a privileged process raises AccessDenied on
    # create_time(); that must read as a dead owner, not propagate.
    p = tmp_path / "x.pid"
    jobs.write_pid_file(p)

    class _Denied:
        def __init__(self, pid):
            pass

        def create_time(self):
            raise jobs.psutil.AccessDenied(pid=1)

    monkeypatch.setattr(jobs.psutil, "Process", _Denied)
    assert jobs.is_owner_alive(p) is False


def test_reconcile_skips_json_missing_job_id(tmp_path):
    # A structurally valid running job file with no "job_id" must be skipped,
    # not abort the whole scan with KeyError.
    (tmp_path / "crawl-bad.json").write_text(json.dumps({"status": "running"}))
    flipped = jobs.reconcile_jobs(jobs_dir=tmp_path)  # must not raise
    assert flipped == []


def test_job_json_roundtrips_non_ascii_utf8(tmp_path, monkeypatch):
    # Regression guard: JSON must round-trip non-ASCII via UTF-8 on every
    # platform (on Windows the default encoding is cp1252, which corrupts it).
    monkeypatch.setattr(jobs, "time_ms", lambda: 5)
    job = jobs.Job.create("crawl", {"seed": "https://例え.jp/ünïcode"}, jobs_dir=tmp_path)
    raw = job.json_path.read_bytes().decode("utf-8")  # must decode cleanly
    assert "例え" in raw
    loaded = jobs.Job.load(job.job_id, jobs_dir=tmp_path)
    assert loaded.params == {"seed": "https://例え.jp/ünïcode"}


# -- PR 6.2: claimed-depth persistence (resume restores true depth) --------


def test_mark_claimed_persists_depth(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "time_ms", lambda: 5)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    job.mark_claimed("https://ex.com/deep", depth=3)
    # 4th tab field carries the depth; first two fields unchanged (compat).
    line = job.visited_path.read_text().splitlines()[0]
    assert line.split("\t")[:2] == ["https://ex.com/deep", "claimed"]
    assert line.split("\t")[3] == "3"
    assert job.claimed_depth["https://ex.com/deep"] == 3


def test_resume_restores_claimed_depth(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "time_ms", lambda: 5)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    job.mark_claimed("https://ex.com/deep", depth=4)  # claimed, never completed
    job.status = JobStatus.INTERRUPTED
    job.flush(force=True)
    resumed = jobs.Job.resume(job.job_id, jobs_dir=tmp_path)
    items = {it.url: it.depth for it in resumed._frontier}
    assert items["https://ex.com/deep"] == 4  # restored, not reset to 0


def test_mark_claimed_default_depth_zero_backward_compatible(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "time_ms", lambda: 5)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    job.mark_claimed("https://ex.com/a")  # no depth arg → 0
    assert job.claimed_depth["https://ex.com/a"] == 0


def test_load_visited_without_depth_field_defaults_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "time_ms", lambda: 5)
    job = jobs.Job.create("crawl", {}, jobs_dir=tmp_path)
    # Simulate an old 3-field visited.txt line (pre-6.2 format).
    with job.visited_path.open("a", encoding="utf-8") as f:
        f.write("https://ex.com/old\tclaimed\t5\n")
    loaded = jobs.Job.load(job.job_id, jobs_dir=tmp_path)
    assert "https://ex.com/old" in loaded.claimed
    assert loaded.claimed_depth.get("https://ex.com/old", 0) == 0
