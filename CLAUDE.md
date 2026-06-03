# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`lightcrawl` is a local CLI plus an agent-facing skill (`skills/lightcrawl/SKILL.md`). As of v0.3 the CLI exposes: `fetch`, `search`, `search-and-read`, `map`, `crawl` (+ `crawl-status`/`crawl-resume`/`crawl-cancel`/`jobs`), `batch-fetch`, `cache stats`/`cache clear`, `list-backends`, `auth login`/`auth list`/`auth show`/`auth revoke`, and a top-level `--version`. Every subcommand prints a single JSON object on stdout and exits 0 on `ok: true` / 1 on `ok: false`. It is a drop-in replacement for the built-in `WebFetch` / `WebSearch` that survives Cloudflare/TLS-fingerprint blocks, JS-rendered SPAs, and login-walled pages, and adds firecrawl-style `/map` + `/crawl` plus an opt-in local cache.

Source layout: package lives at `src/lightcrawl/`; tests at `tests/`; benchmark + diagnostic harness at `bench/`; the agent-facing skill at `skills/lightcrawl/SKILL.md`. There is no MCP server in this repo — agents invoke `lightcrawl` through their normal shell tool. Version lives in two places kept in lockstep by `tests/test_version.py`: `pyproject.toml` and `src/lightcrawl/__init__.py:__version__`.

## Common commands

All commands assume the repo's `.venv` is set up via `pip install -e ".[dev,bench]"` and `playwright install chromium`.

```bash
.venv/bin/pytest -q                              # full suite (fully offline, 597 tests)
.venv/bin/pytest tests/test_router.py -q         # one file
.venv/bin/pytest tests/test_router.py::test_blocks_private_url   # one test
.venv/bin/ruff check src tests bench             # lint

.venv/bin/lightcrawl auth list                # list saved profiles
.venv/bin/lightcrawl fetch https://example.com/   # smoke-test the fetch pipeline
.venv/bin/lightcrawl list-backends            # which search backends have API keys

# Benchmark / diagnostic (hits the network)
.venv/bin/python -m bench.runner --urls bench/urls_smoke.toml --out bench/results/smoke.json
.venv/bin/python -m bench.report bench/results/smoke.json > bench/results/smoke.md
.venv/bin/python -m bench.diagnostic --urls bench/urls_extended.toml --out bench/results/diag.json
```

`pytest-asyncio` runs in `asyncio_mode = "auto"` (set in `pyproject.toml`), so `async def test_*` does not need a decorator.

## Architecture

The codebase has two cooperating subsystems sharing one process:

```
cli.py (argparse entry, one async subcommand per public op)
  ├── Router (router.py) ────────► fetch_http.py / fetch_browser.py / fetch_pdf.py
  └── SearchService (search/service.py) ─── owns ───► Router (router.py)
                                                       │
                                                       ├─ L1: fetch_http.py  (curl_cffi, sync, run via asyncio.to_thread)
                                                       ├─ L2: fetch_browser.py (Playwright + stealth, single Chromium / multi-context)
                                                       ├─ L3: same as L2 but loads storage_state from auth.py profile
                                                       ├─ PDF: fetch_pdf.py (pypdf extraction, L1-only)
                                                       └─ Actions: actions.py (declarative click/write/press/wait/scroll/screenshot)
```

Every async subcommand routes through `cli._safe_run()` which converts a `FetchError` or any uncaught exception into the same `{"ok": false, "error_code": "...", "error_detail": "..."}` envelope, so skills can parse every invocation uniformly.

**Fetch escalation policy** (`router.py`): every request goes L1 → L2 → L3 only as far as needed. The router decides escalation via two signals:

1. `_should_escalate_to_browser(status, html)` — escalates on 403/429/503, on `_looks_like_challenge(html)` (CF detection: a 2-level keyword system, see `_CF_CHALLENGE_STRONG` and `_CF_CHALLENGE_WEAK` in `router.py`), and on `visible_text_ratio(html) < 0.01` for HTML > 2000 bytes (catches SPA shells).
2. Login-wall detection in `content.py` — escalates to authed strategy if the page text matches "sign in to continue" / etc.

**Single Browser, multi-context** (`fetch_browser.py`): one Chromium instance is reused for the whole process; each fetch gets a fresh `BrowserContext`. Authed fetches load a `storage_state` JSON from `~/.lightcrawl/profiles/<name>.json`.

**Profile binding** (`auth.py` + `url_safety.py`): each profile is bound to the eTLD+1 of the login URL (computed via `tldextract`). A `twitter` profile bound to `x.com` cannot fetch `attacker.com/x.com/...` — `domain_matches()` enforces this in the router before any fetch.

**SSRF guard** (`url_safety.py`): every `validate_url()` call resolves the host (handling IPv4/IPv6 literals via `ipaddress.ip_address()` *before* falling back to `socket.gethostbyname()` — IPv6 literals must NOT be sent to `gethostbyname` as it returns a misleading `DNS_FAILED`). Loopback / private / link-local IPs are blocked unless `allow_private=True` or in `extra_allowlist`.

