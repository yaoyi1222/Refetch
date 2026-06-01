# v0.3 PR 3 — ETag / Last-Modified conditional requests (L1 only)

Design doc for the conditional-request slice. Gated by the R1 probe
(`bench/probe_conditional.py`, PR #65 — **PASS**, 304 hit-rate 0.67 on the
`chrome120` impersonate path). See `docs/v0.3/design.md` §5.2, §10(2b/3), §11 R1.

## Goal

When a cached entry has gone stale (its age exceeds `max_age_ms`, or no
`max_age` was set at all in a cache-on context) but carries a validator
(`ETag` / `Last-Modified`), the next fetch should send a conditional GET on
the **L1 (curl_cffi) path**. On a `304 Not Modified` we reuse the cached body
and refresh the entry's freshness instead of re-downloading. This delivers
acceptance §10(2b): ">24h interval, no `--max-age` → 304 path, hit-rate ≥ 0.5".

## Hard constraints (from §11 R1)

- **L1 only.** Conditional requests ride the curl_cffi impersonate path. We do
  NOT add a "downgrade to a non-impersonate path" to force 304s — that throws
  away the TLS fingerprint that is curl_cffi's entire value (the probe proved
  the impersonate path already surfaces real 304s).
- **No regression for the v0.2 default.** A bare `fetch` (no cache flags:
  `max_age_ms=None`, `store_in_cache=False`, `no_cache=False`) must stay
  byte-identical to v0.2 — it sends no conditional headers and never reads the
  cache. Revalidation only fires when cache is opted in.
- **Errors-as-values / one-JSON contract** unchanged: a 304 returns the normal
  success envelope with `cache_hit=True`.

## What's already in place (PR 2 pre-wiring — do not rebuild)

- `Cache.lookup_for_revalidation(url, *, profile)` — age-agnostic read; returns
  a `CacheHit` whose `.headers` dict already carries `etag` / `last-modified`
  (populated by `_read_entry` from the payload JSON).
- `Cache.store(...)` already persists `response["headers"]` (falls back to
  `metadata["headers"]`) into the `etag` / `last_modified` index columns. The
  router simply never produced a `headers` key — that's the missing half.

## Changes

### 1. `fetch_http.py` — expose response validators

`HttpResult` gains two fields:

```python
etag: str | None = None
last_modified: str | None = None
```

populated from `r.headers.get("etag")` / `r.headers.get("last-modified")`.

A `304` response is **not** an error: curl_cffi returns it with status 304 and
an empty body, and `fetch()` already returns any status without raising (it
only raises on transport `RequestsError`). No change needed there — but a
docstring note records that 304 is an expected, returnable status.

### 2. `cache.py` — `mark_revalidated`

```python
def mark_revalidated(self, url, *, profile) -> None:
    """304 path: the cached body is confirmed current as of now. Refresh
    BOTH accessed_at (LRU) and fetched_at (age) so the entry counts as
    fresh for subsequent max_age lookups."""
```

`touch` is left unchanged (accessed_at only) so existing PR 2 semantics and
tests hold; the 304 path needs the stronger freshness reset, hence a distinct
method.

### 3. `router.py` — conditional GET on the L1 auto path

A small helper decides whether cache is "in play" for reads/revalidation:

```python
def _cache_in_play(req) -> bool:
    # opted in via an explicit cache flag; the bare-fetch default is excluded
    return not req.no_cache and (req.store_in_cache or req.max_age_ms is not None
                                 or req.cache_only)
```

`_revalidation(req) -> tuple[CacheHit | None, dict[str, str]]`: when
`_cache_in_play(req)` and not `cache_only`, read `lookup_for_revalidation`; if
the hit exposes `etag` / `last-modified`, build `{If-None-Match, If-Modified-Since}`.
Returns `(None, {})` otherwise. `cache_only` is excluded because it never
reaches the network (the entry hook already returned a hit or `CACHE_MISS`).

