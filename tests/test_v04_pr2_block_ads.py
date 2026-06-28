"""v0.4 PR-2 (#72): block_ads flag — drop requests to ad/tracker domains. Offline."""
from __future__ import annotations

import pytest
from unittest.mock import patch

from lightcrawl.errors import ErrorCode, FetchError
from lightcrawl.router import FetchRequest, Router, _AD_DOMAINS
from lightcrawl.crawl import CrawlParams
from lightcrawl.batch import BatchParams


@pytest.fixture
def router():
    return Router()


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr("lightcrawl.paths.ROOT", tmp_path)
    monkeypatch.setattr("lightcrawl.paths.DUMPS", tmp_path / "dumps")
    monkeypatch.setattr("lightcrawl.paths.PROFILES", tmp_path / "profiles")
    monkeypatch.setattr("lightcrawl.paths.LOGS", tmp_path / "logs")
    monkeypatch.setattr("lightcrawl.content.DUMPS", tmp_path / "dumps")
    monkeypatch.setattr("lightcrawl.auth.PROFILES", tmp_path / "profiles")
    (tmp_path / "dumps").mkdir(parents=True)
    (tmp_path / "profiles").mkdir(parents=True)


async def test_block_ads_blocks_ad_domain(router):
    """With block_ads=True, a URL on an ad domain must fail with URL_BLOCKED."""
    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="8.8.8.8"):
        result = await router.fetch(FetchRequest(
            url="https://www.google-analytics.com/ga.js",
            block_ads=True,
        ))
    assert result["ok"] is False
    assert result["error_code"] == ErrorCode.URL_BLOCKED.value


async def test_block_ads_off_does_not_block(router):
    """With block_ads=False (default), an ad-domain URL is not blocked here (router proceeds normally)."""
    # We only test that URL_BLOCKED is NOT the error — the fetch may fail for
    # other reasons (no real network), but it must not be URL_BLOCKED.
    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="8.8.8.8"), \
         patch("lightcrawl.fetch_http.fetch",
               side_effect=FetchError(ErrorCode.DNS_FAILED, "no network")):
        result = await router.fetch(FetchRequest(
            url="https://www.google-analytics.com/ga.js",
            block_ads=False,
        ))
    assert result.get("error_code") != ErrorCode.URL_BLOCKED.value


async def test_non_ad_domain_not_blocked(router):
    """With block_ads=True, a normal URL must not be blocked by the ad filter."""
    from lightcrawl.fetch_http import HttpResult
    fake = HttpResult(
        final_url="https://example.com/",
        status_code=200,
        text=(
            "<html><head><title>T</title></head><body>"
            "<article><h1>Hi</h1><p>Normal page content that is long enough "
            "to avoid the tiny-body browser escalation heuristic (200+ chars).</p>"
            "</article></body></html>"
        ),
        content_type="text/html",
        elapsed_ms=5,
    )
    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"), \
         patch("lightcrawl.fetch_http.fetch", return_value=fake):
        result = await router.fetch(FetchRequest(
            url="https://example.com/",
            block_ads=True,
        ))
    assert result["ok"] is True


def test_ad_domains_set_is_nonempty():
    """_AD_DOMAINS must exist and contain common ad/tracker domains."""
    assert "google-analytics.com" in _AD_DOMAINS
    assert "doubleclick.net" in _AD_DOMAINS
    assert len(_AD_DOMAINS) >= 5


def test_crawl_params_block_ads_defaults_false():
    """CrawlParams.block_ads must default to False."""
    assert CrawlParams(seed="https://ex.com/").block_ads is False


def test_batch_params_block_ads_defaults_false():
    """BatchParams.block_ads must default to False."""
    assert BatchParams().block_ads is False


def test_url_blocked_error_code_exists():
    """ErrorCode.URL_BLOCKED must exist with the string value 'URL_BLOCKED'."""
    assert ErrorCode.URL_BLOCKED.value == "URL_BLOCKED"
