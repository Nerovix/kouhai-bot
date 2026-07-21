"""Regression tests for the shared Codeforces HTML fetcher."""

from types import SimpleNamespace

import pytest
from requests import exceptions as requests_exceptions

from kouhai_bot.problems import cf_fetcher


class _FakeResponse:
    def __init__(self, body: str, error: Exception | None = None):
        self.text = body
        self.error = error

    def raise_for_status(self):
        if self.error is not None:
            raise self.error


class _FakeScraper:
    def __init__(self, result):
        self.result = result

    def get(self, url, timeout):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _FakePlaywrightManager:
    def __init__(self, playwright):
        self.playwright = playwright

    def __enter__(self):
        return self.playwright

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeChromium:
    def __init__(self, browser):
        self.browser = browser
        self.launch_kwargs = None

    def launch(self, **kwargs):
        self.launch_kwargs = kwargs
        return self.browser


class _FakeBrowser:
    def __init__(self, page):
        self.page = page
        self.context_kwargs = None
        self.closed = False

    def new_context(self, **kwargs):
        self.context_kwargs = kwargs
        return self

    def new_page(self):
        return self.page

    def close(self):
        self.closed = True


class _FakePage:
    def __init__(self, html, selector_exc=None):
        self.html = html
        self.selector_exc = selector_exc
        self.calls = []

    def goto(self, url, **kwargs):
        self.calls.append(("goto", url, kwargs))

    def wait_for_timeout(self, wait_ms):
        self.calls.append(("wait_for_timeout", wait_ms))

    def wait_for_selector(self, selector, **kwargs):
        self.calls.append(("wait_for_selector", selector, kwargs))
        if self.selector_exc is not None:
            raise self.selector_exc

    def content(self):
        self.calls.append(("content",))
        return self.html


@pytest.mark.parametrize(
    "body",
    [
        "",
        "<html><title>Just a moment...</title></html>",
        "<html><body>Please wait.</body></html>",
        "<div class='ttypography'>Tutorial is loading...</div>",
        "<div>Tutorial is loading</div>",
    ],
)
def test_content_valid_rejects_unusable_responses(body):
    assert cf_fetcher.content_valid(body) is False


@pytest.mark.parametrize(
    "body",
    [
        "<div class='problem-statement'>A real statement</div>",
        "<div class='ttypography'>A real editorial with an algorithm.</div>",
    ],
)
def test_content_valid_accepts_statement_and_blog_pages(body):
    assert cf_fetcher.content_valid(body) is True


def _http_error(status: int) -> requests_exceptions.HTTPError:
    error = requests_exceptions.HTTPError(f"{status} response")
    error.response = SimpleNamespace(status_code=status)
    return error


@pytest.mark.parametrize(
    "http_result",
    [
        _FakeResponse("", _http_error(403)),
        requests_exceptions.Timeout("slow"),
        requests_exceptions.ConnectionError("offline"),
        _FakeResponse("<div>Tutorial is loading...</div>"),
    ],
    ids=["403", "timeout", "connection", "loading-placeholder"],
)
def test_auto_falls_back_to_playwright(monkeypatch, http_result):
    browser_calls = []
    monkeypatch.setattr(cf_fetcher, "get_scraper", lambda: _FakeScraper(http_result))

    def fake_browser(url, *, wait_ms):
        browser_calls.append((url, wait_ms))
        return "<div class='ttypography'>rendered editorial</div>"

    monkeypatch.setattr(cf_fetcher, "fetch_html_playwright", fake_browser)

    result = cf_fetcher.fetch_html(
        "https://codeforces.com/blog/entry/85118",
        pw_wait_ms=456,
    )

    assert result == "<div class='ttypography'>rendered editorial</div>"
    assert browser_calls == [("https://codeforces.com/blog/entry/85118", 456)]


def test_non_fallback_http_error_is_reported(monkeypatch):
    monkeypatch.setattr(
        cf_fetcher,
        "get_scraper",
        lambda: _FakeScraper(_FakeResponse("", _http_error(404))),
    )
    monkeypatch.setattr(
        cf_fetcher,
        "fetch_html_playwright",
        lambda *args, **kwargs: pytest.fail("404 must not launch a browser"),
    )

    with pytest.raises(cf_fetcher.CFFetchError) as exc_info:
        cf_fetcher.fetch_html("https://codeforces.com/blog/entry/404")

    assert exc_info.value.kind == "http"


def test_explicit_playwright_uses_headless_chromium(monkeypatch):
    page = _FakePage("<div class='problem-statement'>rendered</div>")
    browser = _FakeBrowser(page)
    chromium = _FakeChromium(browser)
    playwright = SimpleNamespace(chromium=chromium)
    monkeypatch.setattr(
        cf_fetcher,
        "sync_playwright",
        lambda: _FakePlaywrightManager(playwright),
    )

    result = cf_fetcher.fetch_html(
        "https://codeforces.com/problemset/problem/1534/F2",
        fetcher="playwright",
        pw_wait_ms=321,
    )

    assert result == page.html
    assert chromium.launch_kwargs == {"headless": True}
    assert browser.context_kwargs == {
        "user_agent": cf_fetcher.CF_PLAYWRIGHT_USER_AGENT,
        "viewport": cf_fetcher.CF_PLAYWRIGHT_VIEWPORT,
    }
    assert page.calls == [
        (
            "goto",
            "https://codeforces.com/problemset/problem/1534/F2",
            {"wait_until": "domcontentloaded", "timeout": 45_000},
        ),
        ("wait_for_timeout", 321),
        (
            "wait_for_selector",
            cf_fetcher.CF_CONTENT_SELECTOR,
            {"timeout": cf_fetcher.CF_CONTENT_WAIT_MS},
        ),
        ("content",),
    ]
    assert browser.closed is True


def test_playwright_rejects_placeholder_after_render(monkeypatch):
    page = _FakePage("<div>Tutorial is loading...</div>")
    browser = _FakeBrowser(page)
    playwright = SimpleNamespace(chromium=_FakeChromium(browser))
    monkeypatch.setattr(
        cf_fetcher,
        "sync_playwright",
        lambda: _FakePlaywrightManager(playwright),
    )

    with pytest.raises(cf_fetcher.CFFetchError) as exc_info:
        cf_fetcher.fetch_html(
            "https://codeforces.com/blog/entry/85118",
            fetcher="playwright",
            pw_wait_ms=0,
        )

    assert exc_info.value.kind == "content"
