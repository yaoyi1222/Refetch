"""PR 4 — PDF parsing via pypdf (L1-only, no L2 fallback).

Offline tests cover:
  - Successful 2-page PDF extraction with page separator
  - Scanned PDF (no text layer) → PDF_NO_TEXT_LAYER
  - Non-PDF response → UNSUPPORTED_CONTENT_TYPE
  - Magic-bytes fallback when Content-Type is missing
  - PDF_FETCH_BLOCKED on curl_cffi error
  - Router integration: .pdf dispatch, non-.pdf binaries still rejected
"""

from unittest.mock import MagicMock, patch

import pytest

from lightcrawl.errors import ErrorCode, FetchError
from lightcrawl.fetch_http import HttpResult
from lightcrawl.fetch_pdf import PdfResult, fetch_pdf
from lightcrawl.router import FetchRequest, Router


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
    monkeypatch.setattr("lightcrawl.content.DUMPS", tmp_path / "dumps")
    monkeypatch.setattr("lightcrawl.auth.PROFILES", tmp_path / "profiles")
    (tmp_path / "dumps").mkdir(parents=True)
    (tmp_path / "profiles").mkdir(parents=True)


_FAKE_PDF_BYTES = b"%PDF-1.4\n%fake pdf content\n%%EOF"


def _fake_curl_response(content=None, content_type="application/pdf", url="https://example.com/doc.pdf"):
    """Build a mock curl_cffi response object."""
    if content is None:
        content = _FAKE_PDF_BYTES
    r = MagicMock()
    r.content = content
    r.url = url
    r.headers = {"content-type": content_type}
    return r


# ══ fetch_pdf unit tests ══


def test_fetch_pdf_two_pages(monkeypatch):
    """2-page PDF → markdown with \n\n---\n\n separator."""
    mock_reader = MagicMock()
    page1 = MagicMock()
    page1.extract_text.return_value = "Page One Content"
    page2 = MagicMock()
    page2.extract_text.return_value = "Page Two Content"
    mock_reader.pages = [page1, page2]

    monkeypatch.setattr("lightcrawl.fetch_pdf.ccr.get",
                        lambda *a, **kw: _fake_curl_response())
    monkeypatch.setattr("pypdf.PdfReader",
                        lambda *a, **kw: mock_reader)

    result = fetch_pdf("https://example.com/doc.pdf")
    assert result.num_pages == 2
    assert result.content_length == len(_FAKE_PDF_BYTES)
    assert result.markdown == "Page One Content\n\n---\n\nPage Two Content"


def test_fetch_pdf_no_text_layer(monkeypatch):
    """Blank pages (no extractable text) → PDF_NO_TEXT_LAYER."""
    mock_reader = MagicMock()
    page1 = MagicMock()
    page1.extract_text.return_value = ""
    page2 = MagicMock()
    page2.extract_text.return_value = "   "  # whitespace-only → stripped
    mock_reader.pages = [page1, page2]

    monkeypatch.setattr("lightcrawl.fetch_pdf.ccr.get",
                        lambda *a, **kw: _fake_curl_response())
    monkeypatch.setattr("pypdf.PdfReader",
                        lambda *a, **kw: mock_reader)

    with pytest.raises(FetchError) as exc:
        fetch_pdf("https://example.com/scan.pdf")
    assert exc.value.code == ErrorCode.PDF_NO_TEXT_LAYER


def test_fetch_pdf_rejects_non_pdf_content_type(monkeypatch):
    """text/html Content-Type + no %PDF magic → UNSUPPORTED_CONTENT_TYPE."""
    monkeypatch.setattr("lightcrawl.fetch_pdf.ccr.get",
                        lambda *a, **kw: _fake_curl_response(
                            content=b"<html>not a pdf</html>",
                            content_type="text/html",
                        ))

    with pytest.raises(FetchError) as exc:
        fetch_pdf("https://example.com/fake.pdf")
    assert exc.value.code == ErrorCode.UNSUPPORTED_CONTENT_TYPE


