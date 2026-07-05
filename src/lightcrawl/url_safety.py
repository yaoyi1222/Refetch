from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import tldextract

from .errors import ErrorCode, FetchError

_PRIVATE_NETS = [
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
]


@dataclass(frozen=True)
class ResolvedURL:
    url: str
    hostname: str
    etld1: str
    ip: str


def etld1(url_or_host: str) -> str:
    """Return registered domain (eTLD+1), e.g. x.com from foo.x.com or https://x.com/login."""
    host = urlparse(url_or_host).hostname or url_or_host
    ext = tldextract.extract(host)
    if not ext.domain or not ext.suffix:
        return host
    return f"{ext.domain}.{ext.suffix}"


def domain_matches(target_url: str, bound_etld1: str) -> bool:
    """True if target URL's eTLD+1 equals bound_etld1."""
    return etld1(target_url) == bound_etld1


# v0.4 PR-2 — ad/tracker domain blocklist. Lives here (not router) so both the
# router's top-level URL check and fetch_browser's page.route sub-resource
# handler can share one list without a circular import. Matched against eTLD+1.
_AD_DOMAINS: frozenset[str] = frozenset({
    "googletagmanager.com",   # NOTE: can break SPAs that lazy-load via GTM
    "google-analytics.com",
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "facebook.net",
    "fbcdn.net",
    "hotjar.com",
    "optimizely.com",
})


def is_ad_domain(url: str) -> bool:
    """True if the URL's eTLD+1 is a known ad/tracker domain."""
    return etld1(url) in _AD_DOMAINS


def _is_private_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return any(addr in net for net in _PRIVATE_NETS)


def validate_url(
    url: str,
    *,
    allow_private: bool = False,
    extra_allowlist: list[str] | None = None,
) -> ResolvedURL:
    """Validate URL scheme + resolve host, blocking SSRF targets.

    Raises FetchError with URL_NOT_ALLOWED or DNS_FAILED.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise FetchError(ErrorCode.URL_NOT_ALLOWED, f"unsupported scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise FetchError(ErrorCode.URL_NOT_ALLOWED, "missing hostname")

    # Resolve host → IP. Handle IP literals (IPv4 / IPv6) directly —
    # socket.gethostbyname() is IPv4-only and raises gaierror on IPv6
    # literals, which the caller would misinterpret as DNS_FAILED.
    try:
        ip_obj = ipaddress.ip_address(host)
        ip = str(ip_obj)
    except ValueError:
        try:
            ip = socket.gethostbyname(host)
        except socket.gaierror as e:
            raise FetchError(ErrorCode.DNS_FAILED, f"{host}: {e}") from e

    if _is_private_ip(ip) and not allow_private:
        if not (extra_allowlist and host in extra_allowlist):
            raise FetchError(
                ErrorCode.URL_NOT_ALLOWED,
                f"host {host} resolves to private/loopback IP {ip}",
            )

    return ResolvedURL(url=url, hostname=host, etld1=etld1(host), ip=ip)
