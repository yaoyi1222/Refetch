<div align="center">

# lightcrawl

**开源、本地、轻量的 firecrawl 替代品。**

反爬绕过、JS 渲染、登录会话、声明式浏览器动作、PDF 解析、截图、多后端搜索 — 全部在一个本地 CLI 中。无需云端、核心功能无需 API key、无按次计费。

[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-597%20passing-brightgreen.svg)](tests/)
[![Version](https://img.shields.io/badge/version-0.3.0-blue.svg)](CONTRIBUTING.md)

[English](README.md) · [中文](README_zh.md) · [CONTRIBUTING](CONTRIBUTING.md)

</div>

---

## lightcrawl 是什么？

lightcrawl 是一个本地 CLI，任何 AI Agent（Claude Code、Codex、Gemini CLI、Copilot CLI）通过 shell 调用它来抓取和搜索网页。它是 Agent 内置 `WebFetch` / `WebSearch` 的升级替代方案，能在现代 Web 上真正工作。

定位为 **开源、本地、免费的 firecrawl 替代品** — 对标 firecrawl `/scrape` 的核心参数，同时完全在你自己机器上运行。

### 内置工具做不到的事

| 问题 | 内置 WebFetch | lightcrawl |
|---|---|---|
| Cloudflare 保护的页面 | ❌ TLS 指纹不匹配，返回挑战页 | ✅ curl_cffi + Playwright 隐身，自动逐级升级（L1→L2→L3） |
| JavaScript 渲染的 SPA | ❌ 空壳 `<div id="root">` | ✅ 无头 Chromium 执行 JS，等待选择器 |
| 登录墙后的页面（X、LinkedIn） | ❌ 只能拿到登录页 | ✅ 通过 `auth login` 保存登录态，复用 session |
| 整页 dump（导航、侧边栏、广告） | ❌ 浪费 60–95% token | ✅ 自动定位 `<main>`/`<article>`，去噪清洗 |
| PDF 下载链接 | ❌ 静默失败 | ✅ pypdf 逐页文本提取 |
| 单一搜索后端 | ❌ 限速 = 彻底不可用 | ✅ Brave + Serper + Tavily 自动故障转移 |

---

## 功能亮点

### 抓取（对标 firecrawl `/scrape`）

- **三层逐步升级** — L1 `curl_cffi`（Chrome 120 TLS 指纹）→ L2 Playwright + stealth → L3 已保存登录态。每请求只升级到必要层级。
- **内容管线** — 自动定位 `<main>`/`<article>`，剥离不可见元素，返回结构化 `headings` + 行号。节省 30–90% token。
- **输出格式** — markdown（默认）、html、text、screenshot（全页面 PNG）、markdown+screenshot、links（JSON）、images（JSON）。
- **浏览器动作** — `click`、`write`、`press`、`wait`、`scroll`、`screenshot` 在 Playwright 上下文中于页面加载后、内容提取前执行。中间截图可复用，稀疏索引。
- **链接/图片始终抓取** — `metadata.links`（`{url, text, rel}`）和 `metadata.images`（`{url, alt, width?, height?}`）对所有成功请求生效，不依赖 output_format。
- **PDF 解析** — `.pdf` URL 自动路由到 pypdf。逐页文本提取，magic-byte 回退检测，返回 `metadata.num_pages` / `metadata.content_length`。
- **移动端模拟** — iOS Safari impersonate profile（UA + TLS 指纹 + 视口），L1 和 L2 同步切换。
- **自定义 headers + 标签过滤** — `--header KEY=VAL`（可重复），`--include-tag` / `--exclude-tag` firecrawl 风格 DOM 标签范围控制。

### 搜索

- **三个后端** — Brave（独立索引，免费 2k/月）、Serper（Google SERP）、Tavily（LLM 优化 snippet）。自动故障转移。
- **搜索+阅读** — `search-and-read` 一次调用完成搜索 + 并发抓取 top N。
- **结构化结果** — 富 snippet、域名提示、每条结果的 `fetch_hint`。

### 认证

- **登录 Profile** — `auth login` 弹出有头 Chromium，用户手动登录（密码永不接触工具），session 保存复用。
- **域名绑定** — Profile 绑定到登录 URL 的 eTLD+1。
- **SSRF 防护** — 默认拦截 loopback、私有网段、link-local IP。

### v0.3 — 本地 firecrawl（`map`、`crawl`、缓存）

- **`map`** — 站内 URL 发现，sitemap 优先（`robots.txt` → `/sitemap.xml` → `/sitemap_index.xml`），无 sitemap 时回退首页 `<a>` 链接。`lightcrawl map https://fastapi.tiangolo.com/`。
- **`crawl`** — BFS 多页抓取，append-only 落盘 job 存储、崩溃安全恢复、可取消：`crawl`、`crawl-status`、`crawl-resume`、`crawl-cancel`、`jobs`。逐 host 强制 `robots.txt`，支持 `--include-path`/`--exclude-path`（正则）、`--max-pages`/`--max-depth`。
- **`batch-fetch`** — 经共享 Router/缓存并行抓取多个 URL，聚合为单个 JSON；单个失败不影响其余结果。
- **本地缓存** — 通过 `--max-age <时长>` 显式开启(比 `<时长>` 新鲜则用缓存正文,否则实时抓取并写入)。`--no-store` 只读不写、`--cache-only` 离线只读、`--no-cache` 完全绕过。key 含规范化 URL **+ profile**,认证/非认证抓取互不串扰。裸 `fetch` 默认行为不变 — 不带 flag 既不读也不写缓存。
- **条件请求** — 已过期但带 `ETag`/`Last-Modified` 的缓存项,在 L1（指纹伪装）路径发条件 GET；`304` 复用缓存正文并刷新新鲜度（`revalidated: true`）。
- **`cache stats` / `cache clear`** — 查看缓存大小 + host 分布（含 legacy dumps），或全量/按 host 清理。

---

## 快速开始

```bash
git clone https://github.com/yaoyi1222/lightcrawl.git
cd lightcrawl
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev,bench]"
.venv/bin/playwright install chromium
```

```bash
# 可选：搜索后端（任选一个）
export BRAVE_SEARCH_API_KEY=...
export SERPER_API_KEY=...
export TAVILY_API_KEY=...
```

### 接入 Agent

```bash
# Claude Code（项目级）
mkdir -p .claude/skills/lightcrawl
cp skills/lightcrawl/SKILL.md .claude/skills/lightcrawl/SKILL.md

# Claude Code（用户级）
mkdir -p ~/.claude/skills/lightcrawl
cp skills/lightcrawl/SKILL.md ~/.claude/skills/lightcrawl/SKILL.md
```

其他 Agent 同样指向 `skills/lightcrawl/SKILL.md`。无需常驻进程、无需 MCP server、无需 transport 配置。

```bash
.venv/bin/lightcrawl list-backends
.venv/bin/lightcrawl fetch https://example.com/
```

---

## 架构

```
cli.py ─── Router (router.py) ────────► fetch_http.py     L1: curl_cffi + TLS 指纹伪装
       │                               ► fetch_browser.py  L2: Playwright + stealth
       │                               ► fetch_browser.py  L3: L2 + 已保存 storage_state
       │                               ► fetch_pdf.py      PDF: pypdf 提取
       │                               ► actions.py        Actions: click/write/scroll/...
       │
       └── SearchService ─── owns ───► Router（并行抓取）
```

每条命令向 stdout 输出一个 JSON 对象，成功 exit 0，失败 exit 1。Skill 文件（`skills/lightcrawl/SKILL.md`）是 Agent 读取的规范参考。

### Token 效率

| 网站 | 内置 WebFetch | lightcrawl 默认 | 加 `--selector` | 最多节省 |
|---|---|---|---|---|
| **Wikipedia** Python 词条 | 58,000 chars | 40,000 | 40,000 | **31%** |
| **GitHub** psf/requests | 17,500 chars | 8,040 | 2,069 | **90%** |
| **Django docs** 概览 | 22,742 chars | 14,695 | 12,867 | **52%** |
| **Python docs** 教程 | 15,224 chars | 22,160* | — | — |

\*lightcrawl 拿到更多内容 — Playwright 执行 JS 后加载了完整的侧边栏导航。

---

## 命令

| 命令 | 用途 |
|---|---|
| `lightcrawl fetch <url>` | 自动策略升级抓取。支持 `--output-format`、`--selector`、`--actions`、`--mobile`、`--header`、`--include-tag`/`--exclude-tag`、`--remove-base64-images`、下方缓存 flag，screenshot / links / images 输出。 |
| `lightcrawl map <url>` | 站内 URL 发现（sitemap 优先，首页回退）。`--search`、`--limit`。 |
| `lightcrawl crawl <url>` | BFS 多页抓取。`--max-pages`、`--max-depth`、`--include-path`/`--exclude-path`（正则）、`--no-cache`。 |
| `lightcrawl crawl-status` / `crawl-resume` / `crawl-cancel` / `jobs` | 查看、恢复（崩溃安全）、取消 crawl，或列出所有 job。 |
| `lightcrawl batch-fetch <url...>` | 并行抓取多个 URL；聚合的逐 URL JSON。 |
| `lightcrawl cache stats` / `cache clear` | 查看或清理本地抓取缓存。 |
| `lightcrawl search <query>` | Web 搜索，结构化结果 + 每条结果的 `fetch_hint`。 |
| `lightcrawl search-and-read <query>` | 搜索 + 并发抓取 top N。 |
| `lightcrawl list-backends` | 报告已配置的搜索后端。 |
| `lightcrawl auth login <profile> <url>` | 有头浏览器手动登录，保存 profile。 |
| `lightcrawl auth list` / `show` / `revoke` | 管理已保存的登录 profile。 |

缓存 flag（用于 `fetch` / `batch-fetch`）：`--max-age <时长>` 更新鲜则命中并写入（`30m`、`1h`、`24h`）、`--no-store` 只读不写、`--cache-only` 离线命中否则 `CACHE_MISS`、`--no-cache` 绕过。

完整 flag：`lightcrawl <subcmd> --help`，或 `lightcrawl --version`。

---

## 搜索后端

内置三个可插拔后端。默认 **Brave → Serper → Tavily**（选第一个已配置的）。

| 后端 | 强项 | 何时选 |
|---|---|---|
| **Brave** | 独立索引，免费 2k/月 | 默认。绝大多数查询 |
| **Serper** | Google SERP 排名 | Brave 配额用尽或遗漏时 |
| **Tavily** | LLM 优化 snippet（200–500 字） | 长 snippet 直接答 ~70% 查询，省去 fetch |

加新后端约 120 行 — 参考 `src/lightcrawl/search/backends/brave.py`。

---

## vs firecrawl

lightcrawl 对标 firecrawl `/scrape` 端点，并自 v0.3 起覆盖 `/map` 与 `/crawl`。LLM 提取仍延后。

| firecrawl 端点 / 参数 | lightcrawl 状态 |
|---|---|
| `/map`（URL 发现） | ✅ `lightcrawl map`（v0.3） |
| `/crawl`（多页 BFS） | ✅ `lightcrawl crawl` + 生命周期子命令（v0.3） |
| `url` | ✅ |
| `formats: [markdown, html, rawHtml, screenshot, links, ..., images]` | ✅ markdown、html、text、screenshot、markdown+screenshot、links、images |
| `headers` | ✅ `--header KEY=VAL`（可重复） |
| `includeTags` / `excludeTags` | ✅ `--include-tag` / `--exclude-tag` |
| `waitFor`（ms） | ✅ `--wait-for-network-idle` |
| `actions`（click、write、screenshot、scroll、wait、press） | ✅ `--actions '[...]'` |
| `mobile` | ✅ `--mobile`（iOS Safari impersonate） |
| `onlyMainContent` | ✅ 默认行为（自动定位 `<main>`/`<article>`） |
| `removeBase64Images` | ✅ **v0.3 起默认 `True`**（只剥离 `data:` URI 图片；外链 `<img>` 保留进 markdown）。用 `--no-remove-base64-images` 恢复 v0.2 行为。 |
| `location`（国家） | 延后到 v0.4+ |
| `extract`（LLM 结构化） | 延后到 v0.5 |
| `blockAds` | 延后到 v0.4+ |
| 云端托管 | ❌ — 本地运行（你的 IP、你的 cookie、不过第三方云） |
| 免费 | ✅ — MIT 协议，核心抓取无需 API key |

<div align="center">

**lightcrawl = 免费、本地的 firecrawl `/scrape`，附带反爬绕过和登录会话。**

</div>

---

## 配置

`~/.lightcrawl/config.toml`（可选）：

```toml
[ssrf]
extra_allowlist = ["internal.example.com"]

[search]
default_backend = "brave"
```

| 环境变量 | 用途 |
|---|---|
| `BRAVE_SEARCH_API_KEY` | Brave 搜索（免费 2k/月） |
| `SERPER_API_KEY` | Serper Google SERP 代理 |
| `TAVILY_API_KEY` | Tavily LLM 优化搜索 |

---

## 安全

- Profile 以 0600 权限的明文 `storage_state` JSON 存储（Playwright 官方惯例）。
- `auth show` 只返回元信息 — 永不泄露 cookie。
- Profile 绑定到登录 URL 的 eTLD+1（绑定到 `x.com` 的 `twitter` profile 不能用于 `attacker.com`）。
- SSRF 防护默认拦截 loopback、私有网段、云元数据 IP。
- 抓取内容视为数据；skill 指导 Agent 忽略页面内的指令性文字。

---

## 许可证

MIT — 见 [`LICENSE`](LICENSE)。
