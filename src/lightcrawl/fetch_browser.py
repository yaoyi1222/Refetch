from __future__ import annotations

import asyncio
import copy
import platform
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

from playwright.async_api import (
    Browser,
    TimeoutError as PWTimeout,
    async_playwright,
)
from playwright_stealth import Stealth

from .actions import (
    ClickAction as _ClickAction,
    PressAction as _PressAction,
    ScreenshotAction as _ScreenshotAction,
    ScrollAction as _ScrollAction,
    WaitAction as _WaitAction,
    WriteAction as _WriteAction,
)
from .errors import ErrorCode, FetchError

_STEALTH = Stealth()

# Mobile-aware stealth. playwright-stealth's defaults inject DESKTOP values
# (navigator.platform → "Win32", navigator.vendor → "Google Inc.", a
# Chromium-shaped sec-ch-ua header). Layered on top of an iPhone UA those
# create internal inconsistency — exactly the bot signal we're trying to
# avoid by switching the impersonate / device. So for mobile=True we use a
# separate Stealth instance pinning the JS-visible values to real iOS.
_STEALTH_MOBILE = Stealth(
    navigator_platform_override="iPhone",
    navigator_vendor_override="Apple Computer, Inc.",
    # iOS Safari doesn't ship Client Hints; rewriting them to look mobile is
    # less defensible than just not sending them.
    sec_ch_ua=False,
)

DEFAULT_TIMEOUT = 15.0