def test_fetch_pdf_accepts_magic_bytes_without_content_type(monkeypatch):
    """No Content-Type header but body starts with %PDF → accept."""
    mock_reader = MagicMock()
    mock_reader.pages = [MagicMock()]
    mock_reader.pages[0].extract_text.return_value = "Valid PDF"

    monkeypatch.setattr("lightcrawl.fetch_pdf.ccr.get",
                        lambda *a, **kw: _fake_curl_response(content_type=""))
    monkeypatch.setattr("pypdf.PdfReader",
                        lambda *a, **kw: mock_reader)

    result = fetch_pdf("https://example.com/doc.pdf")
    assert result.num_pages == 1


def test_fetch_pdf_extracts_title_from_metadata(monkeypatch):
    """Closes #41. When pypdf metadata has a real title, surface it directly."""
    mock_reader = MagicMock()
    mock_reader.metadata = MagicMock(title="Annual Report 2025")
    page1 = MagicMock()
    page1.extract_text.return_value = "body text"
    mock_reader.pages = [page1]
    monkeypatch.setattr("lightcrawl.fetch_pdf.ccr.get",
                        lambda *a, **kw: _fake_curl_response())
    monkeypatch.setattr("pypdf.PdfReader", lambda *a, **kw: mock_reader)

    result = fetch_pdf("https://example.com/doc.pdf")
    assert result.title == "Annual Report 2025"


def test_fetch_pdf_falls_back_to_first_meaningful_line_when_metadata_title_empty(
    monkeypatch,
):
    """Closes #41. Real-world example from the issue: notice.10jqka.com.cn
    serves PDFs with empty metadata title but a clear cover-page heading.
    Skip page-numbers ("1 / 239") and ultra-short tokens, take the first
    line of substance."""
    mock_reader = MagicMock()
    # An empty-string title (the exact failure mode in the issue) and a None
    # title should both behave the same — fall back to the body.
    mock_reader.metadata = MagicMock(title="")
    page1 = MagicMock()
    page1.extract_text.return_value = (
        "1\n"                                            # bare page number
        "1 / 239\n"                                      # X / Y page marker
        "Page 1 of 239\n"                                # "Page X of Y" format
        "健康元药业集团股份有限公司 2025 年年度报告\n"  # ← this is the title
        "目录\n"
        "1\n"
    )
    mock_reader.pages = [page1]
    monkeypatch.setattr("lightcrawl.fetch_pdf.ccr.get",
                        lambda *a, **kw: _fake_curl_response())
    monkeypatch.setattr("pypdf.PdfReader", lambda *a, **kw: mock_reader)

    result = fetch_pdf("https://example.com/doc.pdf")
    assert result.title == "健康元药业集团股份有限公司 2025 年年度报告"


def test_fetch_pdf_short_cjk_title_accepted(monkeypatch):
    """len(s) < 2 must not filter 2-char CJK titles (e.g. '摘要', '目录').
    The original threshold of 4 rejected these — a real-words title at
    exactly 2 codepoints, perfectly valid Chinese headings."""
    mock_reader = MagicMock()
    mock_reader.metadata = MagicMock(title="")
    page1 = MagicMock()
    page1.extract_text.return_value = "1\n2\n3\n摘要\n"
    mock_reader.pages = [page1]
    monkeypatch.setattr("lightcrawl.fetch_pdf.ccr.get",
                        lambda *a, **kw: _fake_curl_response())
    monkeypatch.setattr("pypdf.PdfReader", lambda *a, **kw: mock_reader)
    result = fetch_pdf("https://example.com/doc.pdf")
    assert result.title == "摘要"


def test_fetch_pdf_title_empty_when_metadata_missing_and_no_meaningful_line(
    monkeypatch,
):
    """If even the body has no usable first-line heading (only page numbers /
    whitespace), title stays empty rather than picking up a meaningless line."""
    mock_reader = MagicMock()
    mock_reader.metadata = None
    page1 = MagicMock()
    page1.extract_text.return_value = "1\n2\n   \n3 / 5\nPage 1 of 5\n"
    mock_reader.pages = [page1]
    monkeypatch.setattr("lightcrawl.fetch_pdf.ccr.get",
                        lambda *a, **kw: _fake_curl_response())
    monkeypatch.setattr("pypdf.PdfReader", lambda *a, **kw: mock_reader)

    result = fetch_pdf("https://example.com/doc.pdf")
    # Falls back to the longest non-numeric line if any; else "".
    # In this contrived case every line is numeric / whitespace.
    assert result.title == ""


