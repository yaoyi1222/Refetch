from enum import Enum


class ErrorCode(str, Enum):
    BLOCKED_BY_CLOUDFLARE = "BLOCKED_BY_CLOUDFLARE"
    JS_TIMEOUT = "JS_TIMEOUT"
    DNS_FAILED = "DNS_FAILED"
    URL_NOT_ALLOWED = "URL_NOT_ALLOWED"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    PROFILE_DOMAIN_MISMATCH = "PROFILE_DOMAIN_MISMATCH"
    PROFILE_NOT_FOUND = "PROFILE_NOT_FOUND"
    CONTENT_TOO_LARGE = "CONTENT_TOO_LARGE"
    HTTP_ERROR = "HTTP_ERROR"
    TIMEOUT = "TIMEOUT"
    UNSUPPORTED_CONTENT_TYPE = "UNSUPPORTED_CONTENT_TYPE"
    SPA_NAVIGATION_LOOP = "SPA_NAVIGATION_LOOP"
    PDF_NO_TEXT_LAYER = "PDF_NO_TEXT_LAYER"
    PDF_FETCH_BLOCKED = "PDF_FETCH_BLOCKED"
    ACTION_FAILED = "ACTION_FAILED"
    # v0.3 cache codes. See docs/v0.3/design.md §7.
    # CACHE_MISS / CRAWL_MAX_PAGES / ROBOTS_DISALLOWED are "expected
    # branches" — callers should treat them as info, not hard failure.
    CACHE_MISS = "CACHE_MISS"                  # cache_only=True and no hit
    CACHE_CORRUPT = "CACHE_CORRUPT"            # payload / index disagree
    CACHE_FLAG_CONFLICT = "CACHE_FLAG_CONFLICT"  # mutually exclusive CLI flags
    # PR 4 — sitemap/map. A parse failure on one sitemap is a soft signal:
    # `run_map` downgrades to the homepage-link fallback rather than failing.
    SITEMAP_PARSE_ERROR = "SITEMAP_PARSE_ERROR"
    # PR 5 — crawl jobs. Raised by the jobs data layer's load/resume
    # validation; PR 6's crawl-status/resume/cancel subcommands consume them.
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_NOT_RESUMABLE = "JOB_NOT_RESUMABLE"
    # PR 6.1 — crawl robots. An "expected branch" (design §7): the crawl engine
    # tags URLs skipped by robots.txt with this and counts them separately from
    # failures; `ok` may still be true.
    ROBOTS_DISALLOWED = "ROBOTS_DISALLOWED"
    # PR 6.2 — crawl engine. Info-level "expected branch" (design §7): the crawl
    # hit its --max-pages cap and stopped; the job still finalizes `completed`.
    CRAWL_MAX_PAGES = "CRAWL_MAX_PAGES"
    UNKNOWN = "UNKNOWN"


class FetchError(Exception):
    def __init__(self, code: ErrorCode, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code.value}: {detail}")
