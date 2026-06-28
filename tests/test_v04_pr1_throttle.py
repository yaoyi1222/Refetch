"""v0.4 PR-1 (#71): DomainThrottle — per-host rate limiting. Fully offline."""
from __future__ import annotations
import asyncio
import pytest
from lightcrawl.throttle import DomainThrottle
from lightcrawl.crawl import CrawlParams


async def test_first_acquire_does_not_sleep(monkeypatch):
    """First acquire for a host must not trigger asyncio.sleep."""
    recorded = []
    async def fake_sleep(d): recorded.append(d)
    monkeypatch.setattr("lightcrawl.throttle.asyncio.sleep", fake_sleep)

    throttle = DomainThrottle(delay_ms=200)
    await throttle.acquire("example.com")
    assert recorded == []


async def test_second_acquire_same_host_sleeps_delay(monkeypatch):
    """Second acquire immediately after first must sleep approximately delay_ms."""
    recorded = []
    async def fake_sleep(d): recorded.append(d)
    monkeypatch.setattr("lightcrawl.throttle.asyncio.sleep", fake_sleep)

    throttle = DomainThrottle(delay_ms=200)
    await throttle.acquire("example.com")  # first — no sleep
    await throttle.acquire("example.com")  # second — should sleep ~200ms
    assert len(recorded) == 1
    assert recorded[0] >= 0.19  # ~200ms (tolerance for monotonic clock precision)


async def test_different_hosts_independent(monkeypatch):
    """Concurrent acquires on different hosts must proceed without sleeping."""
    recorded = []
    async def fake_sleep(d): recorded.append(d)
    monkeypatch.setattr("lightcrawl.throttle.asyncio.sleep", fake_sleep)

    throttle = DomainThrottle(delay_ms=200)
    await asyncio.gather(
        throttle.acquire("host-a.com"),
        throttle.acquire("host-b.com"),
    )
    assert recorded == []


async def test_delay_ms_zero_is_noop(monkeypatch):
    """delay_ms=0 must never sleep, even on repeated same-host acquires."""
    recorded = []
    async def fake_sleep(d): recorded.append(d)
    monkeypatch.setattr("lightcrawl.throttle.asyncio.sleep", fake_sleep)

    throttle = DomainThrottle(delay_ms=0)
    await throttle.acquire("example.com")
    await throttle.acquire("example.com")
    assert recorded == []


def test_crawl_params_throttle_delay_defaults_to_zero():
    """CrawlParams.throttle_delay_ms must default to 0 (throttling off)."""
    p = CrawlParams(seed="https://ex.com/")
    assert p.throttle_delay_ms == 0
