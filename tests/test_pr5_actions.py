"""PR 5 — Declarative Actions (click/write/press/wait/scroll/screenshot).

Offline tests cover:
  - actions.from_dict / parse_actions validation
  - _l1_incapable returns True for non-empty actions
  - FetchRequest with actions forces L2
  - Intermediate screenshots land in screenshots[]
  - ACTION_FAILED error propagation
  - CLI --actions flag parsing
"""

from unittest.mock import AsyncMock, patch

import pytest

from lightcrawl.actions import (
    ClickAction,
    PressAction,
    ScrollAction,
    ScreenshotAction,
    WaitAction,
    WriteAction,
    from_dict,
    parse_actions,
)
from lightcrawl.errors import ErrorCode, FetchError
from lightcrawl.fetch_browser import BrowserResult
from lightcrawl.fetch_http import HttpResult
from lightcrawl.router import (
    FetchRequest,
    Router,
    _l1_incapable,
)

_STEALTH_ASYNC = AsyncMock()


@pytest.fixture
def router():
    r = Router()
    yield r


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


# ══ actions schema validation ══


def test_from_dict_click():
    a = from_dict({"type": "click", "selector": "#submit"})
    assert isinstance(a, ClickAction)
    assert a.selector == "#submit"
    assert a.timeout_ms == 5000  # default


def test_from_dict_click_with_timeout():
    a = from_dict({"type": "click", "selector": "#btn", "timeout_ms": 3000})
    assert a.timeout_ms == 3000


def test_from_dict_write():
    a = from_dict({"type": "write", "selector": "#email", "text": "hi@test.com"})
    assert isinstance(a, WriteAction)
    assert a.text == "hi@test.com"


def test_from_dict_press():
    a = from_dict({"type": "press", "key": "Enter"})
    assert isinstance(a, PressAction)
    assert a.key == "Enter"


def test_from_dict_wait():
    a = from_dict({"type": "wait", "milliseconds": 500})
    assert isinstance(a, WaitAction)
    assert a.milliseconds == 500


def test_from_dict_scroll():
    a = from_dict({"type": "scroll", "pixels": 600, "direction": "up"})
    assert isinstance(a, ScrollAction)
    assert a.pixels == 600
    assert a.direction == "up"


def test_from_dict_scroll_defaults():
    a = from_dict({"type": "scroll"})
    assert a.pixels == 800
    assert a.direction == "down"


def test_from_dict_screenshot():
    a = from_dict({"type": "screenshot", "label": "after-login"})
    assert isinstance(a, ScreenshotAction)
    assert a.label == "after-login"


def test_from_dict_unknown_type():
    with pytest.raises(ValueError, match="unknown action type"):
        from_dict({"type": "drag_and_drop"})


def test_from_dict_missing_type():
    with pytest.raises(ValueError, match="must have a string 'type'"):
        from_dict({"selector": "#btn"})


def test_from_dict_unknown_field():
    with pytest.raises(ValueError, match="unknown fields"):
        from_dict({"type": "click", "selector": "#b", "force": True})


def test_from_dict_type_case_insensitive():
    a = from_dict({"type": "CLICK", "selector": "#btn"})
    assert isinstance(a, ClickAction)


def test_parse_actions_valid_list():
    result = parse_actions([
        {"type": "click", "selector": "#a"},
        {"type": "press", "key": "Tab"},
    ])
    assert len(result) == 2
    assert isinstance(result[0], ClickAction)
    assert isinstance(result[1], PressAction)


def test_parse_actions_empty_list():
    assert parse_actions([]) == []
    assert parse_actions(None) == []


def test_parse_actions_non_list():
    with pytest.raises(ValueError, match="must be a list"):
        parse_actions("not a list")


def test_parse_actions_error_includes_index():
    with pytest.raises(ValueError, match=r"actions\[2\]"):
        parse_actions([
            {"type": "click", "selector": "#a"},
            {"type": "click", "selector": "#b"},
            {"type": "invalid_type"},
        ])


# ══ _l1_incapable integration ══


def test_l1_incapable_with_actions():
    req = FetchRequest(url="https://example.com/", actions=[
        ClickAction(selector="#login")
    ])
    assert _l1_incapable(req) is True


def test_l1_incapable_with_empty_actions():
    req = FetchRequest(url="https://example.com/", actions=[])
    assert _l1_incapable(req) is False


