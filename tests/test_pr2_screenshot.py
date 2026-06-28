"""PR 2 — Screenshot output + `_l1_incapable` helper + unified `screenshots[]`.

Offline tests cover:
  - `_l1_incapable` helper: True for screenshot output formats, False otherwise.
    The helper is the single point future L2-only features (`actions` in PR 5,
    `block_ads` in v0.3) extend, so it gets independent unit tests.
  - `output_format="screenshot"` and `"markdown+screenshot"` force the request
    through L2 even when L1 would have succeeded. `_l1_incapable` is called
    AFTER `_looks_like_binary_url` and `validate_url` so SSRF + binary guards
    are not bypassed by the L2-only field — locked by a binary-URL test.
  - `_format_body` returns "" for screenshot-only, markdown for combined.
  - `_write_screenshot` lands a PNG at `{SCREENSHOTS}/{sha1(url)}.png`,
    overwrites the prior file on a second call (no timestamp suffix),
    and surfaces under `screenshots: [{"stage": "final", "path": ...}]`.
  - Backwards-compat: a default call still has no `screenshots` key.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lightcrawl import fetch_browser as fb_mod
from lightcrawl.errors import ErrorCode
from lightcrawl.fetch_browser import BrowserResult
from lightcrawl.fetch_http import HttpResult
from lightcrawl.router import (
    FetchRequest,
    Router,
    _format_body,
    _l1_incapable,
    _write_screenshot,
)


@pytest.fixture
def router():
    return Router()


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    monkeypatch.setattr("lightcrawl.paths.ROOT", tmp_path)
    monkeypatch.setattr("lightcrawl.paths.DUMPS", tmp_path / "dumps")
    monkeypatch.setattr("lightcrawl.paths.PROFILES", tmp_path / "profiles")
    monkeypatch.setattr("lightcrawl.paths.LOGS", tmp_path / "logs")
    monkeypatch.setattr("lightcrawl.paths.SCREENSHOTS", tmp_path / "screenshots")
    monkeypatch.setattr("lightcrawl.content.DUMPS", tmp_path / "dumps")
    monkeypatch.setattr("lightcrawl.auth.PROFILES", tmp_path / "profiles")
    (tmp_path / "dumps").mkdir(parents=True)
    (tmp_path / "profiles").mkdir(parents=True)


_LONG_HTML = (
    "<html><head><title>T</title></head><body>"
    "<article><h1>Headline</h1>"
    "<p>body text long enough to bypass the tiny-body escalation "
    "heuristic and produce stable markdown output.</p></article>"
    "</body></html>"
)


# ---- _l1_incapable unit tests ----------------------------------------------


def test_l1_incapable_screenshot_only():
    assert _l1_incapable(FetchRequest(url="https://example.com/", output_format="screenshot")) is True


def test_l1_incapable_markdown_plus_screenshot():
    assert _l1_incapable(FetchRequest(url="https://example.com/", output_format="markdown+screenshot")) is True


def test_l1_incapable_default_markdown_is_false():
    assert _l1_incapable(FetchRequest(url="https://example.com/")) is False


def test_l1_incapable_html_text_are_false():
    """These formats can be served by L1; helper must not force-upgrade."""
    assert _l1_incapable(FetchRequest(url="https://example.com/", output_format="html")) is False
    assert _l1_incapable(FetchRequest(url="https://example.com/", output_format="text")) is False


# ---- _format_body unit tests -----------------------------------------------


def test_format_body_screenshot_returns_empty_string():
    from lightcrawl.content import ExtractedContent
    extracted = ExtractedContent(title="t", markdown="MD", plain_text="PT")
    assert _format_body("screenshot", extracted, "<html>raw</html>") == ""


def test_format_body_markdown_plus_screenshot_returns_markdown():
    from lightcrawl.content import ExtractedContent
    extracted = ExtractedContent(title="t", markdown="MD", plain_text="PT")
    assert _format_body("markdown+screenshot", extracted, "<html>raw</html>") == "MD"


# ---- _write_screenshot ------------------------------------------------------


def test_write_screenshot_lands_at_sha1_path(tmp_path, monkeypatch):
    # `_write_screenshot` does `from .paths import SCREENSHOTS` lazily on
    # each call, so patching `lightcrawl.paths.SCREENSHOTS` is sufficient and
    # correct. If a future refactor hoists the import to module scope,
    # ADD a `lightcrawl.router.SCREENSHOTS` patch alongside — don't leave a
    # `raising=False` no-op here pretending to cover both modes.
    monkeypatch.setattr("lightcrawl.paths.SCREENSHOTS", tmp_path / "screenshots")

    png = b"\x89PNG\r\n\x1a\n" + b"X" * 16
    path = _write_screenshot("https://example.com/foo", png)

    assert path.startswith(str(tmp_path / "screenshots"))
    assert path.endswith(".png")

    import hashlib as h
    expected_digest = h.sha1(b"https://example.com/foo").hexdigest()[:16]
    assert path.endswith(f"{expected_digest}.png")

    from pathlib import Path
    assert Path(path).read_bytes() == png


def test_write_screenshot_overwrites_same_url(tmp_path, monkeypatch):
    """Spec: 'overwrite mode, no timestamp'. Second screenshot at the same
    URL must land at the same path and clobber the prior bytes."""
    monkeypatch.setattr("lightcrawl.paths.SCREENSHOTS", tmp_path / "screenshots")

    from pathlib import Path

    first = _write_screenshot("https://example.com/foo", b"FIRST_PNG_BYTES")
    second = _write_screenshot("https://example.com/foo", b"SECOND_PNG_BYTES")

    assert first == second
    assert Path(first).read_bytes() == b"SECOND_PNG_BYTES"


# ---- end-to-end via Router: screenshot output forces L2, lands in response --


async def test_screenshot_output_forces_l2_even_when_l1_would_succeed(
    router, tmp_path, monkeypatch
):
    """Even if L1 would happily return 200, an `output_format=\"screenshot\"`
    request must skip L1 entirely and go to L2. Without this the screenshot
    would never get taken."""
    monkeypatch.setattr("lightcrawl.paths.SCREENSHOTS", tmp_path / "screenshots")

    l1_called = {"count": 0}

    def fake_l1(url, *, timeout, **_kwargs):
        l1_called["count"] += 1
        return HttpResult(
            final_url=url, status_code=200, text=_LONG_HTML,
            content_type="text/html", elapsed_ms=5,
        )

    async def fake_l2(pool, url, *, screenshot=False, **_kwargs):
        return BrowserResult(
            final_url=url, status_code=200, text=_LONG_HTML,
            content_type="text/html", elapsed_ms=10,
            screenshot_png=b"\x89PNG\r\n\x1a\n" + b"Y" * 16 if screenshot else None,
        )

    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"), \
         patch("lightcrawl.fetch_http.fetch", side_effect=fake_l1), \
         patch("lightcrawl.fetch_browser.fetch", side_effect=fake_l2):
        out = await router.fetch(
            FetchRequest(url="https://example.com/", output_format="screenshot")
        )

    assert l1_called["count"] == 0, "L1 must be skipped when output is screenshot-only"
    assert out["ok"] is True
    assert out["strategy_used"] == "browser"
    # Screenshot-only body is empty
    assert out["content"] == ""
    assert out["content_truncated"] is False
    assert out["dump_path"] is None
    # screenshots[] populated under the unified shape
    assert "screenshots" in out
    assert len(out["screenshots"]) == 1
    assert out["screenshots"][0]["stage"] == "final"
    assert out["screenshots"][0]["path"].endswith(".png")


async def test_markdown_plus_screenshot_returns_both(router, tmp_path, monkeypatch):
    monkeypatch.setattr("lightcrawl.paths.SCREENSHOTS", tmp_path / "screenshots")

    async def fake_l2(pool, url, *, screenshot=False, **_kwargs):
        return BrowserResult(
            final_url=url, status_code=200, text=_LONG_HTML,
            content_type="text/html", elapsed_ms=10,
            screenshot_png=b"\x89PNG\r\n\x1a\n" + b"Z" * 16 if screenshot else None,
        )

    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"), \
         patch("lightcrawl.fetch_browser.fetch", side_effect=fake_l2):
        out = await router.fetch(
            FetchRequest(url="https://example.com/", output_format="markdown+screenshot")
        )

    assert out["ok"] is True
    # Markdown body present
    assert "Headline" in out["content"]
    # Screenshot also present
    assert len(out["screenshots"]) == 1
    assert out["screenshots"][0]["stage"] == "final"


# ---- SSRF / binary guards must precede _l1_incapable ----------------------


async def test_binary_url_rejected_even_with_screenshot_format(router):
    """The call-order constraint in `_l1_incapable`'s docstring: it MUST run
    AFTER `_looks_like_binary_url`. Otherwise a .zip URL with
    output_format=screenshot would skip the binary guard and try to take a
    screenshot of a downloadable file (Playwright errors are uglier than
    our `UNSUPPORTED_CONTENT_TYPE`). .pdf no longer binary since PR 4."""
    out = await router.fetch(
        FetchRequest(url="https://example.com/file.zip", output_format="screenshot")
    )
    assert out["ok"] is False
    assert out["error_code"] == ErrorCode.UNSUPPORTED_CONTENT_TYPE.value
    # No screenshot dir lookup, no Playwright launch
    assert "screenshots" not in out


async def test_ssrf_rejected_even_with_screenshot_format(router):
    """Same call-order rule for SSRF: a private IP URL must be blocked
    before `_l1_incapable` could force it through Playwright."""
    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="127.0.0.1"):
        out = await router.fetch(
            FetchRequest(url="http://localhost/admin", output_format="screenshot")
        )
    assert out["ok"] is False
    assert out["error_code"] == ErrorCode.URL_NOT_ALLOWED.value
    assert "screenshots" not in out


# ---- backwards compat -------------------------------------------------------


async def test_default_call_has_no_screenshots_key(router):
    """Compat lock from PR 1a/1b. Default `output_format="markdown"` must
    produce a response whose top-level key set is byte-identical to PR 1b
    — i.e. no `screenshots` field leaking in just because PR 2 introduced
    it."""
    fake = HttpResult(
        final_url="https://example.com/",
        status_code=200,
        text=_LONG_HTML,
        content_type="text/html",
        elapsed_ms=5,
    )
    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"), \
         patch("lightcrawl.fetch_http.fetch", return_value=fake):
        out = await router.fetch(FetchRequest(url="https://example.com/"))

    expected_keys = {
        "ok", "url", "final_url", "strategy_used", "fetched_at", "title",
        "content", "content_truncated", "dump_path", "full_content_hash",
        "metadata", "attempts", "headings",
    }
    assert set(out.keys()) == expected_keys
    assert "screenshots" not in out


# ---- fetch_browser plumbing: real-path test, not a tautology --------------


async def test_screenshot_kwarg_reaches_real_page_screenshot():
    """Lock the wiring inside the **real** `fetch_browser.fetch`:
    `screenshot=True` must call `page.screenshot(full_page=True, type="png")`
    and the returned bytes must land on `BrowserResult.screenshot_png`.
    Patches one layer deeper than `fetch_browser.fetch` (per PR 1b review)
    so a refactor that drops the `page.screenshot()` call is caught."""
    captured_screenshot_kwargs = {}

    async def fake_page_screenshot(**kwargs):
        captured_screenshot_kwargs.update(kwargs)
        return b"\x89PNG\r\n\x1a\n" + b"P" * 32

    fake_response = MagicMock(status=200, headers={"content-type": "text/html"})
    fake_page = AsyncMock()
    fake_page.goto = AsyncMock(return_value=fake_response)
    fake_page.content = AsyncMock(return_value=_LONG_HTML)
    fake_page.screenshot = fake_page_screenshot
    fake_page.close = AsyncMock()
    fake_page.url = "https://example.com/"

    fake_ctx = AsyncMock()
    fake_ctx.new_page = AsyncMock(return_value=fake_page)

    @asynccontextmanager
    async def fake_context(self, *, storage_state=None, **context_kwargs):
        yield fake_ctx

    pool = fb_mod.BrowserPool()
    with patch.object(fb_mod.BrowserPool, "context", fake_context), \
         patch.object(fb_mod._STEALTH, "apply_stealth_async", AsyncMock()):
        r = await fb_mod.fetch(pool, "https://example.com/", screenshot=True)

    assert captured_screenshot_kwargs.get("full_page") is True
    assert captured_screenshot_kwargs.get("type") == "png"
    assert r.screenshot_png is not None
    assert r.screenshot_png.startswith(b"\x89PNG")


async def test_screenshot_default_false_does_not_call_page_screenshot():
    """Mirror: default `screenshot=False` must NOT invoke
    `page.screenshot()`. Otherwise every fetch pays the screenshot cost."""
    page_screenshot_calls = {"n": 0}

    async def fake_page_screenshot(**kwargs):
        page_screenshot_calls["n"] += 1
        return b""

    fake_response = MagicMock(status=200, headers={"content-type": "text/html"})
    fake_page = AsyncMock()
    fake_page.goto = AsyncMock(return_value=fake_response)
    fake_page.content = AsyncMock(return_value=_LONG_HTML)
    fake_page.screenshot = fake_page_screenshot
    fake_page.close = AsyncMock()
    fake_page.url = "https://example.com/"

    fake_ctx = AsyncMock()
    fake_ctx.new_page = AsyncMock(return_value=fake_page)

    @asynccontextmanager
    async def fake_context(self, *, storage_state=None, **context_kwargs):
        yield fake_ctx

    pool = fb_mod.BrowserPool()
    with patch.object(fb_mod.BrowserPool, "context", fake_context), \
         patch.object(fb_mod._STEALTH, "apply_stealth_async", AsyncMock()):
        r = await fb_mod.fetch(pool, "https://example.com/")  # default screenshot=False

    assert page_screenshot_calls["n"] == 0
    assert r.screenshot_png is None
