"""PR 1a — `headers` + `include_tags` / `exclude_tags`.

Offline tests covering:
  - L1 (fetch_http) and L2 (fetch_browser) both receive the user `headers` dict.
  - `exclude_tags` strips additional tag names beyond the built-in script/style
    block in `_REMOVE_TAGS`.
  - `include_tags` non-empty skips the automatic <main>/<article> scoping (the
    explicit semantics chosen in 02.md so an `aside`-only request doesn't
    silently return nothing on pages with a single <main> not containing the
    aside).
  - Backwards-compat: a default `fetch_url(url=...)` call produces the same
    keys + same body content as before — none of the new params change the
    response when left at their defaults.
"""

from unittest.mock import patch

import pytest

from lightcrawl import content as content_mod
from lightcrawl.fetch_http import HttpResult
from lightcrawl.router import FetchRequest, Router


@pytest.fixture
def router():
    return Router()


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    monkeypatch.setattr("lightcrawl.paths.ROOT", tmp_path)
    monkeypatch.setattr("lightcrawl.paths.DUMPS", tmp_path / "dumps")
    monkeypatch.setattr("lightcrawl.paths.PROFILES", tmp_path / "profiles")
    monkeypatch.setattr("lightcrawl.paths.LOGS", tmp_path / "logs")
    monkeypatch.setattr("lightcrawl.content.DUMPS", tmp_path / "dumps")
    monkeypatch.setattr("lightcrawl.auth.PROFILES", tmp_path / "profiles")
    (tmp_path / "dumps").mkdir(parents=True)
    (tmp_path / "profiles").mkdir(parents=True)


# Body must be ≥200 chars to avoid `_should_escalate_to_browser`'s "tiny body"
# heuristic. Otherwise the router falls through to the real Playwright path.
_LONG_HTML = (
    "<html><head><title>T</title></head><body>"
    "<article><h1>Headline</h1>"
    "<p>body text long enough to extract properly with readability "
    "and then markdownify it nicely without tripping any of the "
    "escalation heuristics that look at tiny bodies.</p></article>"
    "</body></html>"
)


def _ok(html: str) -> HttpResult:
    return HttpResult(
        final_url="https://example.com/",
        status_code=200,
        text=html,
        content_type="text/html",
        elapsed_ms=5,
    )


# ---- headers passthrough -----------------------------------------------------


async def test_headers_passed_to_l1(router):
    seen = {}

    def fake_fetch(url, *, timeout, headers=None, **_kwargs):
        seen["headers"] = headers
        return _ok(_LONG_HTML)

    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"), \
         patch("lightcrawl.fetch_http.fetch", side_effect=fake_fetch):
        out = await router.fetch(
            FetchRequest(
                url="https://example.com/",
                headers={"X-lightcrawl-Test": "1", "Referer": "https://r.example/"},
            )
        )
    assert out["ok"] is True
    assert seen["headers"] == {"X-lightcrawl-Test": "1", "Referer": "https://r.example/"}


async def test_headers_default_empty_does_not_pass_dict_to_l1(router):
    """When the user didn't supply headers, we pass `None` to curl_cffi, not
    `{}` — keeps the request shape byte-identical to v0.1 for the default call.
    """
    seen = {}

    def fake_fetch(url, *, timeout, headers=None, **_kwargs):
        seen["headers"] = headers
        # Use _LONG_HTML (≥200 chars) so the router stays on L1: a tiny body
        # trips _should_escalate_to_browser's "len(html) < 200" heuristic and
        # falls through to the real Playwright path, which has no browser in CI.
        return _ok(_LONG_HTML)

    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"), \
         patch("lightcrawl.fetch_http.fetch", side_effect=fake_fetch):
        await router.fetch(FetchRequest(url="https://example.com/"))
    assert seen["headers"] is None


async def test_headers_passed_to_l2(router):
    """When L1 forces escalation (CF block), the headers must arrive at L2 too."""
    seen = {}

    def fake_l1(url, *, timeout, headers=None, **_kwargs):
        # Trigger escalation: 403 status
        return HttpResult(
            final_url=url, status_code=403, text="<html></html>",
            content_type="text/html", elapsed_ms=5,
        )

    from lightcrawl import fetch_browser as fb_mod

    async def fake_l2(pool, url, *, wait_for=None, timeout=10.0, storage_state=None,
                     headers=None, **_kwargs):
        seen["headers"] = headers
        return fb_mod.BrowserResult(
            final_url=url, status_code=200,
            text="<html><body><article><p>body body body.</p></article></body></html>",
            content_type="text/html", elapsed_ms=10,
        )

    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"), \
         patch("lightcrawl.fetch_http.fetch", side_effect=fake_l1), \
         patch("lightcrawl.fetch_browser.fetch", side_effect=fake_l2):
        out = await router.fetch(
            FetchRequest(
                url="https://example.com/",
                headers={"Cookie": "sid=abc"},
            )
        )
    assert out["ok"] is True
    assert seen["headers"] == {"Cookie": "sid=abc"}


# ---- include_tags / exclude_tags --------------------------------------------


HTML_WITH_NAV_ASIDE_MAIN = """
<html>
<head><title>T</title></head>
<body>
  <nav><a href="/x">nav link</a></nav>
  <main>
    <article>
      <h1>Main Heading</h1>
      <p>Primary body content sentence here for extraction.</p>
    </article>
  </main>
  <aside>
    <h2>Side Heading</h2>
    <p>Side note paragraph.</p>
  </aside>
  <footer>footer text</footer>
</body>
</html>
"""


