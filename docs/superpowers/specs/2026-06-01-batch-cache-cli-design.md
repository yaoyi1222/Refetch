# batch-fetch + cache CLI 设计（v0.3 PR 7）

v0.3 的收尾 PR。把"本地 firecrawl"四件套补齐最后一件(`batch-fetch`)并暴露缓存
运维命令(`cache stats` / `cache clear`)。只依赖 PR 2(cache,已合入),与 crawl
三连(PR 6.x)独立。

## 1. 范围

- `batch-fetch URL... | --urls-file FILE`:并发抓取一组已知 URL,一次性返回。
- `cache stats`:缓存容量 / host 数 + legacy `~/.lightcrawl/dumps/` 占用。
- `cache clear [--older-than DUR | --host HOST | --all]`:受控清理。
- `Cache.clear_all()`:显式全清(复用 `_delete_where("1=1")`)。
- 新错误码 `CACHE_CLEAR_NO_SCOPE`。

## 2. batch-fetch(`src/lightcrawl/batch.py`)

不走 job 框架(无 resume/cancel/落盘):URL 集已知,并发抓取,全部完成后一次返回。
每个 URL 经 `Router.fetch` —— 继承 L1→L2→L3 escalation、SSRF guard、Router cache
切面(PR 2.3)。**像 crawl.py 一样,不碰 `fetch_http`/`Cache`**。

- `BatchParams`:concurrency=8、output_format、profile、max_inline_tokens、timeout_ms
  + 缓存字段。缓存默认 **on, 1h**(design §3 / §5.7),CLI 把 flag 解析进这些字段。
- `run_batch_fetch(urls, params, router)`:`asyncio.Semaphore(concurrency)` 限流,
  每个 fetch 各自 try/except(单条崩溃不沉没整批),`gather(return_exceptions=True)`
  做兜底。返回 `{ok, count, ok_count, failed_count, results}`。
- **顶层 `ok=true` = "批次完成",不等于"全部成功"**(honesty contract #5):即使每条
  `results` 都 `ok:false`,顶层仍 `ok:true`,调用方必须读 `ok_count`/`failed_count`。
- 空 URL 列表 → `count:0` + `notes` 说明原因(honesty contract #4),仍 `ok:true`。

**取舍**:不去重(用户给的列表照抓,重复项由 cache 吸收);不设 URL 数硬上限
(semaphore 已限并发,内存可控)。

## 3. cache stats

`Cache().stats()`(entry_count / total_bytes / payload_bytes / dump_bytes /
screenshot_bytes / hosts)+ `legacy_dumps_usage()`。后者报告 v0.2 旧目录
`~/.lightcrawl/dumps/` 的占用字节,提示用户手动 `rm`(design §615)。

## 4. cache clear —— 销毁性,显式范围(方案 A)

经与用户确认选**方案 A**:CLI 单 JSON 契约无法交互确认,因此**裸 `cache clear`
拒绝执行**。

- 范围互斥:`--older-than DUR` / `--host HOST` / `--all` 三选一。
  - 0 个 → `CACHE_CLEAR_NO_SCOPE`(ok:false, exit 1):全清必须刻意。
  - >1 个 → `CACHE_FLAG_CONFLICT`(三者互斥,且 `gc()` 一次只走一种模式)。
- 校验在 handler 里做、返回 JSON 信封,**不用 argparse 的 mutex group**(后者打到
  stderr + exit 2,会破坏单 JSON 契约 —— 沿用 `_validate_cache_flags` 的既有理由)。
- 映射:`--all`→`Cache.clear_all()`;`--older-than`→`gc(older_than_ms=)`;
  `--host`→`gc(host=)`。输出 `{ok, scope, deleted_entries, freed_bytes}`。

`Cache.clear_all()` 复用 `_delete_where(conn, "1=1", ())`,让侧文件清理逻辑只有一处。

## 5. CLI 接线

复用既有模式:`_cmd_*`/`_run_*`/`_safe_run`、单 JSON、exit 0/1。
- `batch-fetch`:positional `urls` + `--urls-file`(一行一 URL,`#` 注释 / 空行忽略,
  解析器 `_read_urls_file` 在 cli)。缓存 flag 复用 §3 真值表;与 crawl 一样默认
  cache-on/1h,经新抽出的 `_resolve_cached_cache_kwargs`(crawl 同步改用它,DRY)。
- `cache`:子命令组(同 `auth`),含 `stats` / `clear`。

## 6. 测试(全离线)

- `tests/test_batch_fetch.py`(9):全成功计数、部分失败仍 ok:true、**全失败仍 ok:true
  且 ok_count=0**、空列表 + notes、**并发上限(线程探针 peak≤concurrency)**、cache 命中
  跳过网络、CLI positional / urls-file / 缓存冲突。
- `tests/test_cache_cli.py`(6):stats 计数 + legacy 字段、clear 无范围被拒、范围冲突、
  `--all` 全清、`--host` 只清该 host、`--older-than`(推进时钟)。

## 7. 验证

- `pytest tests/test_batch_fetch.py tests/test_cache_cli.py -q` 15 green
- 全套 **584 passed** 零回归
- `ruff check` 本 PR 文件 clean
- 真机冒烟:`cache stats`(含真实 legacy dumps 占用)、`cache clear`(无范围拒绝 / 冲突)、
  `batch-fetch`(空列表 notes)—— JSON 契约、exit code、错误码均正确

## 8. 推迟(明确记录,非遗漏)

- 启动时对 legacy `~/.lightcrawl/dumps/` 打 stderr 警告(design §611):PR 7 行只点名
  "占用报告",已由 `cache stats` 满足;启动 stderr 提示与单 JSON-on-stdout 契约正交,
  留待单独处理。
- ETag/Last-Modified 条件请求(PR 3):受 R1 探针 gate,独立于本 PR。
