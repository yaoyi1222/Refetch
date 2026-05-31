# PR 6.2 — `crawl.py` (BFS crawl engine)

> v0.3 tracker #22 · design.md §5.5 · 2026-05-31

Second slice of PR 6 (crawl): **6.1 robots → 6.2 engine (this) → 6.3 CLI**.
Built on PR 5 (`jobs.py`) and PR 6.1 (`robots.py`); stacked on the 6.1 branch.

## 1. 范围与边界

`crawl.py` is the BFS orchestration layer *above* the Router. Every page is
fetched via `Router.fetch`, inheriting L1→L2→L3 escalation, the SSRF guard, and
the Router cache aspect (PR 2.3). The engine never touches `fetch_http` /
`fetch_browser` / `Cache` directly.

**纳入 6.2:** `CrawlParams`, `fetch_one`, `run_crawl`, the domain/path/outlink
helpers, `CRAWL_MAX_PAGES` error code, and the `jobs.py` claimed-depth change.

**不在 6.2（→ 6.3）:** the `crawl` / `crawl-status` / `crawl-resume` /
`crawl-cancel` / `jobs` subcommands, signal handlers, `--async`/`--wait`, CLI
flag parsing, startup reconcile, `JOB_ALREADY_RUNNING`. 6.2 is driven directly
with a `CrawlParams` + a `Job`.

## 2. 关键架构决策

- **Frontier = the Job's persistent queue**, not a separate `asyncio.Queue`. The
  main loop is single-coroutine, so the frontier is never accessed concurrently
  (concurrency lives only in the gathered `fetch_one` tasks). Using
  `job.push_frontier` / `job.pop_frontier` makes the frontier crash-safe and
  makes `resume` work with no extra bookkeeping (the restored frontier is used
  as-is; the seed is only enqueued when the frontier is empty).
- **Cache via the Router aspect, not manual lookup/store.** `fetch_one` sets the
  four cache fields on `FetchRequest`; the Router does the lookup/store and
  returns `cache_hit`. crawl.py does not import `Cache`. Supersedes design §5.5
  #8's pre-aspect pseudocode.
- **robots checks run serially in the main loop**, never inside the gathered
  fetch tasks, so `RobotsCache` needs no concurrency guard (resolves the PR 6.1
  forward note).
- **Cache-hit accounting:** `pages_fetched` counts every successful page (cache
  or network); `pages_skipped_cache` is the subset served from cache (= network
  calls avoided). So the "≥95% cache hit on re-crawl" acceptance reads as
  `skipped_cache / fetched ≥ 0.95`. This keeps `job.record` cache-agnostic
  (no jobs.py change) rather than the literal §5.5 #8 "hit ⇒ skip" wording.

## 3. `CrawlParams`

`seed`, `max_depth=3`, `max_pages=100`, `include_paths=()`, `exclude_paths=()`,
`allow_subdomains=False`, `crawl_entire_domain=False`, `ignore_robots=False`,
`ignore_query_parameters=False`, `concurrency=4`, `user_agent="*"`,
`output_format="markdown"`, `profile=None`, plus cache fields
`max_age_ms=3_600_000` (1h), `cache_only=False`, `store_in_cache=True`,
`no_cache=False`. The CLI (6.3) resolves flags into these; `--no-cache` is the
authoritative override (`no_cache=True`).

## 4. 主循环 `run_crawl(params, job, router) -> None`

```
robots = RobotsCache(router, ignore=params.ignore_robots, user_agent=params.user_agent)
if not job._frontier: job.push_frontier(FrontierItem(seed, 0))   # resume skips this
sem = Semaphore(concurrency); in_flight = set()
while not job.should_stop():
    if not job._frontier and not in_flight: break
    while job._frontier and len(in_flight) < concurrency:
        item = job.pop_frontier()
        canon = canonicalize_url(item.url, ignore_query=params.ignore_query_parameters)
        if canon in job.claimed: continue                       # dedup / cycle guard
        if not _domain_allows(item.url, params): continue        # out of scope, not counted
        if item.depth > 0 and not _path_allows(item.url, params):# seed always fetched
            job.progress.pages_skipped_filter += 1; continue
        if not await robots.allows(item.url):
            job.progress.pages_skipped_robots += 1; continue
        job.mark_claimed(canon, item.depth)
        in_flight.add(create_task(fetch_one(item, params, router, sem)))
    if not in_flight: break
    done, in_flight = await wait(in_flight, FIRST_COMPLETED)
    for t in done:
        r = t.result(); job.record(r)
        if r.get("cache_hit"): job.progress.pages_skipped_cache += 1
        if r.get("ok") and r["depth"] < params.max_depth:
            for link in _outlinks(r, params): job.push_frontier(FrontierItem(link, r["depth"]+1))
    if job.progress.pages_fetched >= params.max_pages: break
finally: job.finalize()
```