Wiring (only the **auto L1** branch and the forced `--strategy http` branch —
both are curl_cffi; the browser branches are untouched):

1. Build `cond_headers` via `_revalidation(req)`. Merge them under the
   caller's `req.headers` (caller-supplied wins on collision, matching
   `fetch_http.fetch`'s merge order).
2. Run L1 with the merged headers.
3. If `r.status_code == 304` **and** we had a revalidation hit:
   `cache.mark_revalidated(...)` then return `_success_from_cache(req, hit, ...)`
   with `revalidated=True` added to the envelope and an `Attempt("http", "304")`
   recorded. The cached body is served; no re-extraction, no L2 escalation.
4. Otherwise proceed exactly as today (extract, escalate, `_cache_store_if_requested`).

Edge: a 304 with **no** revalidation hit in hand (shouldn't happen, but be
defensive) falls through to normal handling rather than serving a phantom body.

### 4. `router._success_from_http` — emit `headers`

Add a top-level `"headers"` dict containing only the present validators:

```python
headers = {}
if r.etag: headers["etag"] = r.etag
if r.last_modified: headers["last-modified"] = r.last_modified
```

so `Cache.store` persists them and the *next* fetch can revalidate. Keys are
lower-cased to match what `store` reads (`headers.get("etag")`,
`headers.get("last-modified")`).

### 5. `_success_from_cache` — `revalidated` flag

Accept `revalidated: bool = False`; when True, set `result["revalidated"] = True`
and `cache_age_ms = 0` (just refreshed). This lets crawl progress / agents tell
a 304-revalidated hit from a plain age-based hit.

## Out of scope

- Browser-path (L2/L3) conditional requests — Playwright doesn't expose a clean
  conditional-GET + 304-body-reuse path; L1 is where validators live.
- `min_age_ms`, `If-Range`/range requests — deferred (design §1 非目标).

## Test plan (offline, `tests/test_router_conditional.py`)

Patch `socket.gethostbyname` + `fetch_http.fetch` per the house pattern. A
canned `fetch_http.fetch` stub inspects incoming `headers` to simulate an
origin: returns 200 + `etag`/`last_modified` on an unconditional GET; returns
304 (empty body) when the request carries a matching `If-None-Match` /
`If-Modified-Since`.

1. **store captures validators** — a cache-on fetch persists `etag`/`last-modified`
   (assert via `Cache.lookup_for_revalidation().headers`).
2. **stale entry → conditional GET → 304 → reuse** — second fetch past `max_age`
   sends `If-None-Match`, origin 304s, response is `cache_hit=True`,
   `revalidated=True`, body equals the cached body, and the stub saw the
   conditional header.
3. **304 refreshes freshness** — after the 304, a subsequent `max_age`-bounded
   lookup hits (fetched_at was bumped).
4. **200 on revalidation → overwrite** — origin returns a new body + new etag;
   response is a live fetch (not cache_hit), and the new etag is stored.
5. **no validator stored → no conditional header** — entry without etag/lastmod
   sends a plain GET.
6. **bare fetch sends no conditional header** — default flags: no revalidation
   read, no `If-None-Match`, even when an entry exists (regression guard for the
   v0.2 default).
7. **`no_cache` skips revalidation** — explicit bypass sends a plain GET.
8. **Last-Modified-only entry** — uses `If-Modified-Since`, 304 reuse works.
9. **`fetch_http.HttpResult` carries etag/last_modified** — unit-level on the
   stub-free path is covered indirectly; a direct field-presence test guards the
   dataclass shape.

## Verification

- `.venv/bin/pytest tests/test_router_conditional.py -q` — new suite green.
- `.venv/bin/pytest -q` — full suite, zero regressions.
- `.venv/bin/ruff check src/lightcrawl tests` — clean.
- The R1 probe (already PASS) remains the empirical gate; no new network test.
