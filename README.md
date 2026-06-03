<div align="center">

# lightcrawl

**Open-source, local, lightweight firecrawl alternative.**

Anti-bot bypass, JS rendering, login sessions, declarative browser actions, PDF parsing, screenshots, and multi-backend search — all in one local CLI. No cloud, no API keys required for core functionality, no per-request pricing.

[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-597%20passing-brightgreen.svg)](tests/)
[![Version](https://img.shields.io/badge/version-0.3.0-blue.svg)](CONTRIBUTING.md)

[English](README.md) · [中文](README_zh.md) · [CONTRIBUTING](CONTRIBUTING.md)

</div>

---

## What is lightcrawl?

lightcrawl is a local CLI that any AI agent (Claude Code, Codex, Gemini CLI, Copilot CLI) invokes through its shell to fetch and search the web. It is a drop-in replacement for the agent's built-in `WebFetch` / `WebSearch` tools that actually works on the modern web.

Positioned as an **open-source, local, free firecrawl alternative** — matching firecrawl `/scrape`'s core parameter surface while running entirely on your machine.

### When built-in tools fail

| Problem | Built-in WebFetch | lightcrawl |
|---|---|---|
| Cloudflare-protected pages | ❌ TLS fingerprint mismatch, challenge page | ✅ curl_cffi + Playwright stealth, auto-escalation (L1→L2→L3) |
| JavaScript-rendered SPAs | ❌ Empty `<div id="root">` shell | ✅ Headless Chromium executes JS, waits for selectors |
| Login-walled pages (X, LinkedIn) | ❌ Gets sign-in page | ✅ Saved login sessions via `auth login` |
| Full-page dump (nav, sidebar, ads) | ❌ Wastes 60–95% of tokens | ✅ Auto-scopes to `<main>`/`<article>`, strips noise |
| PDF download links | ❌ Silent failure | ✅ pypdf text extraction with per-page output |
| Single backend, no failover | ❌ Rate-limit = dead end | ✅ Brave + Serper + Tavily with automatic failover |

---

## Feature highlights

### Fetch (firecrawl `/scrape` parity)

- **Three-layer escalation** — L1 `curl_cffi` (Chrome 120 impersonate) → L2 Playwright + stealth → L3 saved login sessions. Each request only escalates as far as needed.
- **Content pipeline** — auto-scopes to `<main>`/`<article>`, strips invisible elements, returns structured `headings` with line numbers. Saves 30–90% of tokens.
- **Output formats** — markdown (default), html, text, screenshot (full-page PNG), markdown+screenshot, links (JSON), images (JSON).
- **Declarative browser actions** — `click`, `write`, `press`, `wait`, `scroll`, `screenshot` execute in the Playwright context between page load and content extraction. Reusable intermediate screenshots with sparse indexing.
- **Links & images always-on** — `metadata.links` (`{url, text, rel}`) and `metadata.images` (`{url, alt, width?, height?}`) populated on every response regardless of output format.
- **PDF parsing** — `.pdf` URLs are auto-dispatched to pypdf. Per-page text extraction, magic-byte fallback, `metadata.num_pages` / `metadata.content_length`.
- **Mobile emulation** — iOS Safari impersonate profile (UA + TLS fingerprint + viewport) on both L1 and L2.
- **Custom headers + tag filtering** — `--header KEY=VAL` (repeatable), `--include-tag` / `--exclude-tag` for firecrawl-style DOM tag scoping.

### Search

- **Three backends** — Brave (independent index, free 2k/mo), Serper (Google SERP), Tavily (LLM-optimized snippets). Auto-failover.
- **Search + read** — `search-and-read` finds results and fetches top N pages in one call.
- **Structured results** — rich snippets, domain hints, per-result `fetch_hint`.

### Auth

- **Login profiles** — `auth login` opens a headed Chromium, user logs in manually (password never touches the tool), session saved for reuse.
- **Domain-bound** — profiles are bound to the login URL's eTLD+1.
- **SSRF guard** — loopback, private, link-local IPs blocked by default.

### v0.3 — local firecrawl (`map`, `crawl`, cache)

- **`map`** — in-domain URL discovery, sitemap-first (`robots.txt` → `/sitemap.xml` → `/sitemap_index.xml`) with a homepage-`<a>` fallback. `lightcrawl map https://fastapi.tiangolo.com/`.
- **`crawl`** — BFS multi-page crawl with an append-only on-disk job store, crash-safe resume, and cancellation: `crawl`, `crawl-status`, `crawl-resume`, `crawl-cancel`, `jobs`. Per-host `robots.txt` enforcement, `--include-path`/`--exclude-path` (regex), `--max-pages`/`--max-depth`.
- **`batch-fetch`** — fetch many URLs in parallel through the shared Router/cache; one JSON object aggregating per-URL results, one failure never loses the rest.
- **Local cache** — opt-in via `--max-age <dur>` (serve the stored body if fresher than `<dur>`, otherwise fetch live and store the result). `--no-store` reads without writing, `--cache-only` is an offline read, `--no-cache` bypasses entirely. Keyed by canonical URL **+ profile** so authed and unauthed fetches never collide. The bare `fetch` default is unchanged — no cache read or write unless a flag opts in.
- **Conditional requests** — a stale cache-on fetch carrying `ETag`/`Last-Modified` sends a conditional GET on the L1 (impersonated) path; a `304` reuses the cached body and refreshes its freshness (`revalidated: true`).
- **`cache stats` / `cache clear`** — inspect cache size + host breakdown (incl. legacy dumps), or clear all / by host.

---

## Quick start

```bash
git clone https://github.com/yaoyi1222/lightcrawl.git
cd lightcrawl
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev,bench]"
.venv/bin/playwright install chromium
```

```bash
# Optional: search backends (pick one)
export BRAVE_SEARCH_API_KEY=...
export SERPER_API_KEY=...
export TAVILY_API_KEY=...
```

### Wire it into your agent

```bash
# Claude Code (per-project)
mkdir -p .claude/skills/lightcrawl
cp skills/lightcrawl/SKILL.md .claude/skills/lightcrawl/SKILL.md

# Claude Code (user-wide)
mkdir -p ~/.claude/skills/lightcrawl
cp skills/lightcrawl/SKILL.md ~/.claude/skills/lightcrawl/SKILL.md
```

For other agents, point them at `skills/lightcrawl/SKILL.md`. No daemon, no MCP server, no transport setup.

```bash
.venv/bin/lightcrawl list-backends
.venv/bin/lightcrawl fetch https://example.com/
```

---

## Architecture

```
cli.py ─── Router (router.py) ────────► fetch_http.py     L1: curl_cffi + TLS impersonation
       │                               ► fetch_browser.py  L2: Playwright + stealth
       │                               ► fetch_browser.py  L3: L2 + saved storage_state
       │                               ► fetch_pdf.py      PDF: pypdf extraction
       │                               ► actions.py        Actions: click/write/scroll/...
       │
       └── SearchService ─── owns ───► Router (parallel fetches)
```

Every command prints one JSON object on stdout, exits 0 on `ok: true`, 1 on `ok: false`. The skill (`skills/lightcrawl/SKILL.md`) is the canonical reference your agent reads.

### Token efficiency

| Site | Built-in WebFetch | lightcrawl default | With `--selector` | Best saving |
|---|---|---|---|---|
| **Wikipedia** Python | 58,000 chars | 40,000 | 40,000 | **31%** |
| **GitHub** psf/requests | 17,500 chars | 8,040 | 2,069 | **90%** |
| **Django docs** overview | 22,742 chars | 14,695 | 12,867 | **52%** |
| **Python docs** tutorial | 15,224 chars | 22,160* | — | — |

\*More content via lightcrawl — Playwright renders JS that loads sidebar navigation.

---

## Commands

| Command | What it does |
|---|---|
| `lightcrawl fetch <url>` | Fetch with auto strategy escalation. Supports `--output-format`, `--selector`, `--actions`, `--mobile`, `--header`, `--include-tag`/`--exclude-tag`, `--remove-base64-images`, the cache flags below, screenshot / links / images output. |
| `lightcrawl map <url>` | Discover in-domain URLs (sitemap-first, homepage fallback). `--search`, `--limit`. |
| `lightcrawl crawl <url>` | BFS multi-page crawl. `--max-pages`, `--max-depth`, `--include-path`/`--exclude-path` (regex), `--no-cache`. |
| `lightcrawl crawl-status` / `crawl-resume` / `crawl-cancel` / `jobs` | Inspect, resume (crash-safe), cancel a crawl, or list all jobs. |
| `lightcrawl batch-fetch <url...>` | Fetch many URLs in parallel; aggregated per-URL JSON. |
| `lightcrawl cache stats` / `cache clear` | Inspect or clear the local fetch cache. |
| `lightcrawl search <query>` | Web search with structured results and per-result `fetch_hint`. |
| `lightcrawl search-and-read <query>` | Search then parallel-fetch top N results. |
| `lightcrawl list-backends` | Report configured search backends. |
| `lightcrawl auth login <profile> <url>` | Open headed browser for manual login, save profile. |
| `lightcrawl auth list` / `show` / `revoke` | Manage saved login profiles. |

Cache flags (on `fetch` / `batch-fetch`): `--max-age <dur>` serve-if-fresher-and-store (`30m`, `1h`, `24h`), `--no-store` read but don't write, `--cache-only` offline hit-or-`CACHE_MISS`, `--no-cache` bypass.

Full flags: `lightcrawl <subcmd> --help`, or `lightcrawl --version`.

---

## Search backends

Three pluggable backends ship in-tree. Defaults to **Brave → Serper → Tavily** (first configured).

| Backend | Strength | When to pick |
|---|---|---|
| **Brave** | Independent index, free 2k/mo | Default. Most queries. |
| **Serper** | Google SERP ranking | Brave quota exhausted or Google miss. |
| **Tavily** | LLM-optimized snippets (200–500 chars) | Long snippets answer ~70% of queries without a fetch. |

Adding a new backend is ~120 lines — see `src/lightcrawl/search/backends/brave.py`.

---

## vs firecrawl

lightcrawl targets parity with firecrawl's `/scrape` endpoint, and as of v0.3 also covers `/map` and `/crawl`. LLM-based extraction stays deferred.

| firecrawl endpoint / param | lightcrawl status |
|---|---|
| `/map` (URL discovery) | ✅ `lightcrawl map` (v0.3) |
| `/crawl` (multi-page BFS) | ✅ `lightcrawl crawl` + lifecycle subcommands (v0.3) |
| `url` | ✅ |
| `formats: [markdown, html, rawHtml, screenshot, links, ..., images]` | ✅ markdown, html, text, screenshot, markdown+screenshot, links, images |
| `headers` | ✅ `--header KEY=VAL` (repeatable) |
| `includeTags` / `excludeTags` | ✅ `--include-tag` / `--exclude-tag` |
| `waitFor` (ms) | ✅ `--wait-for-network-idle` |
| `actions` (click, write, screenshot, scroll, wait, press) | ✅ `--actions '[...]'` |
| `mobile` | ✅ `--mobile` (iOS Safari impersonate) |
| `onlyMainContent` | ✅ default behavior (auto-scopes to `<main>`/`<article>`) |
| `removeBase64Images` | ✅ **default `True` since v0.3** (strips `data:` URI images only; external `<img>` flow through). Restore v0.2 behavior with `--no-remove-base64-images`. |
| `location` (country) | deferred to v0.4+ |
| `extract` (LLM-structured) | deferred to v0.5 |
| `blockAds` | deferred to v0.4+ |
| Cloud-hosted | ❌ — runs locally (your IP, your cookies, no third-party cloud) |
| Free | ✅ — MIT license, no API keys needed for core fetch |

<div align="center">

**lightcrawl = free, local firecrawl `/scrape` with anti-bot bypass and login sessions.**

</div>

---

## Configuration

`~/.lightcrawl/config.toml` (optional):

```toml
[ssrf]
extra_allowlist = ["internal.example.com"]

[search]
default_backend = "brave"
```

| Environment variable | Purpose |
|---|---|
| `BRAVE_SEARCH_API_KEY` | Brave search API (free 2k/mo) |
| `SERPER_API_KEY` | Serper Google SERP proxy |
| `TAVILY_API_KEY` | Tavily LLM-optimized search |

---

## Security

- Profiles stored as 0600-permission `storage_state` JSON (Playwright convention).
- `auth show` returns metadata only — never cookie contents.
- Profiles bound to login URL's eTLD+1 (a `twitter` profile for `x.com` cannot be used on `attacker.com`).
- SSRF guard blocks loopback, private nets, cloud metadata IPs.
- Fetched content treated as data; the skill instructs agents to ignore in-page directives.

---

## License

MIT — see [`LICENSE`](LICENSE).
