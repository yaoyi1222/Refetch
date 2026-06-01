from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from curl_cffi import requests as ccr

from .errors import ErrorCode, FetchError

DEFAULT_TIMEOUT = 5.0
MAX_BYTES = 10 * 1024 * 1024  # 10MB

# Desktop default — chrome120 is well-tested in curl_cffi. Newer profiles
# (chrome142/chrome146) exist but moving the default belongs in its own
# upgrade PR with bench-level validation.
DEFAULT_IMPERSONATE = "chrome120"

# Mobile profile used when FetchRequest.mobile=True. Picked from the iOS
# Safari family curl_cffi ships (`safari260_ios`, `safari184_ios`, …) —
# `safari260_ios` is the most recent at time of writing. Switching the
# whole impersonate (UA + TLS + JA3 + HTTP/2 settings) avoids the bot
# signal of a UA-only flip. Validate in the experiment before merging.
MOBILE_IMPERSONATE = "safari260_ios"


@dataclass
class HttpResult:
    final_url: str
    status_code: int
    text: str
    content_type: str
    elapsed_ms: int
    # v0.3 PR 3 — response validators for conditional requests. ``None``
    # when the origin sends no ETag / Last-Modified. A 304 response is a
    # normal, returnable status (empty body); only transport errors raise.
    etag: str | None = None
    last_modified: str | None = None


def fetch(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    impersonate: str = DEFAULT_IMPERSONATE,
    headers: dict[str, str] | None = None,
) -> HttpResult:
    """L1 fetch using curl_cffi with browser TLS fingerprint impersonation.

    `headers` are merged into the request after the impersonate profile sets
    its defaults — caller-supplied values win on key collision. Pass at your
    own risk: overriding `User-Agent` here desyncs UA from the TLS fingerprint
    (a known bot signal). For `mobile`-style switches, prefer changing the
    impersonate profile instead of the UA header (see 02.md PR 1b).
    """
    try:
        r = ccr.get(
            url,
            timeout=timeout,
            impersonate=impersonate,
            allow_redirects=True,
            max_recv_speed=0,
            headers=headers or None,
        )
    except ccr.errors.RequestsError as e:
        msg = str(e)
        if "timeout" in msg.lower() or "timed out" in msg.lower():
            raise FetchError(ErrorCode.TIMEOUT, msg) from e
        raise FetchError(ErrorCode.HTTP_ERROR, msg) from e

    raw = r.content or b""
    if len(raw) > MAX_BYTES:
        raise FetchError(
            ErrorCode.CONTENT_TOO_LARGE,
            f"response is {len(raw)} bytes; max is {MAX_BYTES}",
        )

    ctype = r.headers.get("content-type", "")
    elapsed = getattr(r, "elapsed", None)
    if isinstance(elapsed, timedelta):
        elapsed_ms = int(elapsed.total_seconds() * 1000)
    elif isinstance(elapsed, (int, float)):
        elapsed_ms = int(elapsed * 1000)
    else:
        elapsed_ms = 0

    return HttpResult(
        final_url=str(r.url),
        status_code=r.status_code,
        text=r.text,
        content_type=ctype,
        elapsed_ms=elapsed_ms,
        etag=r.headers.get("etag"),
        last_modified=r.headers.get("last-modified"),
    )