def _default_user_agent() -> str:
    """Build a Chrome 120 UA whose platform token matches the host OS, so the
    UA doesn't contradict the JS-visible navigator.platform that stealth
    exposes — UA/platform mismatch is itself a bot-detection signal."""
    sysname = platform.system()
    if sysname == "Linux":
        os_token = "X11; Linux x86_64"
    elif sysname == "Windows":
        os_token = "Windows NT 10.0; Win64; x64"
    else:
        # Darwin + unknown -> macOS UA (the project's prior default)
        os_token = "Macintosh; Intel Mac OS X 10_15_7"
    return (
        f"Mozilla/5.0 ({os_token}) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )


_DEFAULT_UA = _default_user_agent()


@dataclass
class WaitFor:
    selector: str | None = None
    network_idle: bool = False
    timeout_ms: int = 10_000


@dataclass
class BrowserResult:
    final_url: str
    status_code: int
    text: str
    content_type: str
    elapsed_ms: int
    # PR 2: PNG bytes when fetch() is invoked with screenshot=True. None
    # otherwise. Bytes (not base64 / not a path) so the router can decide
    # where to write — keeps fetch_browser.fetch agnostic to filesystem
    # layout / paths.SCREENSHOTS and the {sha1(url)}.png naming convention.
    screenshot_png: bytes | None = None
    # PR 5: intermediate screenshots from ScreenshotAction entries. Each
    # dict: {index, label, png_bytes}. The router writes them to disk and
    # appends to screenshots[].
    action_screenshots: list[dict] | None = None


class BrowserPool:
    """Single Chromium process, multiple short-lived contexts.

    Browser launch is ~1-2s, so we keep one browser alive and create a new
    context per fetch. Contexts are cheap and isolate cookies/state.
    """

    def __init__(self, max_concurrent_contexts: int = 4) -> None:
        self._pw = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(max_concurrent_contexts)

    async def _ensure(self) -> Browser:
        async with self._lock:
            if self._browser is None or not self._browser.is_connected():
                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(headless=True)
            return self._browser

    @asynccontextmanager
    async def context(
        self,
        *,
        storage_state: str | dict | None = None,
        **context_kwargs,
    ):
        """Create a fresh `BrowserContext`. Extra kwargs are forwarded to
        `browser.new_context()` so callers can unpack a Playwright device
        descriptor (`pool._pw.devices['iPhone 13']`) for mobile emulation
        without this method needing to know about every device knob (UA,
        viewport, device_scale_factor, is_mobile, has_touch …)."""
        browser = await self._ensure()
        async with self._sem:
            kwargs = {
                "user_agent": _DEFAULT_UA,
                "viewport": {"width": 1280, "height": 800},
            }
            kwargs.update(context_kwargs)  # caller wins on collision
            ctx = await browser.new_context(storage_state=storage_state, **kwargs)
            try:
                yield ctx
            finally:
                await ctx.close()

    async def mobile_context_kwargs(self) -> dict:
        """Return the iPhone 13 descriptor for unpacking into `context(...)`.

        Implies `_ensure()` because Playwright's `devices` table lives on the
        started Playwright instance, not on the import.

        Returns a `deepcopy` — the descriptor has nested dicts (`viewport`),
        so a shallow `dict(...)` would still share those nested references
        and a caller mutating `result['viewport']['width']` would poison
        Playwright's device table for every subsequent fetch."""
        await self._ensure()
        return copy.deepcopy(self._pw.devices["iPhone 13"])

    async def close(self) -> None:
        async with self._lock:
            if self._browser is not None:
                await self._browser.close()
                self._browser = None
            if self._pw is not None:
                await self._pw.stop()
                self._pw = None


async def fetch(
    pool: BrowserPool,
    url: str,
    *,
    wait_for: WaitFor | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    storage_state: str | dict | None = None,
    headers: dict[str, str] | None = None,
    mobile: bool = False,
    screenshot: bool = False,
    actions: list | None = None,
) -> BrowserResult:
    """L2 fetch via Playwright with stealth always enabled.

    `actions` (PR 5) is an optional list of action dataclass instances from
    `lightcrawl.actions`. They execute after `wait_for` and before
    `page.content()`. ScreenshotAction entries produce intermediate PNGs
    returned via `BrowserResult.action_screenshots`."""
    wait_for = wait_for or WaitFor()
    started = time.monotonic()
    if actions is None:
        actions = []

    extra_ctx_kwargs: dict = {}
    if mobile:
        extra_ctx_kwargs = await pool.mobile_context_kwargs()

    png_bytes: bytes | None = None  # populated below if screenshot=True
    action_shots: list[dict] = []   # intermediate screenshots (PR 5)

    async with pool.context(storage_state=storage_state, **extra_ctx_kwargs) as ctx:
        stealth = _STEALTH_MOBILE if mobile else _STEALTH
        await stealth.apply_stealth_async(ctx)
        if headers:
            # NOTE: `set_extra_http_headers` *replaces* the context's extra
            # headers, it does not merge. Today stealth doesn't set extras
            # via this API (it injects via JS + UA-via-context-options), so
            # the caller's dict is safe. If stealth ever starts setting
            # default Referer/Accept-Language here, swap to a manual merge.
            await ctx.set_extra_http_headers(headers)
        page = await ctx.new_page()
        try:
            response = await page.goto(
                url,
                timeout=int(timeout * 1000),
                wait_until="domcontentloaded",
            )
            if wait_for.selector:
                try:
                    await page.wait_for_selector(
                        wait_for.selector, timeout=wait_for.timeout_ms
                    )
                except PWTimeout as e:
                    raise FetchError(
                        ErrorCode.JS_TIMEOUT,
                        f"selector {wait_for.selector!r} not found within {wait_for.timeout_ms}ms",
                    ) from e
            elif wait_for.network_idle:
                try:
                    await page.wait_for_load_state(
                        "networkidle", timeout=wait_for.timeout_ms
                    )
                except PWTimeout:
                    pass  # best-effort; SPAs often never go idle

            # PR 5: execute declarative actions in order. Each ScreenshotAction
            # appends to `action_shots`; the router saves them to disk later.
            # Screenshot cap is enforced in actions.parse_actions(), not here.
            for i, action in enumerate(actions):
                try:
                    if isinstance(action, _ClickAction):
                        await page.click(action.selector, timeout=action.timeout_ms)
                    elif isinstance(action, _WriteAction):
                        await page.fill(action.selector, action.text,
                                        timeout=action.timeout_ms)
                    elif isinstance(action, _PressAction):
                        await page.keyboard.press(action.key)
                    elif isinstance(action, _WaitAction):
                        await asyncio.sleep(action.milliseconds / 1000.0)
                    elif isinstance(action, _ScrollAction):
                        sign = 1 if action.direction == "down" else -1
                        await page.evaluate(
                            "([x, y]) => window.scrollBy(x, y)",
                            [0, sign * action.pixels],
                        )
                    elif isinstance(action, _ScreenshotAction):
                        png = await page.screenshot(full_page=True, type="png")
                        action_shots.append({
                            "index": i,
                            "label": action.label,
                            "png_bytes": png,
                        })
                except FetchError:
                    # Re-raise without wrapping — preserves the inner error code
                    # (e.g. a network error during page.screenshot()).
                    raise
                except Exception as e:
                    # Catch Playwright errors (PWTimeout + non-timeout like
                    # "element not visible", "intercepts pointer events",
                    # "frame detached") AND other failures (negative-wait
                    # ValueError on non-CPython runtimes). Re-wrap as a clear
                    # ACTION_FAILED with the action index.
                    raise FetchError(
                        ErrorCode.ACTION_FAILED,
                        f"action[{i}] type={type(action).__name__}: {e}",
                    ) from e

            # Bug 8: page.content() raises if the SPA is mid-navigation.
            # One short re-settle attempt rescues most cases; if it still
            # fails the outer except maps it to SPA_NAVIGATION_LOOP.
            try:
                html = await page.content()
            except Exception as inner:
                msg = str(inner).lower()
                if "navigating" in msg and "changing the content" in msg:
                    try:
                        await page.wait_for_load_state(
                            "domcontentloaded", timeout=3000
                        )
                    except PWTimeout:
                        pass
                    html = await page.content()
                else:
                    raise
            final_url = page.url
            status = response.status if response is not None else 0
            ctype = ""
            if response is not None:
                try:
                    ctype = response.headers.get("content-type", "")
                except Exception:
                    ctype = ""

            # PR 2: capture the PNG immediately after extracting HTML so the
            # screenshot corresponds to the DOM state as closely as Playwright
            # allows. A small temporal gap remains — `page.content()` and
            # `page.screenshot()` are distinct round-trips, so on animated /
            # lazy-load pages they may differ by a few frames. Playwright has
            # no atomic DOM-plus-pixels snapshot API.
            # full_page=True so the whole scrollable area shows up; PNG over
            # JPEG for lossless text rendering. The bytes go on the
            # BrowserResult — the router decides where to write them.
            if screenshot:
                try:
                    png_bytes = await page.screenshot(full_page=True, type="png")
                except PWTimeout as e:
                    raise FetchError(ErrorCode.TIMEOUT, f"screenshot: {e}") from e
        except PWTimeout as e:
            raise FetchError(ErrorCode.TIMEOUT, str(e)) from e
        except Exception as e:
            if isinstance(e, FetchError):
                raise
            msg = str(e)
            low = msg.lower()
            # Bug 9: Playwright surfaces "Download is starting" when a URL
            # responds with Content-Disposition attachment. Map to a clear
            # error code instead of the generic HTTP_ERROR bucket.
            if "download is starting" in low:
                raise FetchError(
                    ErrorCode.UNSUPPORTED_CONTENT_TYPE,
                    "the URL triggered a file download; not an HTML page",
                ) from e
            # Bug 8: SPA mid-navigation — page.content() raises before the
            # DOM settles. The retry inside this function above usually
            # recovers; if it doesn't, surface a specific code.
            if "navigating" in low and "changing the content" in low:
                raise FetchError(
                    ErrorCode.SPA_NAVIGATION_LOOP,
                    "the page kept navigating; the SPA never settled",
                ) from e
            raise FetchError(ErrorCode.HTTP_ERROR, msg) from e
        finally:
            await page.close()

    return BrowserResult(
        final_url=final_url,
        status_code=status,
        text=html,
        content_type=ctype,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        screenshot_png=png_bytes,
        action_screenshots=action_shots or None,
    )