def test_fetch_pdf_timeout_maps_to_fetch_error(monkeypatch):
    """curl_cffi timeout → ErrorCode.TIMEOUT."""
    import curl_cffi

    def raise_timeout(*a, **kw):
        raise curl_cffi.requests.errors.RequestsError("Connection timed out")

    monkeypatch.setattr("lightcrawl.fetch_pdf.ccr.get", raise_timeout)
    with pytest.raises(FetchError) as exc:
        fetch_pdf("https://example.com/doc.pdf")
    assert exc.value.code == ErrorCode.TIMEOUT


def test_fetch_pdf_other_curl_error_maps_to_pdf_fetch_blocked(monkeypatch):
    """Non-timeout curl_cffi errors → PDF_FETCH_BLOCKED."""
    import curl_cffi

    def raise_ssl(*a, **kw):
        raise curl_cffi.requests.errors.RequestsError("SSL handshake failed")

    monkeypatch.setattr("lightcrawl.fetch_pdf.ccr.get", raise_ssl)
    with pytest.raises(FetchError) as exc:
        fetch_pdf("https://example.com/doc.pdf")
    assert exc.value.code == ErrorCode.PDF_FETCH_BLOCKED


def test_fetch_pdf_content_too_large(monkeypatch):
    """Response > 50MB → CONTENT_TOO_LARGE."""
    monkeypatch.setattr("lightcrawl.fetch_pdf.ccr.get",
                        lambda *a, **kw: _fake_curl_response(
                            content=b"x" * (50 * 1024 * 1024 + 1),
                        ))

    with pytest.raises(FetchError) as exc:
        fetch_pdf("https://example.com/huge.pdf")
    assert exc.value.code == ErrorCode.CONTENT_TOO_LARGE


def test_fetch_pdf_corrupt_pdf_raises(monkeypatch):
    """pypdf can't open the file → UNSUPPORTED_CONTENT_TYPE."""
    monkeypatch.setattr("lightcrawl.fetch_pdf.ccr.get",
                        lambda *a, **kw: _fake_curl_response(
                            content=b"not a pdf at all",
                            content_type="application/pdf",
                        ))

    with pytest.raises(FetchError) as exc:
        fetch_pdf("https://example.com/corrupt.pdf")
    assert exc.value.code == ErrorCode.UNSUPPORTED_CONTENT_TYPE


# ══ Router integration ══


async def test_pdf_router_integration_success(router):
    """End-to-end through Router: .pdf URL → fetch_pdf → success response."""
    def fake_fetch_pdf(url, *, timeout, headers=None):
        return PdfResult(
            markdown="Page 1\n\n---\n\nPage 2",
            num_pages=2,
            content_length=1000,
            final_url=url,
            elapsed_ms=42,
        )

    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"), \
         patch("lightcrawl.fetch_pdf.fetch_pdf", side_effect=fake_fetch_pdf):
        out = await router.fetch(
            FetchRequest(url="https://example.com/doc.pdf")
        )

    assert out["ok"] is True
    assert out["strategy_used"] == "pdf"
    assert out["content"] == "Page 1\n\n---\n\nPage 2"
    assert out["metadata"]["num_pages"] == 2
    assert out["metadata"]["content_length"] == 1000
    assert out["metadata"]["content_type"] == "application/pdf"
    assert out["metadata"]["links"] == []
    assert out["metadata"]["images"] == []


