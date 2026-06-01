# Crawl CLI 设计（v0.3 PR 6.3）

Third and final slice of PR 6 (crawl): **6.1 robots → 6.2 engine → 6.3 CLI (this)**.
6.2 left the engine (`run_crawl`) and job layer (`jobs.py`) driveable only from
Python with a `CrawlParams` + `Job`. 6.3 is the user-facing surface: five
subcommands, foreground execution, signals, and startup self-heal.

## 1. 范围

**做（本 PR）**
- 子命令：`crawl` / `crawl-status` / `crawl-resume` / `crawl-cancel` / `jobs`。
- 启动时 `reconcile_jobs()`（死主进程的 `running` job → `interrupted`）。
- 前台爬取 + SIGINT/SIGTERM 优雅中断（finalize `interrupted`，可 resume）。
- flag → `CrawlParams` 解析，复用 §3 缓存 flag 真值表 + `CACHE_FLAG_CONFLICT`。
- 新错误码 `JOB_ALREADY_RUNNING`，守护对运行中 job 的 resume。
- 在 crawl/crawl-status 中以 info 级 `CRAWL_MAX_PAGES` 暴露命中 `--max-pages`。

**不做（推迟）**
- `--async` 后台分离 / `--wait`。经与用户确认选「方案 B：仅前台」：最小正确切片、
  完全离线可测、信号/resume/cancel 全保留；引擎与 job 层本就为 async 设计，
  `--async` 日后可无重构补上（YAGNI）。

## 2. 执行模型（方案 B）

`crawl URL` 直接 `await run_crawl(...)` 阻塞到终态，再打印最终 job 摘要 —— 与
`fetch`/`search`/`map` 一致的「一条命令一个结果」。`crawl-resume` 同样前台运行。
没有派生后台进程，因此没有 double-fork/setsid/`/dev/null` stdio 这条脆弱且难测的
路径。

权衡（取舍已记录在 PR 描述）：大爬虫占住终端；`crawl-cancel` 一个运行中的 job 只在
它正跑于别处某前台进程时即时生效。两者都可用 shell `&` / 多终端绕过,且不阻碍未来的
`--async`。

## 3. 子命令

| 命令 | 行为 | 关键错误码 |
|---|---|---|
| `crawl <url> [flags]` | 建 job、前台 BFS、打印摘要 | `CACHE_FLAG_CONFLICT`、info `CRAWL_MAX_PAGES` |
| `crawl-status <id>` | 只读 `<id>.json`，打印 status+progress+errors_tail | `JOB_NOT_FOUND` |
| `crawl-resume <id>` | 重开 interrupted job，前台续爬 | `JOB_NOT_FOUND`、`JOB_NOT_RESUMABLE`、`JOB_ALREADY_RUNNING` |
| `crawl-cancel <id>` | 写 `.cancel`；空闲 job 立即 finalize cancelled | `JOB_NOT_FOUND` |
| `jobs` | 列全部 job（JSON-only，最新在前） | — |

所有命令遵守既有契约：stdout 一个 JSON 对象，`ok:true`→exit 0 / `ok:false`→exit 1，
经 `_safe_run` 把 `FetchError`/未捕获异常统一成 `{ok:false,error_code,error_detail}`。

## 4. 缓存解析

复用 `_add_cache_flags` / `_validate_cache_flags` / `_resolve_cache_kwargs`
（PR 2.4）。与 fetch 不同，crawl 默认 **cache on, 1h**：当未给任何缓存 flag 时，
`_resolve_crawl_params` 把 `max_age_ms` 补成 `crawl._DEFAULT_MAX_AGE_MS`（1h），
于是一小时内的重爬复用已抓结果。`--no-cache` / `--cache-only` / `--max-age` / `--no-store`
覆盖默认，真值表冲突仍返回 `CACHE_FLAG_CONFLICT`。

## 5. 信号与生命周期

`install_signal_handlers(loop, job)`（在 `crawl.py`）把 SIGINT/SIGTERM 接到
`job.request_shutdown("interrupted")` —— **不抛异常**：BFS 循环轮询
`job.should_stop()` 在下一轮退出，`finalize()` 落成 `interrupted`（可 resume）。
无法装信号处理器的环境（Windows ProactorLoop、非主线程）降级为默认行为而非报错。

`crawl-cancel` 写 `.cancel` 文件：运行中的 job 由其自身进程轮询 `should_stop()` →
停止 → `finalize()`（`.cancel` 存在则胜出为 `CANCELLED`）；空闲 job（owner 不活）
直接 `finalize()` 落成 `CANCELLED`。

## 6. 启动 reconcile 与 resume 守护

`main()` 先 `reconcile_jobs()`：把死 owner 的 `running` job 翻成 `interrupted`
（create_time 匹配，免疫 PID 复用）。因此 `crawl-resume` 看到的 `running` 必是
**活着的** owner → 抛 `JOB_ALREADY_RUNNING`（避免双跑同一 job）；`interrupted` 才
经 `Job.resume` 重开。非 interrupted 的其它终态由 `Job.resume` 抛 `JOB_NOT_RESUMABLE`。

## 7. 数据层增量（`jobs.py`）

- `read_status(job_id)`：只读 `<id>.json`，不碰 visited/frontier 侧文件 —— 对 5 万页
  job 的状态查询不必吞兆字节 visited.txt。缺失/损坏 → `JOB_NOT_FOUND`。
- `list_jobs()`：JSON-only 汇总，最新在前；无 `job_id` 或 JSON 损坏的文件跳过而非致命。

## 8. CRAWL_MAX_PAGES 的暴露

不改 6.2 引擎：摘要从持久化的 `params.max_pages` 与 `progress.pages_fetched` 推断 ——
`status==completed && pages_fetched >= max_pages` 即附 `error_code=CRAWL_MAX_PAGES`
+ `note`（仍 `ok:true`，info 级 expected branch）。crawl 与 crawl-status 共用
`_crawl_payload`，两处一致。

## 9. 测试（`tests/test_crawl_cli.py`，13，全离线）

同 test_crawl.py 的 canned-page Router 收发，`jobs.paths.JOBS` 指向 tmp：crawl 端到端
摘要、max-pages note、缓存冲突；crawl-status 进度 + not-found；jobs 列举；cancel
（空闲 finalize / 运行中仅请求 / 终态 no-op）；resume 守护（JOB_ALREADY_RUNNING）+
resume 跑完 interrupted；`main()` 启动 reconcile 翻死 job；信号处理器置 interrupted。

## 10. 验证

- `pytest tests/test_crawl_cli.py -q` 13 green
- 全套 569 passed（6.2 后为 556，+13）零回归
- `ruff check` 本 PR 文件 clean
- 真机冒烟：`jobs` / `crawl-status <missing>` / `crawl --help` / `crawl --no-cache --cache-only`
  —— JSON 契约、exit code、错误码均正确
