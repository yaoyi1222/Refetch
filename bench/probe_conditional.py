"""R1 probe — does curl_cffi's impersonate path correctly do conditional GETs?

Gate for v0.3 PR 3 (ETag / Last-Modified conditional requests, L1 only). See
docs/v0.3/design.md §11 R1. Network-touching and opt-in — NOT run by pytest.

The whole risk PR 3 hinges on: when curl_cffi sends an `If-None-Match` /
`If-Modified-Since` header *on its browser-impersonate path*, does the origin
return a real 304 (and does curl_cffi surface it), or does the impersonate path
strip / mangle the conditional so 304 never hits? This probe answers it
empirically before any PR 3 code is written.

For each target, using the SAME impersonate profile production L1 uses
(`fetch_http.DEFAULT_IMPERSONATE`):

  1. GET → capture status, ETag, Last-Modified, body length, final URL.
  2. If ETag present:        re-GET the final URL with `If-None-Match`  → expect 304.
  3. If Last-Modified present: re-GET the final URL with `If-Modified-Since` → expect 304.

A 304 is verified to also carry an empty body. The verdict is the 304 hit-rate
over targets that *expose* a validator (origins that send none are excluded from
the denominator — they can't be conditionally requested by anyone).

  PASS  → curl_cffi's impersonate path supports conditional requests; PR 3 viable.
  FAIL  → defer ETag/Last-Modified to v0.4. Do NOT introduce a "downgrade to a
          non-impersonate path" to force 304s — that throws away curl_cffi's core
          value (TLS fingerprint), so 304 would never hit in production anyway
          (design §11 R1, review B2).

Usage:
    .venv/bin/python -m bench.probe_conditional \
        --urls bench/urls_conditional.toml \
        --out bench/results/conditional.json
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from curl_cffi import requests as ccr  # noqa: E402

from lightcrawl.fetch_http import DEFAULT_IMPERSONATE  # noqa: E402

# Built-in fallback if --urls is omitted and the default toml is absent.
DEFAULT_TARGETS: list[dict] = [
    {"url": "https://www.cloudflare.com/robots.txt", "category": "cloudflare"},
    {"url": "https://pages.github.com/", "category": "github_pages"},
    {"url": "https://nextjs.org/", "category": "vercel"},
    {"url": "https://cdnjs.cloudflare.com/ajax/libs/jquery/3.7.1/jquery.min.js",
     "category": "static_cdn"},
]


@dataclass
class ValidatorProbe:
    kind: str                       # "etag" | "last_modified"
    sent: str                       # the validator value echoed back
    status: int | None = None
    body_empty: bool | None = None
    is_304: bool = False
    error: str | None = None


@dataclass
class TargetResult:
    url: str
    category: str
    base_status: int | None = None
    final_url: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    has_validator: bool = False
    any_304: bool = False
    probes: list = field(default_factory=list)
    error: str | None = None


def _get(url: str, *, timeout: float, headers: dict | None = None):
    """Plain impersonate GET. Returns the curl_cffi Response (caller reads
    status/headers/content); raises on transport error."""
    return ccr.get(
        url,
        timeout=timeout,
        impersonate=DEFAULT_IMPERSONATE,
        allow_redirects=True,
        headers=headers or None,
    )


def _probe_validator(final_url: str, kind: str, header: str, value: str,
                     *, timeout: float) -> ValidatorProbe:
    p = ValidatorProbe(kind=kind, sent=value)
    try:
        r = _get(final_url, timeout=timeout, headers={header: value})
    except Exception as e:  # noqa: BLE001 — probe records, never crashes the run
        p.error = f"{type(e).__name__}: {e}"
        return p
    p.status = r.status_code
    p.body_empty = len(r.content or b"") == 0
    p.is_304 = r.status_code == 304
    return p


def probe_target(target: dict, *, timeout: float) -> TargetResult:
    res = TargetResult(url=target["url"], category=target.get("category", "?"))
    try:
        r = _get(res.url, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        res.error = f"{type(e).__name__}: {e}"
        return res
    res.base_status = r.status_code
    res.final_url = str(r.url)
    res.etag = r.headers.get("etag")
    res.last_modified = r.headers.get("last-modified")
    res.has_validator = bool(res.etag or res.last_modified)

    if res.etag:
        res.probes.append(
            _probe_validator(res.final_url, "etag", "If-None-Match", res.etag, timeout=timeout)
        )
    if res.last_modified:
        res.probes.append(
            _probe_validator(res.final_url, "last_modified", "If-Modified-Since",
                             res.last_modified, timeout=timeout)
        )
    res.any_304 = any(p.is_304 for p in res.probes)
    return res


def _load_targets(path: str | None) -> list[dict]:
    if path:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        return data.get("urls", [])
    default = Path(__file__).resolve().parent / "urls_conditional.toml"
    if default.exists():
        return tomllib.loads(default.read_text(encoding="utf-8")).get("urls", [])
    return DEFAULT_TARGETS


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="R1 conditional-request (304) probe — PR 3 gate")
    ap.add_argument("--urls", default=None, help="TOML target file (default: bench/urls_conditional.toml)")
    ap.add_argument("--out", default=None, help="write the JSON report to this path")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--min-hit-rate", type=float, default=0.5,
                    help="PASS threshold: fraction of validator-exposing targets that 304 (default 0.5)")
    args = ap.parse_args(argv)

    targets = _load_targets(args.urls)
    results = [probe_target(t, timeout=args.timeout) for t in targets]

    with_validator = [r for r in results if r.has_validator]
    hit = [r for r in with_validator if r.any_304]
    etag_targets = [r for r in results if r.etag]
    etag_304 = [r for r in etag_targets if any(p.kind == "etag" and p.is_304 for p in r.probes)]
    lm_targets = [r for r in results if r.last_modified]
    lm_304 = [r for r in lm_targets if any(p.kind == "last_modified" and p.is_304 for p in r.probes)]

    denom = len(with_validator)
    hit_rate = (len(hit) / denom) if denom else 0.0
    passed = denom > 0 and hit_rate >= args.min_hit_rate

    summary = {
        "impersonate": DEFAULT_IMPERSONATE,
        "targets": len(results),
        "with_validator": denom,
        "any_304": len(hit),
        "hit_rate": round(hit_rate, 3),
        "min_hit_rate": args.min_hit_rate,
        "etag": {"targets": len(etag_targets), "got_304": len(etag_304)},
        "last_modified": {"targets": len(lm_targets), "got_304": len(lm_304)},
        "verdict": "PASS" if passed else "FAIL",
    }
    report = {"summary": summary, "results": [asdict(r) for r in results]}

    # Human-readable to stderr (stdout stays clean if piped).
    print(f"\nR1 conditional-request probe (impersonate={DEFAULT_IMPERSONATE})", file=sys.stderr)
    for r in results:
        if r.error:
            print(f"  [{r.category}] {r.url}  ERROR: {r.error}", file=sys.stderr)
            continue
        val = []
        if r.etag:
            val.append("ETag")
        if r.last_modified:
            val.append("Last-Modified")
        vstr = "+".join(val) or "no-validator"
        marks = " ".join(f"{p.kind}:{p.status}{'✓' if p.is_304 else '✗'}" for p in r.probes) or "—"
        print(f"  [{r.category}] base={r.base_status} {vstr:<22} {marks}  {r.url}", file=sys.stderr)
    print(
        f"\n  validator-exposing targets: {denom}/{len(results)}"
        f"  |  304 hit: {len(hit)}  |  hit-rate: {hit_rate:.2f}"
        f"  (ETag {len(etag_304)}/{len(etag_targets)}, "
        f"Last-Modified {len(lm_304)}/{len(lm_targets)})",
        file=sys.stderr,
    )
    print(f"  VERDICT: {summary['verdict']}"
          f"  ({'PR 3 viable' if passed else 'defer ETag/Last-Modified to v0.4 — no non-impersonate downgrade'})",
          file=sys.stderr)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  wrote {out}", file=sys.stderr)
    else:
        print(json.dumps(report, indent=2, ensure_ascii=False))

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
