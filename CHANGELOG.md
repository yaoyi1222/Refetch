# Changelog

All notable changes to lightcrawl are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); dates are
ISO 8601.

## [0.3.0] — 2026-06-03

v0.3 upgrades lightcrawl from "enhanced WebFetch" to "local firecrawl"
with map / crawl / cache as the headline features. See
`docs/v0.3/design.md` for the full plan.

### Breaking changes

- **`remove_base64_images` default flipped from `False` to `True`.**
  Affects `FetchRequest.remove_base64_images`, the
  `html_to_markdown(remove_base64_images=...)` function-level default,
  and the `lightcrawl fetch` CLI subcommand (which now honors the new
  dataclass default when the flag is absent — see Fixed below).
  v0.2 stripped every `<img>` by default for byte-identical v0.1 output;
  v0.3 strips only `data:` URI images, letting external `<img>` tags
  flow into markdown. To restore v0.2 behavior, pass
  `remove_base64_images=False` programmatically or use the new
  `--no-remove-base64-images` CLI flag.

### Fixed

- **Declared `cssselect` as a runtime dependency.** `content.py`'s selector
  path (`--selector`, `_suggested_selectors`) calls lxml's `.cssselect()`,
  which requires the `cssselect` package. It was never in `dependencies` and
  only worked because the `[bench]` extra pulls it in transitively
  (`readability-lxml` → `cssselect`). A plain `pip install lightcrawl` (or
  `.[dev]`) therefore got a silently-degraded selector feature — `.cssselect()`
  raised, was swallowed by a defensive `try/except`, and extraction fell back
  to `<body>`. Surfaced by the new `[dev]`-only CI matrix.
- CLI now honors the new `remove_base64_images=True` default. The v0.3
  PR 1 initial commit (`bcf0ec2`) flipped the `FetchRequest` dataclass
  default but `cli.py` was still passing `args.remove_base64_images`
  (an argparse `store_true` False on absence) straight through,
  silently overriding the new default. The `--remove-base64-images`
  flag now uses `argparse.BooleanOptionalAction`; absence means
  "fall through to dataclass default", explicit `--remove-base64-images`
  forces True, and the auto-generated `--no-remove-base64-images`
  forces False.

### Added

- **URL canonicalization** (`canonical.py`) — pure-function canonicalization
  and `url_hash(canonical_url, profile=...)`, the single source of truth for
  cache keys and crawl dedup. The `profile` dimension is a security boundary:
  an authed fetch with `profile=twitter` hashes differently than an unauthed
  fetch of the same URL, preventing cross-profile cache replay.
- **Local fetch cache** (`cache.py` + Router cache aspect) — SQLite-WAL index
  + atomic body store under `~/.lightcrawl/cache/`. New `FetchRequest` fields
  and CLI flags: `--max-age <dur>` (serve the stored body if fresher, else
  fetch live and store), `--no-store` (read but don't write), `--cache-only`
  (offline, hit-or-`CACHE_MISS`), `--no-cache` (bypass). Cache key includes the
  `profile` dimension; the bare `fetch` default is unchanged (byte-identical to
  v0.2 — no cache read or write unless a flag opts in).
- **Conditional requests** (PR 3) — on a stale cache-on fetch carrying an
  `ETag` / `Last-Modified`, the L1 (curl_cffi, impersonated) path sends a
  conditional GET; a `304 Not Modified` reuses the cached body and refreshes
  its freshness (`revalidated: true` in the envelope). L1 only; gated by the
  R1 probe (304 hit-rate 0.67 on `chrome120`).
- **`lightcrawl map <url>`** (`sitemap.py`) — in-domain URL discovery,
  sitemap-first (robots.txt `Sitemap:` → `/sitemap.xml` → `/sitemap_index.xml`),
  falling back to homepage `<a>` links. Emits `{source, count, urls, notes?}`.
- **Crawl** (`jobs.py` + `crawl.py` + `robots.py`) — BFS multi-page crawl with
  an append-only on-disk job store, crash-safe resume, and cancellation:
  `crawl`, `crawl-status`, `crawl-resume`, `crawl-cancel`, `jobs`. Per-host
  robots.txt allow/disallow enforcement; `--include-path`/`--exclude-path`
  (regex, matched on path+query);
  `--no-cache` override. Liveness via psutil PID + create_time double-check;
  atomic writes via `os.replace` (Windows-safe).
- **`lightcrawl batch-fetch`** (`batch.py`) — fetch many URLs in parallel
  through the shared Router/cache, one JSON object aggregating per-URL results;
  one failure never loses the others.
- **`lightcrawl cache stats` / `cache clear`** — report cache size / host
  breakdown + legacy dumps usage; clear all or by host.
- **`lightcrawl --version`** — prints `lightcrawl 0.3.0`.

### CI / packaging

- `.github/workflows/ci.yml` — ruff + the full offline suite on
  `ubuntu-latest`, plus the cross-platform-sensitive modules
  (`canonical`/`cache`/`jobs`/`sitemap`/`batch_fetch`) on `windows-latest`.
- Version bumped to `0.3.0` (`pyproject.toml` + `lightcrawl.__version__`),
  with `tests/test_version.py` pinning the two together.

## [0.2.0] — 2026-05-18

See `git log v0.2.0` and PR #16–#21 for the full v0.2 changeset.

## [0.1.0]

Initial public CLI + skill release.