def test_l1_incapable_actions_and_screenshot():
    """Both actions and screenshot format → still True."""
    req = FetchRequest(
        url="https://example.com/",
        output_format="screenshot",
        actions=[WaitAction(milliseconds=100)],
    )
    assert _l1_incapable(req) is True


# ══ CLI _parse_actions ══


def test_parse_actions_cli_json():
    from lightcrawl.cli import _parse_actions

    result = _parse_actions(
        '[{"type":"click","selector":"#btn"},{"type":"scroll","pixels":100}]'
    )
    assert len(result) == 2
    assert isinstance(result[0], ClickAction)
    assert isinstance(result[1], ScrollAction)


def test_parse_actions_cli_file(tmp_path):
    """@file syntax: read actions from a JSON file."""
    from lightcrawl.cli import _parse_actions

    p = tmp_path / "actions.json"
    p.write_text('[{"type": "press", "key": "Escape"}]')
    result = _parse_actions(f"@{p}")
    assert len(result) == 1
    assert isinstance(result[0], PressAction)


def test_parse_actions_cli_empty():
    from lightcrawl.cli import _parse_actions

    assert _parse_actions(None) == []
    assert _parse_actions("") == []


def test_parse_actions_cli_invalid_json():
    from lightcrawl.cli import _parse_actions

    with pytest.raises(ValueError, match="invalid"):
        _parse_actions("{not json}")


# ══ fetch_browser action execution ══


async def test_actions_passed_to_fetch_browser():
    """Verify the router passes actions to fetch_browser.fetch()."""
    actions = [ClickAction(selector="#btn")]

    async def fake_browser_fetch(pool, url, *, wait_for=None, timeout=None,
                                storage_state=None, headers=None, mobile=None,
                                screenshot=None, actions=None, block_ads=False):
        # Capture the actions kwarg
        fake_browser_fetch.captured_actions = actions
        return BrowserResult(
            final_url=url, status_code=200, text="<html>ok</html>",
            content_type="text/html", elapsed_ms=10,
        )
    fake_browser_fetch.captured_actions = None

    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"), \
         patch("lightcrawl.fetch_browser.fetch", side_effect=fake_browser_fetch), \
         patch("lightcrawl.fetch_browser._STEALTH.apply_stealth_async", AsyncMock()):
        router = Router()
        try:
            out = await router.fetch(FetchRequest(
                url="https://example.com/", actions=actions,
            ))
        finally:
            await router.close()

    assert out["ok"] is True
    assert out["strategy_used"] == "browser"
    captured = fake_browser_fetch.captured_actions
    assert captured is not None
    assert len(captured) == 1
    assert isinstance(captured[0], ClickAction)
    assert captured[0].selector == "#btn"


async def test_action_failure_propagates(router):
    """ACTION_FAILED errors from fetch_browser → failure response."""
    def fake_browser_err(*args, **kwargs):
        raise FetchError(ErrorCode.ACTION_FAILED,
                         "action[0] type=ClickAction: selector '#missing' not found")

    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"), \
         patch("lightcrawl.fetch_browser.fetch", side_effect=fake_browser_err):
        out = await router.fetch(FetchRequest(
            url="https://example.com/",
            actions=[ClickAction(selector="#missing")],
        ))

    assert out["ok"] is False
    assert out["error_code"] == ErrorCode.ACTION_FAILED.value
    assert "action[0]" in out["error_detail"]
    assert "missing" in out["error_detail"]


async def test_actions_force_l2_even_with_l1_success(router):
    """Non-empty actions must skip L1 and go to L2."""
    l1_called = {"n": 0}

    def fake_l1(*a, **kw):
        l1_called["n"] += 1
        return HttpResult(
            final_url="https://example.com/", status_code=200,
            text="<html>ok</html>", content_type="text/html", elapsed_ms=5,
        )

    async def fake_l2(pool, url, *, actions=None, **kw):
        return BrowserResult(
            final_url=url, status_code=200, text="<html>ok</html>",
            content_type="text/html", elapsed_ms=10,
        )

    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"), \
         patch("lightcrawl.fetch_http.fetch", side_effect=fake_l1), \
         patch("lightcrawl.fetch_browser.fetch", side_effect=fake_l2):
        out = await router.fetch(FetchRequest(
            url="https://example.com/",
            actions=[ClickAction(selector="#btn")],
        ))

    assert out["ok"] is True
    assert out["strategy_used"] == "browser"
    assert l1_called["n"] == 0, "L1 must be skipped when actions are present"