def test_exclude_tags_strips_nav():
    """`exclude_tags=['nav']` removes the nav block from the cleaned DOM
    before main/article auto-scoping. Default behavior (no exclude) keeps it
    because <nav> is deliberately NOT in `_REMOVE_TAGS` (see content.py)."""
    out = content_mod.html_to_markdown(HTML_WITH_NAV_ASIDE_MAIN, exclude_tags=["nav"])
    assert "nav link" not in out.markdown
    assert "Main Heading" in out.markdown


def test_include_tags_skips_auto_main_scoping():
    """`include_tags=['aside']` must skip the automatic <main> scoping —
    otherwise on this fixture the result would be empty (aside lives outside
    <main>). The 02.md semantics decision is:
        non-empty include_tags ⇒ ignore _select_target's main/article fallback
        and gather every matching tag in document order."""
    out = content_mod.html_to_markdown(HTML_WITH_NAV_ASIDE_MAIN, include_tags=["aside"])
    assert "Side Heading" in out.markdown
    # And main's content is NOT pulled in (we only asked for aside)
    assert "Main Heading" not in out.markdown


def test_include_tags_no_double_pull_on_nested():
    """Regression: when include_tags matches both an ancestor and its
    descendant (e.g. ['article', 'h1'] on `<article><h1>X</h1><p>P</p></article>`),
    `lxml.Element.append()` would reparent the descendant out of the moved
    ancestor — producing duplicate or out-of-order content. The fix walks
    matches in document order and drops any node whose ancestor was already
    selected."""
    html = "<html><body><article><h1>X</h1><p>P</p></article></body></html>"
    out = content_mod.html_to_markdown(html, include_tags=["article", "h1"])
    assert out.markdown.count("X") == 1
    # Document order inside the kept article: h1 (X) precedes p (P)
    assert out.markdown.index("X") < out.markdown.index("P")


def test_include_tags_multiple_kept_in_document_order():
    out = content_mod.html_to_markdown(
        HTML_WITH_NAV_ASIDE_MAIN, include_tags=["article", "aside"]
    )
    assert "Main Heading" in out.markdown
    assert "Side Heading" in out.markdown
    # article precedes aside in source — markdown order should preserve this
    assert out.markdown.index("Main Heading") < out.markdown.index("Side Heading")


def test_include_tags_no_match_falls_back_to_body():
    """If no element matches include_tags, fall back to whole body rather than
    silently producing empty markdown (caller can still see they got something
    and adjust their tag list)."""
    out = content_mod.html_to_markdown(
        HTML_WITH_NAV_ASIDE_MAIN, include_tags=["nonexistent-tag"]
    )
    assert "Main Heading" in out.markdown  # body content present


def test_exclude_tags_combines_with_include_tags():
    """exclude_tags happens during _clean_dom (before _select_target);
    include_tags drives _select_target. They should compose."""
    out = content_mod.html_to_markdown(
        HTML_WITH_NAV_ASIDE_MAIN,
        include_tags=["article", "aside"],
        exclude_tags=["footer"],  # ignored anyway in this fixture but proves no crash
    )
    assert "Main Heading" in out.markdown
    assert "Side Heading" in out.markdown
    assert "footer text" not in out.markdown


# ---- input validation at the MCP boundary ----------------------------------


def test_clean_tags_rejects_malformed():
    """Regression: empty strings and CSS-selector-like inputs must be dropped
    before they reach lxml's xpath builder, otherwise `XPathEvalError` escapes
    the "errors are values, not exceptions" boundary contract."""
    from lightcrawl.cli import _clean_tags

    assert _clean_tags(["article", "", "nav"]) == ["article", "nav"]
    assert _clean_tags(["div[onclick]"]) == []          # CSS attr selector
    assert _clean_tags(["nav, footer"]) == []            # comma-joined typo
    assert _clean_tags(["MAIN"]) == ["main"]             # case-normalized
    assert _clean_tags([" article "]) == ["article"]    # whitespace-stripped
    assert _clean_tags([123, None, ""]) == []
    assert _clean_tags("article") == []                  # not a list
    assert _clean_tags(None) == []


# ---- backwards-compat: default call response shape --------------------------


async def test_default_fetch_url_response_keys_unchanged(router):
    """The default call (no new params) must produce the exact same top-level
    keys as before — the v0.2 acceptance gate."""
    # Body must be >200 chars to avoid the "tiny body" L2-escalation
    # heuristic in `_should_escalate_to_browser` (otherwise the router
    # bypasses the L1 mock and tries the real Playwright path).
    fake = _ok(
        "<html><head><title>T</title></head><body>"
        "<article><h1>Hi</h1>"
        "<p>body text long enough to extract properly with readability "
        "and then markdownify it nicely without tripping any of the "
        "escalation heuristics that look at tiny bodies.</p></article>"
        "</body></html>"
    )
    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"), \
         patch("lightcrawl.fetch_http.fetch", return_value=fake):
        out = await router.fetch(FetchRequest(url="https://example.com/"))

    expected_keys = {
        "ok", "url", "final_url", "strategy_used", "fetched_at", "title",
        "content", "content_truncated", "dump_path", "metadata", "attempts",
        "headings",
    }
    assert expected_keys.issubset(out.keys())
    # No new top-level keys leaked by accident
    assert set(out.keys()) == expected_keys
    assert out["ok"] is True
    assert "Hi" in out["content"]