**Content pipeline** (`content.py` — the most-touched module):
- `_clean_dom(doc)` strips `<script>/<style>/<iframe>/<svg>/<form>/<img>/<noscript>/...` plus elements with `aria-hidden=true` (case-insensitive) and `style="display:none"` (case-insensitive, whitespace-tolerant). Mutating during iteration is forbidden; collect into a `to_remove` list and remove in a second pass.
- `_select_target(doc, selector)` auto-scopes to a single `<main>` or `<article>` when present (this is what makes Wikipedia's H1 land on line 1 instead of line 95). Falls back to `<body>`.
- `_dom_headings(target)` extracts `<h1>`–`<h6>` directly from DOM (not from rendered markdown — so inline `<code>`/`<strong>`/`<em>` formatting doesn't break extraction).
- `_locate_headings_in_markdown` matches DOM heading text to ATX heading lines using `_strip_md_formatting()` to remove backticks/asterisks/links from the markdown side before comparison. Without this, headings with inline formatting return `line=None`.
- `_extract_links(doc, base_url)` and `_extract_images(doc, base_url)` (PR 3) scan the raw DOM before `_clean_dom`. Links get `{url, text, rel}` with internal/external classification; images get `{url, alt, width?, height?}`. Both appear in `metadata` on every response and as dedicated `output_format`s.
- `detect_spa_shell(html)` returns True if `_SPA_SHELL_PATTERNS` matches an empty `<div id="root">` / `<div id="__next">` regardless of page length, OR if there's a `<noscript>` JS warning on a small page.
- Overflow handling: anything over `max_inline_tokens` is dumped to `~/.lightcrawl/dumps/<sha1>.md`; the response carries `dump_path` plus the heading list with line numbers so the agent can grep the dump.

**Search service** (`search/service.py`): `search_and_read` runs `search` then fans out `Router.fetch` calls via `asyncio.gather(..., return_exceptions=True)` with each fetch wrapped in a `try/except` that returns a failure dict — one crash cannot lose the other in-flight results.

**v0.3 subsystems** (all share the same one-JSON / errors-as-values contract):
- **Canonicalization** (`canonical.py`): pure-function URL canonicalization + `url_hash(canonical_url, profile=...)`, the single source of truth for cache keys and crawl dedup. The `profile` dimension is a security boundary — never bypass it when adding a URL-keyed code path.
- **Cache** (`cache.py` + Router cache aspect): SQLite-WAL index + atomic body store under `~/.lightcrawl/cache/`, opt-in per-request. The bare `fetch` default stays byte-identical to v0.2 (no read/write unless a cache flag opts in). Conditional requests (ETag/Last-Modified → `304`) ride the L1 (curl_cffi impersonate) path only; `_revalidation` in `router.py` builds the conditional headers, `mark_revalidated` refreshes freshness on a 304.
- **Map** (`sitemap.py` + `lightcrawl map`): sitemap-first URL discovery, homepage `<a>` fallback. Fetches go through `Router.fetch` (inherits SSRF guard + escalation); sitemap reads use a very high `max_inline_tokens` so the raw XML isn't dumped/truncated.
- **Crawl** (`jobs.py` + `crawl.py` + `robots.py`): BFS engine + append-only on-disk job store with crash-safe resume and cancellation. Liveness uses psutil PID + create_time double-check; all writes are atomic via `os.replace` (Windows-safe); remote signals via a `.cancel` file (no `loop.add_signal_handler`).
- **Batch** (`batch.py` + `lightcrawl batch-fetch`): parallel multi-URL fetch through the shared Router/cache; one URL's failure never drops the others.

## Conventions and gotchas

- **Errors are values, not exceptions, at the public boundary.** `Router.fetch()` and `SearchService.*` always return a dict with `ok: bool` + `error_code` (a value from `errors.py::ErrorCode`). Internal code raises `FetchError(code, detail)`; the router/service/`cli._safe_run` catches and converts. Don't print raw tracebacks to stdout — it breaks the one-JSON-per-invocation contract skills rely on.
- **L1 timeout has a known thread leak** (Bug 6 in `bug.md`, marked wontfix): `asyncio.wait_for(asyncio.to_thread(curl_cffi.fetch, ...))` cannot cancel curl_cffi's blocking I/O. The asyncio side gives up at `l1_timeout + 1.0s`; the underlying thread continues until curl's own timeout. The auto-escalation to L2 prevents repeated leaks on the same URL. Don't try to "fix" this without migrating L1 to an async HTTP client — see the wontfix section in `bug.md` for context.
- **DOM mutation requires a two-pass collect-then-remove.** `lxml`'s `doc.iter()` invalidates if you remove elements mid-iteration. See `_clean_dom` for the canonical pattern.
- **No fabricated content; surface real failures.** The skill (`skills/lightcrawl/SKILL.md`) enforces an honesty contract: when a fetch fails, return the structured `error_code` rather than papering over with training data.
- **Tests must stay offline.** The pytest fixture in `tests/test_router.py` patches `socket.gethostbyname` and `fetch_http.fetch`; new tests should follow the same monkeypatch pattern. Network-touching code lives in `bench/` (which is opt-in and not run by `pytest`).
- **`readability-lxml` and `trafilatura` are bench-only.** They are listed under `[bench]` extras for the diagnostic comparison harness; the production pipeline does NOT depend on them. If you find yourself importing either from `src/`, that's a bug.
- **The benchmark + design history is in-tree.** `plan.md`, `bug.md`, `lightcrawl.md`, `websearch.md`, `websearch_plus.md` capture diagnostic data and prior design decisions — read them before second-guessing why a thing is the way it is.