async def test_intermediate_screenshots_appear_in_response(router, tmp_path, monkeypatch):
    """ScreenshotAction entries must produce {stage:action} entries in
    screenshots[] and be saved to disk."""
    monkeypatch.setattr("lightcrawl.paths.SCREENSHOTS", tmp_path / "screenshots")

    action_shots = [
        {"index": 1, "label": "after-click", "png_bytes": b"\x89PNG\xa1"},
        {"index": 3, "label": None, "png_bytes": b"\x89PNG\xb2"},
    ]

    async def fake_l2(pool, url, *, actions=None, screenshot=False, **kw):
        return BrowserResult(
            final_url=url, status_code=200, text="<html>ok</html>",
            content_type="text/html", elapsed_ms=10,
            action_screenshots=action_shots,
            screenshot_png=b"\x89PNG_final" if screenshot else None,
        )

    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"), \
         patch("lightcrawl.fetch_browser.fetch", side_effect=fake_l2):
        out = await router.fetch(FetchRequest(
            url="https://example.com/",
            output_format="markdown+screenshot",
            actions=[
                ClickAction(selector="#btn"),
                ScreenshotAction(label="after-click"),
                WriteAction(selector="#f", text="hi"),
                ScreenshotAction(),  # label=None
            ],
        ))

    assert out["ok"] is True
    assert "screenshots" in out
    shots = out["screenshots"]
    assert len(shots) == 3  # 2 action + 1 final

    # Action screenshots come first, in order
    assert shots[0]["stage"] == "action"
    assert shots[0]["index"] == 1
    assert shots[0]["label"] == "after-click"
    assert shots[0]["path"].endswith("_act1.png")

    assert shots[1]["stage"] == "action"
    assert shots[1]["index"] == 3
    assert shots[1]["label"] is None
    assert shots[1]["path"].endswith("_act3.png")

    # Final screenshot
    assert shots[2]["stage"] == "final"
    assert shots[2]["path"].endswith(".png")

    # Verify files on disk
    from pathlib import Path
    assert Path(shots[0]["path"]).read_bytes() == b"\x89PNG\xa1"
    assert Path(shots[1]["path"]).read_bytes() == b"\x89PNG\xb2"


async def test_no_screenshots_when_no_screenshot_actions_or_final(router):
    """Without screenshot output format or ScreenshotAction, screenshots
    key must not appear."""
    async def fake_l2(pool, url, *, actions=None, **kw):
        return BrowserResult(
            final_url=url, status_code=200, text="<html>ok</html>",
            content_type="text/html", elapsed_ms=10,
        )

    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"), \
         patch("lightcrawl.fetch_browser.fetch", side_effect=fake_l2):
        out = await router.fetch(FetchRequest(
            url="https://example.com/",
            actions=[ClickAction(selector="#btn")],
        ))

    assert out["ok"] is True
    assert "screenshots" not in out


# ══ scroll direction / wait validation in from_dict / parse_actions ══


def test_from_dict_scroll_rejects_invalid_direction():
    with pytest.raises(ValueError, match="scroll direction"):
        from_dict({"type": "scroll", "direction": "left"})


def test_from_dict_scroll_rejects_negative_pixels():
    with pytest.raises(ValueError, match="scroll pixels"):
        from_dict({"type": "scroll", "pixels": -1})


def test_from_dict_wait_rejects_negative():
    with pytest.raises(ValueError, match="wait milliseconds must be >= 0"):
        from_dict({"type": "wait", "milliseconds": -100})


def test_from_dict_wait_rejects_over_limit():
    with pytest.raises(ValueError, match="wait milliseconds must be <="):
        from_dict({"type": "wait", "milliseconds": 999999})


def test_parse_actions_enforces_screenshot_cap():
    """>20 ScreenshotAction entries → ValueError at parse time, not runtime."""
    actions = [{"type": "screenshot"}] * 21
    with pytest.raises(ValueError, match="max 20 ScreenshotAction"):
        parse_actions(actions)


def test_parse_actions_allows_exactly_20_screenshots():
    """20 ScreenshotAction entries is at the limit — should pass."""
    actions = [{"type": "screenshot"}] * 20
    result = parse_actions(actions)
    assert len(result) == 20


# ══ action exception handling ══


