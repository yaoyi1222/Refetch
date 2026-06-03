"""Cache hit-rate benchmark — v0.3 PR 8 / acceptance §10.2.

NETWORK benchmark. Not collected by pytest (network code lives in ``bench/``).
It measures the two acceptance tiers entirely through the **public CLI**
(``lightcrawl batch-fetch``), reading only the response envelope
(``cache_hit`` / ``revalidated`` / ``headers``) — no internals, no fabricated
numbers. Run it and it prints exactly what it measured.

Tiers (design ``docs/v0.3/design.md`` §10.2):
  (a) warm re-fetch within ``--max-age 1h`` → ``cache_hit`` ratio ≥ 0.95.
  (b) stale re-fetch (``--max-age 1ms`` forces staleness while keeping the
      cache in play) → conditional GET → ``revalidated`` (304) ratio ≥ 0.5
      over the URLs that carry an ``ETag`` / ``Last-Modified``. A tiny
      ``--max-age`` stands in for the ">24h, no --max-age" crawl scenario so
      the bench needn't wait a day.

Usage::

    python -m bench.bench_cache --seed https://fastapi.tiangolo.com/ --limit 25
    python -m bench.bench_cache --url https://a/ --url https://b/
    python -m bench.bench_cache --seed https://fastapi.tiangolo.com/ \
        --out bench/results/v0.3_cache.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Run the CLI exactly as an agent would, in this interpreter's environment.
_CLI = [sys.executable, "-m", "lightcrawl.cli"]

_DEFAULT_SEED = "https://fastapi.tiangolo.com/"


def _run_cli(args: list[str]) -> dict:
    """Invoke ``lightcrawl <args>`` and parse its single JSON object."""
    proc = subprocess.run(  # noqa: S603 - args are bench-controlled
        [*_CLI, *args],
        capture_output=True,
        text=True,
    )
    if not proc.stdout.strip():
        raise RuntimeError(f"no JSON from CLI: {args!r}\nstderr: {proc.stderr[:500]}")
    return json.loads(proc.stdout)


def discover_urls(seed: str, limit: int) -> list[str]:
    out = _run_cli(["map", seed, "--limit", str(limit)])
    if not out.get("ok"):
        raise RuntimeError(f"map failed: {out.get('error_code')} {out.get('error_detail')}")
    return [u["url"] for u in out.get("urls", [])][:limit]


def _batch(urls: list[str], max_age: str) -> list[dict]:
    out = _run_cli(["batch-fetch", *urls, "--max-age", max_age, "--output-format", "text"])
    return out.get("results", [])


def _ratio(num: int, den: int) -> float:
    return round(num / den, 3) if den else 0.0


def run(urls: list[str]) -> dict:
    total = len(urls)

    # Pass 1 — warm the cache (entries are written under --max-age).
    warm = _batch(urls, "1h")
    warm_hits = sum(1 for r in warm if r.get("cache_hit"))
    with_validator = sum(1 for r in warm if r.get("headers"))

    # Tier (a) — immediate re-fetch, entries are fresh for a 1h window.
    tier_a = _batch(urls, "1h")
    a_hits = sum(1 for r in tier_a if r.get("cache_hit"))

    # Tier (b) — re-fetch with a 1ms window: entries are "stale" so the router
    # issues a conditional GET; a 304 shows up as revalidated=True.
    tier_b = _batch(urls, "1ms")
    b_reval = sum(1 for r in tier_b if r.get("revalidated"))

    return {
        "total_urls": total,
        "warm_pass": {"cache_hit": warm_hits, "with_validator": with_validator},
        "tier_a_max_age_1h": {
            "cache_hit": a_hits,
            "ratio": _ratio(a_hits, total),
            "target": 0.95,
            "pass": _ratio(a_hits, total) >= 0.95,
        },
        "tier_b_conditional_304": {
            "revalidated": b_reval,
            "ratio_over_all": _ratio(b_reval, total),
            "ratio_over_validatored": _ratio(b_reval, with_validator),
            "target": 0.5,
            "pass": _ratio(b_reval, with_validator) >= 0.5,
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="lightcrawl cache hit-rate benchmark")
    ap.add_argument("--seed", help="seed URL; its sitemap/links supply the URL set")
    ap.add_argument("--url", action="append", dest="urls", help="explicit URL (repeatable)")
    ap.add_argument("--limit", type=int, default=25, help="max URLs from --seed (default 25)")
    ap.add_argument("--out", type=Path, help="also write the JSON summary here")
    args = ap.parse_args(argv)

    if args.urls:
        urls = list(args.urls)
    else:
        urls = discover_urls(args.seed or _DEFAULT_SEED, args.limit)
    if not urls:
        print(json.dumps({"ok": False, "error": "no URLs to benchmark"}))
        return 1

    summary = {"ok": True, **run(urls)}
    blob = json.dumps(summary, indent=2)
    print(blob)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(blob + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
