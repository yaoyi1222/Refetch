from __future__ import annotations

import asyncio
import time
from collections import defaultdict


class DomainThrottle:
    """Per-host asyncio rate limiter.

    acquire(host) must be awaited before each fetch. It serializes concurrent
    requests to the same host and ensures at least delay_ms between successive
    acquires for the same host. delay_ms=0 disables throttling (no-op).
    """

    def __init__(self, delay_ms: int) -> None:
        self.delay_ms = delay_ms
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_release: dict[str, float] = {}

    async def acquire(self, host: str) -> None:
        if self.delay_ms <= 0:
            return
        async with self._locks[host]:
            last = self._last_release.get(host)
            if last is not None:
                to_sleep = (self.delay_ms / 1000) - (time.monotonic() - last)
                if to_sleep > 0:
                    await asyncio.sleep(to_sleep)
            # Stamp the release time (after the sleep), so the next queued
            # caller measures its gap from when we actually returned — not
            # from when we entered the lock. Entry-time stamping collapses the
            # delay under contention (queued callers inherit the prior waiter's
            # already-elapsed sleep).
            self._last_release[host] = time.monotonic()