async def test_non_timeout_playwright_error_maps_to_action_failed(router):
    """page.click() raising a non-PWTimeout Playwright error (e.g.
    'element not visible') must become ACTION_FAILED, not UNKNOWN."""
    async def fake_l2(pool, url, *, actions=None, **kw):
        # Simulate what the real fetch_browser.fetch does — re-raise as
        # FetchError(ACTION_FAILED) regardless of the inner exception type.
        raise FetchError(
            ErrorCode.ACTION_FAILED,
            "action[0] type=ClickAction: Error: element is not visible",
        )

    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"), \
         patch("lightcrawl.fetch_browser.fetch", side_effect=fake_l2):
        out = await router.fetch(FetchRequest(
            url="https://example.com/",
            actions=[ClickAction(selector="#hidden-btn")],
        ))

    assert out["ok"] is False
    assert out["error_code"] == ErrorCode.ACTION_FAILED.value
    assert "action[0]" in out["error_detail"]


# ══ CLI @file edge cases ══


def test_parse_actions_cli_missing_file():
    """@missing_file.json → clear ValueError, not a traceback."""
    from lightcrawl.cli import _parse_actions

    with pytest.raises(ValueError, match="cannot read actions file"):
        _parse_actions("@/nonexistent/path/actions.json")


# ══ strategy=http + actions must fail fast ══


async def test_strategy_http_with_actions_fails_fast(router):
    """--strategy http + non-empty actions → clear error, not silent no-op."""
    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"):
        out = await router.fetch(FetchRequest(
            url="https://example.com/",
            strategy="http",
            actions=[ClickAction(selector="#btn")],
        ))

    assert out["ok"] is False
    assert "actions" in out["error_detail"].lower()
    assert "strategy" in out["error_detail"].lower()


# ══ WriteAction.timeout_ms ══


def test_from_dict_write_with_timeout():
    a = from_dict({"type": "write", "selector": "#f", "text": "hi",
                   "timeout_ms": 10000})
    assert isinstance(a, WriteAction)
    assert a.timeout_ms == 10000


def test_from_dict_write_default_timeout():
    a = from_dict({"type": "write", "selector": "#f", "text": "hi"})
    assert a.timeout_ms == 5000


# ══ timeout_ms validation ══


def test_from_dict_click_rejects_negative_timeout():
    with pytest.raises(ValueError, match="timeout_ms must be between"):
        from_dict({"type": "click", "selector": "#b", "timeout_ms": -5000})


def test_from_dict_click_rejects_zero_timeout():
    with pytest.raises(ValueError, match="timeout_ms must be between"):
        from_dict({"type": "click", "selector": "#b", "timeout_ms": 0})


def test_from_dict_write_rejects_overlimit_timeout():
    with pytest.raises(ValueError, match="timeout_ms must be between"):
        from_dict({"type": "write", "selector": "#f", "text": "hi",
                   "timeout_ms": 999999})


def test_from_dict_click_accepts_valid_timeout():
    a = from_dict({"type": "click", "selector": "#b", "timeout_ms": 30000})
    assert a.timeout_ms == 30000


# ══ type validation in _validate_value ══


def test_from_dict_scroll_pixels_string_returns_value_error():
    """pixels='100' → ValueError (not TypeError leaking). Used to raise
    TypeError before the isinstance check was added."""
    with pytest.raises(ValueError, match=r"actions\[0\]"):
        parse_actions([{"type": "scroll", "pixels": "100"}])


def test_from_dict_wait_milliseconds_bool_returns_value_error():
    """milliseconds=True → ValueError (bool is int subclass, must be rejected)."""
    with pytest.raises(ValueError, match=r"actions\[0\]"):
        parse_actions([{"type": "wait", "milliseconds": True}])


# ══ PressAction key validation ══


def test_from_dict_press_accepts_valid_keys():
    for key in ["Enter", "Tab", "Escape", "ArrowDown", "ArrowUp", "Backspace",
                "Delete", "Space"]:
        a = from_dict({"type": "press", "key": key})
        assert a.key == key


def test_from_dict_press_rejects_lowercase_key():
    """Playwright keys are case-sensitive; 'enter' ≠ 'Enter'."""
    with pytest.raises(ValueError, match="unknown press key"):
        from_dict({"type": "press", "key": "enter"})


def test_from_dict_press_rejects_unknown_key():
    with pytest.raises(ValueError, match="unknown press key"):
        from_dict({"type": "press", "key": "Ctrl"})


def test_from_dict_press_rejects_non_string_key():
    with pytest.raises(ValueError, match=r"actions\[0\]"):
        parse_actions([{"type": "press", "key": 13}])
