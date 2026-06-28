"""PR 1b — `mobile` + `remove_base64_images`.

Offline tests covering:
  - `mobile=True` switches the L1 impersonate profile to the iOS Safari one
    (caller-supplied or routed by the router) — verified by mock observation
    on `fetch_http.fetch`.
  - `mobile=True` on L2 unpacks Playwright's "iPhone 13" device descriptor
    into the BrowserContext kwargs — verified by mocking `pool.context()`.
  - `remove_base64_images=True` strips `<img src="data:...">` elements while
    leaving regular `<img src="https://...">` in the markdown.
  - Default-args call (`mobile=False`, `remove_base64_images=False`) keeps the
    v0.1/PR 1a byte-identical response shape.

The mobile **production validation** is the network experiment recorded in
issue #17 — these tests only assert the wiring.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lightcrawl import content as content_mod
from lightcrawl import fetch_browser as fb_mod
from lightcrawl import fetch_http as fh_mod
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


_LONG_HTML = (
    "<html><head><title>T</title></head><body>"
    "<article><h1>Headline</h1>"
    "<p>body text long enough to bypass the tiny-body escalation "
    "heuristic and produce stable markdown output.</p></article>"
    "</body></html>"
)


def _ok(html: str = _LONG_HTML) -> HttpResult:
    return HttpResult(
        final_url="https://example.com/",
        status_code=200,
        text=html,
        content_type="text/html",
        elapsed_ms=5,
    )


# ---- L1: mobile flips the impersonate profile ------------------------------


async def test_mobile_routes_to_safari_impersonate_on_l1(router):
    """`mobile=True` must pass curl_cffi's iOS Safari profile, not the
    desktop chrome120 default. Flipping UA only would desync UA from the
    TLS fingerprint — a known bot signal — so the whole impersonate value
    has to change."""
    seen = {}

    def fake_fetch(url, *, timeout, headers=None, impersonate=None, **_kwargs):
        seen["impersonate"] = impersonate
        return _ok()

    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"), \
         patch("lightcrawl.fetch_http.fetch", side_effect=fake_fetch):
        await router.fetch(FetchRequest(url="https://example.com/", mobile=True))
    assert seen["impersonate"] == fh_mod.MOBILE_IMPERSONATE
    assert "ios" in seen["impersonate"].lower()  # sanity check the constant


async def test_default_mobile_false_uses_desktop_impersonate(router):
    """Compat gate: when the caller doesn't ask for mobile, L1 still uses
    the chrome120 default — no behavior drift from v0.1."""
    seen = {}

    def fake_fetch(url, *, timeout, headers=None, impersonate=None, **_kwargs):
        seen["impersonate"] = impersonate
        return _ok()

    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"), \
         patch("lightcrawl.fetch_http.fetch", side_effect=fake_fetch):
        await router.fetch(FetchRequest(url="https://example.com/"))
    assert seen["impersonate"] == fh_mod.DEFAULT_IMPERSONATE
    assert seen["impersonate"] == "chrome120"


# ---- L2: mobile unpacks the iPhone 13 device descriptor --------------------


async def test_mobile_unpacks_iphone_device_into_browser_context():
    """Lock the wiring inside the **real** `fetch_browser.fetch` body:
    `mobile=True` must call `pool.mobile_context_kwargs()` and unpack the
    result into `pool.context(...)`. Patching `fetch_browser.fetch` itself
    would be a tautology — the fake would just reimplement the very logic
    under test (reviewer #1 in PR #28). Instead we patch one layer deeper:
    the pool's `context()` and `mobile_context_kwargs()` methods, plus
    stealth + page primitives, then call the real `fetch()`."""
    seen_kwargs: dict = {}

    fake_iphone_descriptor = {
        "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 26_0 like Mac OS X) Safari/604.1",
        "viewport": {"width": 390, "height": 844},
        "is_mobile": True,
        "has_touch": True,
        "device_scale_factor": 3,
    }

    async def fake_mobile_kwargs(self):
        # Return the descriptor so a deepcopy or shallow copy in the helper
        # has no visible effect on this test — we only assert the contract,
        # not the implementation. The deepcopy guard is verified separately.
        return dict(fake_iphone_descriptor)

    fake_response = MagicMock()
    fake_response.status = 200
    fake_response.headers = {"content-type": "text/html"}
    fake_page = AsyncMock()
    fake_page.goto = AsyncMock(return_value=fake_response)
    fake_page.content = AsyncMock(return_value=_LONG_HTML)
    fake_page.close = AsyncMock()
    fake_page.url = "https://example.com/"
    fake_page.wait_for_selector = AsyncMock()
    fake_page.wait_for_load_state = AsyncMock()

    fake_ctx = AsyncMock()
    fake_ctx.new_page = AsyncMock(return_value=fake_page)

    @asynccontextmanager
    async def fake_context(self, *, storage_state=None, **context_kwargs):
        seen_kwargs.update(context_kwargs)
        yield fake_ctx

    pool = fb_mod.BrowserPool()

    with patch.object(fb_mod.BrowserPool, "mobile_context_kwargs", fake_mobile_kwargs), \
         patch.object(fb_mod.BrowserPool, "context", fake_context), \
         patch.object(fb_mod._STEALTH_MOBILE, "apply_stealth_async", AsyncMock()), \
         patch.object(fb_mod._STEALTH, "apply_stealth_async", AsyncMock()):
        result = await fb_mod.fetch(pool, "https://example.com/", mobile=True)

    # The contract under test: real fetch() unpacked the device descriptor
    assert seen_kwargs.get("is_mobile") is True
    assert seen_kwargs.get("has_touch") is True
    assert "iPhone" in seen_kwargs.get("user_agent", "")
    assert seen_kwargs.get("viewport", {}).get("width") == 390
    # Returned result is sane
    assert result.status_code == 200


async def test_mobile_false_does_not_unpack_iphone_kwargs():
    """Mirror of the test above for the negative case: with mobile=False,
    `mobile_context_kwargs()` must NOT be called (the pool keeps its
    desktop defaults). If a future refactor swaps the conditional, this
    test catches it without us shipping a silent desktop→mobile regression."""
    mobile_kwargs_called = {"count": 0}

    async def fake_mobile_kwargs(self):
        mobile_kwargs_called["count"] += 1
        return {}

    fake_response = MagicMock(status=200, headers={"content-type": "text/html"})
    fake_page = AsyncMock()
    fake_page.goto = AsyncMock(return_value=fake_response)
    fake_page.content = AsyncMock(return_value=_LONG_HTML)
    fake_page.close = AsyncMock()
    fake_page.url = "https://example.com/"
    fake_ctx = AsyncMock()
    fake_ctx.new_page = AsyncMock(return_value=fake_page)

    @asynccontextmanager
    async def fake_context(self, *, storage_state=None, **context_kwargs):
        yield fake_ctx

    pool = fb_mod.BrowserPool()

    with patch.object(fb_mod.BrowserPool, "mobile_context_kwargs", fake_mobile_kwargs), \
         patch.object(fb_mod.BrowserPool, "context", fake_context), \
         patch.object(fb_mod._STEALTH, "apply_stealth_async", AsyncMock()):
        await fb_mod.fetch(pool, "https://example.com/", mobile=False)

    assert mobile_kwargs_called["count"] == 0


async def test_mobile_selects_mobile_stealth_instance():
    """Reviewer #3: when mobile=True the iOS-aware `_STEALTH_MOBILE` must
    be used instead of the desktop `_STEALTH`, otherwise stealth's default
    `navigator.platform="Win32"` + `navigator.vendor="Google Inc."` ride
    on top of an iPhone UA and create exactly the cross-layer
    inconsistency we're trying to avoid."""
    fake_response = MagicMock(status=200, headers={"content-type": "text/html"})
    fake_page = AsyncMock()
    fake_page.goto = AsyncMock(return_value=fake_response)
    fake_page.content = AsyncMock(return_value=_LONG_HTML)
    fake_page.close = AsyncMock()
    fake_page.url = "https://example.com/"
    fake_ctx = AsyncMock()
    fake_ctx.new_page = AsyncMock(return_value=fake_page)

    @asynccontextmanager
    async def fake_context(self, *, storage_state=None, **context_kwargs):
        yield fake_ctx

    async def fake_mobile_kwargs(self):
        return {"user_agent": "iPhone", "viewport": {"width": 390, "height": 844},
                "is_mobile": True, "has_touch": True, "device_scale_factor": 3}

    desktop_stealth_calls = []
    mobile_stealth_calls = []

    async def desktop_apply(ctx):
        desktop_stealth_calls.append(ctx)

    async def mobile_apply(ctx):
        mobile_stealth_calls.append(ctx)

    pool = fb_mod.BrowserPool()
    with patch.object(fb_mod.BrowserPool, "context", fake_context), \
         patch.object(fb_mod.BrowserPool, "mobile_context_kwargs", fake_mobile_kwargs), \
         patch.object(fb_mod._STEALTH, "apply_stealth_async", desktop_apply), \
         patch.object(fb_mod._STEALTH_MOBILE, "apply_stealth_async", mobile_apply):
        await fb_mod.fetch(pool, "https://example.com/", mobile=True)
        await fb_mod.fetch(pool, "https://example.com/", mobile=False)

    assert len(mobile_stealth_calls) == 1, "mobile=True should use _STEALTH_MOBILE"
    assert len(desktop_stealth_calls) == 1, "mobile=False should use _STEALTH"


def test_stealth_mobile_overrides_are_ios_consistent():
    """Lock the iOS-correctness of `_STEALTH_MOBILE` so a future stealth
    upgrade can't silently revert these to desktop defaults."""
    assert fb_mod._STEALTH_MOBILE.navigator_platform_override == "iPhone"
    assert fb_mod._STEALTH_MOBILE.navigator_vendor_override == "Apple Computer, Inc."
    # iOS Safari doesn't ship Client Hints
    assert fb_mod._STEALTH_MOBILE.sec_ch_ua is False


async def test_mobile_context_kwargs_returns_deep_copy():
    """Reviewer N1: the dict returned by `mobile_context_kwargs()` must
    not share mutable nested structures (e.g. `viewport`) with
    Playwright's `devices` table — otherwise a caller mutating
    `kwargs['viewport']['width']` poisons every subsequent fetch."""
    pool = fb_mod.BrowserPool()

    # Stub a minimal Playwright instance whose `devices` table mimics the
    # real one's shape (the iPhone descriptor has a nested viewport dict).
    fake_pw = MagicMock()
    fake_pw.devices = {
        "iPhone 13": {
            "user_agent": "iPhone",
            "viewport": {"width": 390, "height": 844},
            "is_mobile": True,
            "has_touch": True,
            "device_scale_factor": 3,
        }
    }
    pool._pw = fake_pw

    async def noop_ensure():
        return None

    with patch.object(pool, "_ensure", noop_ensure):
        result = await pool.mobile_context_kwargs()

    # Mutate the returned nested dict aggressively
    result["viewport"]["width"] = 9999
    result["is_mobile"] = "tampered"

    # Source table must be unaffected
    assert fake_pw.devices["iPhone 13"]["viewport"]["width"] == 390
    assert fake_pw.devices["iPhone 13"]["is_mobile"] is True


# ---- base64 image stripping ------------------------------------------------


_HTML_WITH_IMAGES = """
<html><head><title>T</title></head><body>
  <article>
    <h1>Pictures</h1>
    <p>Inline base64:
       <img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg==" alt="dot">
    </p>
    <p>External: <img src="https://example.com/logo.png" alt="logo"></p>
    <p>Body text long enough to clear the tiny-body escalation heuristic
       and produce a stable markdown extraction.</p>
  </article>
</body></html>
"""


def test_remove_base64_images_strips_data_uri_keeps_external():
    out = content_mod.html_to_markdown(_HTML_WITH_IMAGES, remove_base64_images=True)
    assert "iVBORw0K" not in out.markdown            # base64 payload gone
    assert "data:image" not in out.markdown          # no data: URI
    assert "example.com/logo.png" in out.markdown    # external image survived
    assert "Pictures" in out.markdown                # body intact


def test_default_v03_keeps_external_strips_only_base64():
    """v0.3 default `remove_base64_images=True`: data: URIs are stripped,
    external <img> tags survive into markdown. This replaces the v0.2
    `test_default_strips_all_images_v01_behavior` test — same input HTML,
    inverted expectations to match the new default."""
    out = content_mod.html_to_markdown(_HTML_WITH_IMAGES)
    assert "iVBORw0K" not in out.markdown            # base64 payload gone
    assert "data:image" not in out.markdown          # no data: URI
    assert "example.com/logo.png" in out.markdown    # external image survived
    assert "Pictures" in out.markdown                # body intact


def test_keep_images_preserves_picture_source_wrapper_so_img_survives():
    """Reviewer #2: when `remove_base64_images=True`, an `<img>` wrapped
    in `<picture><source>...</picture>` (the common responsive-image
    pattern) must survive into the markdown. lxml's HTML parser treats
    `<source>` as non-void and nests the next-sibling `<img>` *inside*
    it during parsing, so any code that removes `<source>` would
    cascade-kill the `<img>` next to it in source order. `<svg>` stays
    stripped either way."""
    html = (
        "<html><body>"
        "<picture>"
        '<source srcset="https://e.com/x.webp" type="image/webp">'
        '<img src="https://e.com/x.png" alt="x">'
        "</picture>"
        '<svg><path d="M0,0"/></svg>'
        "<p>body text long enough to bypass the tiny-body escalation "
        "heuristic and produce stable markdown.</p>"
        "</body></html>"
    )
    out = content_mod.html_to_markdown(html, remove_base64_images=True)

    # The <img> nested under <source> (lxml's parser quirk) survives
    assert "e.com/x.png" in out.markdown
    # <svg> always stripped (also in _REMOVE_TAGS, never useful as text)
    assert "M0,0" not in out.markdown
    # Body text intact
    assert "body text" in out.markdown


def test_default_v03_keeps_picture_source_img():
    """v0.3 default keeps the external <img> even when nested inside
    <picture><source>...</picture>. <svg> remains always stripped (it's
    in _REMOVE_TAGS regardless of the flag). Replaces v0.2's
    `test_default_behavior_strips_picture_source_img_together`."""
    html = (
        "<html><body>"
        "<picture>"
        '<source srcset="https://e.com/x.webp">'
        '<img src="https://e.com/x.png" alt="x">'
        "</picture>"
        '<svg><path d="M0,0"/></svg>'
        "<p>body text long enough to bypass the tiny-body escalation "
        "heuristic and produce stable markdown.</p>"
        "</body></html>"
    )
    out = content_mod.html_to_markdown(html)  # default: True in v0.3
    assert "e.com/x.png" in out.markdown      # external <img> survives
    assert "M0,0" not in out.markdown         # <svg> always stripped
    assert "body text" in out.markdown        # body intact


def test_drop_base64_images_helper_two_pass():
    """The helper itself: must remove every data: <img> from a doc with
    interleaved elements without leaving holes from mid-iteration mutation
    (the CLAUDE.md DOM-mutation rule). Three base64 images in one parent."""
    from lxml import html as lxml_html

    doc = lxml_html.fromstring(
        "<html><body>"
        '<p><img src="data:image/png;base64,AAA"></p>'
        '<p><img src="data:image/png;base64,BBB"></p>'
        '<p><img src="data:image/png;base64,CCC"></p>'
        '<p><img src="https://e.com/x.png"></p>'
        "</body></html>"
    )
    content_mod._drop_base64_images(doc)
    remaining = doc.xpath("//img/@src")
    assert remaining == ["https://e.com/x.png"]


# ---- backwards compat ------------------------------------------------------


async def test_default_call_response_unchanged_after_pr1b(router):
    """Default `fetch_url`/CLI call must produce a response with the
    v0.1/PR1a top-level key set even with PR 1b's new fields merged."""
    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"), \
         patch("lightcrawl.fetch_http.fetch", return_value=_ok()):
        out = await router.fetch(FetchRequest(url="https://example.com/"))

    expected_keys = {
        "ok", "url", "final_url", "strategy_used", "fetched_at", "title",
        "content", "content_truncated", "dump_path", "full_content_hash",
        "metadata", "attempts", "headings",
    }
    assert set(out.keys()) == expected_keys
    assert out["ok"] is True
    assert "Headline" in out["content"]