### 边界条件（design §5.5）
1. **Dedup / cycle guard** — `claimed` keyed on canonical URL.
2. **Domain boundary** — `_domain_allows`: host-equal default; eTLD+1 under
   `--allow-subdomains` / `--crawl-entire-domain`. Out-of-scope links are not
   enqueued and not counted.
3. **robots** — per-host via `RobotsCache`; disallow → `pages_skipped_robots`,
   not a failure. `--ignore-robots-txt` short-circuits.
4. **include/exclude** — `re.search` on the **raw** path+query; exclude wins
   over include (fail-closed); a hit → `pages_skipped_filter`. The seed (depth 0)
   is exempt — it is the entry point and is always fetched.
5. **Fault isolation** — `fetch_one` wraps the fetch; any exception becomes a
   failed-page result (recorded, not swallowed), so one page can't abort the
   crawl.
6. **outlinks** — reuse `content._extract_links` (via `metadata.links`), filtered
   by `_domain_allows`. Path filtering happens at pop time so excluded links are
   counted.
7. **`max_pages` soft cap** — checked after each completed batch; actual count
   may overshoot by `< concurrency`. No lock.
8. **cache** — Router aspect; `cache_hit` → `pages_skipped_cache`.
9. **`--no-cache`** — authoritative; resolved by the CLI into `no_cache=True`.

## 5. `jobs.py` 改动（claimed-depth 持久化）

`mark_claimed(url, depth=0)` writes a 4th tab field `url\tclaimed\tts\tdepth`.
`_load_visited` parses it into `claimed_depth: dict[str, int]`. `resume()`
re-enqueues `claimed - completed` at `claimed_depth.get(url, 0)` instead of 0, so
a depth-limited crawl resumed mid-flight doesn't re-expand in-flight URLs as
seeds. Backward-compatible: old 3-field lines parse fine (depth defaults to 0);
existing tests only assert the first two fields.

## 6. 错误处理

- `fetch_one` is the per-page fault boundary (errors recorded as failed pages).
- `CRAWL_MAX_PAGES` added to `errors.py` — info-level "expected branch"
  (design §7); 6.2 defines it, 6.3 surfaces it in crawl-status. Hitting the cap
  finalizes the job `completed`.
- No raised errors at the engine boundary; `run_crawl` mutates the Job and always
  `finalize()`s (terminal state read from the Job by 6.3).

## 7. 测试（`tests/test_crawl.py`, 13, 全离线 + `tests/test_jobs.py` +4）

Canned pages served through the real Router + content pipeline (`fetch_http`
patched, bodies padded past the browser-escalation threshold), driving a real
`Job`: BFS reachability, max-depth, max-pages soft cap, domain boundary (default
+ subdomains), exclude/include path filters (+ counts), robots disallow/ignore
(+ count), cycle dedup, fault isolation, cache-hit accounting (tmp `Cache`),
finalize. jobs: claimed-depth persistence + resume restores depth + backward
compat.

## 8. 验证

- `pytest tests/test_crawl.py tests/test_jobs.py tests/test_robots.py -q` green
- full suite zero regressions
- `ruff check` clean

## 9. 下游（6.3）

CLI subcommands, signal handlers (`install_signal_handlers`), `--async`/`--wait`,
`.cancel` writing, flag→`CrawlParams` resolution + cache truth-table validation,
startup `reconcile_jobs`, `JOB_ALREADY_RUNNING`, surfacing `CRAWL_MAX_PAGES`.
