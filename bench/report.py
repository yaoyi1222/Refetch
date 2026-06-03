"""Render a run.json into a Markdown comparison report.

Usage:
  .venv/bin/python -m bench.report bench/results/run.json > bench/results/report.md
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean


def _pct_saving(baseline: int, new: int) -> str:
    if baseline <= 0:
        return "—"
    saved = (baseline - new) / baseline * 100
    sign = "↓" if saved >= 0 else "↑"
    return f"{sign}{abs(saved):.1f}%"


def _fmt_int(n: int | None) -> str:
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _outcome_for(row: dict, mode: str) -> dict | None:
    for o in row["outcomes"]:
        if o["mode"] == mode:
            return o
    return None


def render(data: dict) -> str:
    rows = data["rows"]
    out: list[str] = []
    out.append("# WebFetch token-consumption benchmark\n")
    out.append(f"_Token counter: `{data['token_strategy']}`_\n")
    out.append(
        "Two columns matter: **tokens returned to the model** (what eats your "
        "context window) and **success**. `plus_auto` is `fetch_url(url)` with "
        "no extra hints; `plus_selector` adds a CSS selector chosen from the "
        "router's `suggested_selectors` hint (i.e. what a model would naturally "
        "do on the second call).\n"
    )

    out.append("## Per-URL results\n")
    out.append(
        "| URL | Category | Baseline tok | Plus auto tok | Plus selector tok | "
        "Saving (auto) | Saving (selector) | Baseline ok | Plus auto ok | Plus selector ok | Plus strategy |"
    )
    out.append("|---|---|---:|---:|---:|---:|---:|:-:|:-:|:-:|---|")

    cat_rollup: dict[str, list[tuple[int, int, int | None]]] = defaultdict(list)
    success: dict[str, list[bool]] = defaultdict(list)

    for r in rows:
        b = _outcome_for(r, "baseline")
        a = _outcome_for(r, "plus_auto")
        s = _outcome_for(r, "plus_selector")
        b_tok = b["tokens_returned"] if b and b["ok"] else 0
        a_tok = a["tokens_returned"] if a and a["ok"] else 0
        s_tok = s["tokens_returned"] if s and s["ok"] else None

        cat_rollup[r["category"]].append((b_tok, a_tok, s_tok))
        success["baseline"].append(bool(b and b["ok"]))
        success["plus_auto"].append(bool(a and a["ok"]))
        if s is not None:
            success["plus_selector"].append(bool(s["ok"]))

        url_disp = r["url"]
        if len(url_disp) > 55:
            url_disp = url_disp[:52] + "…"
        out.append(
            "| {url} | {cat} | {bt} | {at} | {st} | {sa} | {ss} | "
            "{bok} | {aok} | {sok} | {strat} |".format(
                url=url_disp,
                cat=r["category"],
                bt=_fmt_int(b_tok) if b and b["ok"] else f"❌ {b['error_detail'] or ''}"[:30],
                at=_fmt_int(a_tok) if a and a["ok"] else f"❌ {a['error_code'] or ''}"[:20],
                st=(_fmt_int(s_tok) if s and s["ok"] else (f"❌ {s['error_code']}"[:20] if s else "—")),
                sa=_pct_saving(b_tok, a_tok) if b and b["ok"] and a and a["ok"] else "—",
                ss=_pct_saving(b_tok, s_tok or 0) if b and b["ok"] and s and s["ok"] else "—",
                bok="✅" if b and b["ok"] else "❌",
                aok="✅" if a and a["ok"] else "❌",
                sok=("✅" if s and s["ok"] else ("❌" if s else "—")),
                strat=(a["strategy_used"] if a and a["ok"] else "—"),
            )
        )

    out.append("\n## Category roll-up (mean tokens, successful fetches only)\n")
    out.append("| Category | n | Baseline | Plus auto | Plus selector | Saving auto | Saving selector |")
    out.append("|---|---:|---:|---:|---:|---:|---:|")
    for cat, triples in cat_rollup.items():
        b_vals = [t[0] for t in triples if t[0] > 0]
        a_vals = [t[1] for t in triples if t[1] > 0]
        s_vals = [t[2] for t in triples if t[2] is not None and t[2] > 0]
        if not b_vals or not a_vals:
            continue
        out.append(
            f"| {cat} | {len(triples)} | "
            f"{_fmt_int(int(mean(b_vals)))} | {_fmt_int(int(mean(a_vals)))} | "
            f"{_fmt_int(int(mean(s_vals))) if s_vals else '—'} | "
            f"{_pct_saving(int(mean(b_vals)), int(mean(a_vals)))} | "
            f"{_pct_saving(int(mean(b_vals)), int(mean(s_vals))) if s_vals else '—'} |"
        )

    out.append("\n## Success rates\n")
    for mode, oks in success.items():
        n = len(oks)
        if n == 0:
            continue
        rate = sum(oks) / n * 100
        out.append(f"- **{mode}**: {sum(oks)}/{n}  ({rate:.0f}%)")

    out.append("\n## Notes\n")
    out.append("- `tokens_returned` is what the model actually sees in the response. "
               "When `truncated=true`, `plus` writes the full content to a dump file "
               "instead of returning it inline — that's deliberate and counts as a saving "
               "(the model can read the dump file selectively).")
    out.append("- Baseline failure on a URL counts as 0 tokens; the row still appears so "
               "you can see *why* it failed (the cell shows the HTTP error).")
    out.append("- Run multiple `--rounds` to smooth out network jitter; tokens are "
               "deterministic per response so only `elapsed_ms` benefits from averaging.")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: bench.report <run.json>", file=sys.stderr)
        return 2
    data = json.loads(Path(args[0]).read_text())
    sys.stdout.write(render(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