async def test_pdf_router_integration_failure(router):
    """Router surfaces fetch_pdf errors."""
    def fake_fetch_pdf(*args, **kwargs):
        raise FetchError(ErrorCode.PDF_NO_TEXT_LAYER, "no text")

    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"), \
         patch("lightcrawl.fetch_pdf.fetch_pdf", side_effect=fake_fetch_pdf):
        out = await router.fetch(
            FetchRequest(url="https://example.com/scan.pdf")
        )

    assert out["ok"] is False
    assert out["error_code"] == ErrorCode.PDF_NO_TEXT_LAYER.value


async def test_pdf_redirect_to_html_suggests_browser_strategy(router):
    """Closes #40. SSE / 10jqka / Sina announcement PDFs commonly redirect
    to an HTML download/landing page. fetch_pdf raises
    UNSUPPORTED_CONTENT_TYPE (correct — the body isn't a PDF), but the
    bare error gives the user no next step. Surface a suggestion that
    points at `--strategy browser`, which can render the landing page
    so the user can grab the real PDF URL or screenshot the inline body."""
    def fake_fetch_pdf(*args, **kwargs):
        raise FetchError(
            ErrorCode.UNSUPPORTED_CONTENT_TYPE,
            "expected application/pdf or %PDF magic bytes; got content-type='text/html; charset=utf-8'",
        )

    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"), \
         patch("lightcrawl.fetch_pdf.fetch_pdf", side_effect=fake_fetch_pdf):
        out = await router.fetch(
            FetchRequest(url="https://static.sse.com.cn/disclosure/600380_UYT2.pdf")
        )

    assert out["ok"] is False
    assert out["error_code"] == ErrorCode.UNSUPPORTED_CONTENT_TYPE.value
    assert any("--strategy browser" in s for s in out["suggestions"]), out["suggestions"]


async def test_pdf_no_text_layer_does_not_get_browser_suggestion(router):
    """Browser strategy doesn't help a scanned PDF — surfacing the hint there
    would be misleading. Limit the suggestion to the redirect-to-HTML case."""
    def fake_fetch_pdf(*args, **kwargs):
        raise FetchError(ErrorCode.PDF_NO_TEXT_LAYER, "scanned PDF, no text layer")

    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"), \
         patch("lightcrawl.fetch_pdf.fetch_pdf", side_effect=fake_fetch_pdf):
        out = await router.fetch(
            FetchRequest(url="https://example.com/scan.pdf")
        )

    assert out["ok"] is False
    assert not any("--strategy browser" in s for s in out["suggestions"])


async def test_non_pdf_binaries_still_rejected(router):
    """PR 4 only removes .pdf from the binary list; .zip still rejected."""
    out = await router.fetch(FetchRequest(url="https://example.com/archive.zip"))
    assert out["ok"] is False
    assert out["error_code"] == ErrorCode.UNSUPPORTED_CONTENT_TYPE.value
    assert out["attempts"] == []


async def test_pdf_query_param_not_treated_as_pdf(router):
    """.pdf in query string (not path) → normal HTML fetch, not PDF route."""
    fake = HttpResult(
        final_url="https://example.com/search?q=foo.pdf",
        status_code=200,
        text=(
            "<html><head><title>Search</title></head><body>"
            "<article><h1>Results</h1><p>search results text long enough "
            "to bypass the tiny-body escalation heuristic and produce "
            "stable output in tests.</p></article></body></html>"
        ),
        content_type="text/html",
        elapsed_ms=5,
    )
    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="93.184.216.34"), \
         patch("lightcrawl.fetch_http.fetch", return_value=fake):
        out = await router.fetch(
            FetchRequest(url="https://example.com/search?q=foo.pdf")
        )

    assert out["ok"] is True
    assert out["strategy_used"] == "http"  # normal HTML, not "pdf"


async def test_ssrf_guards_still_apply_to_pdf_urls(router):
    """PDF dispatch must come AFTER SSRF validation — private IP PDFs blocked."""
    with patch("lightcrawl.url_safety.socket.gethostbyname", return_value="127.0.0.1"):
        out = await router.fetch(
            FetchRequest(url="http://localhost/doc.pdf")
        )
    assert out["ok"] is False
    assert out["error_code"] == ErrorCode.URL_NOT_ALLOWED.value
