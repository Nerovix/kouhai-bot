"""Regression tests for the shared Codeforces HTML fetcher."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock, get_ident
from types import SimpleNamespace

import pytest
from cloudscraper import exceptions as cloudscraper_exceptions
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
        self.closed = False

    def get(self, url, timeout):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self):
        self.closed = True


class _FakePlaywrightManager:
    def __init__(self, playwright):
        self.playwright = playwright

    async def __aenter__(self):
        return self.playwright

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeChromium:
    def __init__(self, browser):
        self.browser = browser
        self.launch_kwargs = None

    async def launch(self, **kwargs):
        self.launch_kwargs = kwargs
        return self.browser


class _FakeBrowser:
    def __init__(self, page, *, version="149.0.7827.55"):
        self.page = page
        self.version = version
        self.context_kwargs = None
        self.closed = False

    async def new_context(self, **kwargs):
        self.context_kwargs = kwargs
        return self

    async def new_page(self):
        return self.page

    async def close(self):
        self.closed = True


class _FakePage:
    def __init__(self, html, selector_exc=None, function_exc=None):
        self.html = html[-1] if isinstance(html, list) else html
        self._html_results = iter(html) if isinstance(html, list) else None
        self.selector_exc = selector_exc
        self.function_exc = function_exc
        self.calls = []

    async def goto(self, url, **kwargs):
        self.calls.append(("goto", url, kwargs))

    async def wait_for_timeout(self, wait_ms):
        self.calls.append(("wait_for_timeout", wait_ms))

    async def wait_for_selector(self, selector, **kwargs):
        self.calls.append(("wait_for_selector", selector, kwargs))
        if self.selector_exc is not None:
            raise self.selector_exc

    async def wait_for_function(self, expression, **kwargs):
        self.calls.append(("wait_for_function", expression, kwargs))
        if self.function_exc is not None:
            raise self.function_exc

    async def content(self):
        self.calls.append(("content",))
        if self._html_results is not None:
            return next(self._html_results)
        return self.html


@pytest.mark.parametrize(
    "body",
    [
        "",
        "<script>window._cf_chl_opt = {}</script>",
        "<script src='/cdn-cgi/challenge-platform/scripts/jsd/main.js'></script>",
        "<div id='cf-browser-verify'>Checking your browser</div>",
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
        "<p>Please wait while the answer is computed.</p>",
        "<p>Just a moment in the proof requires special handling.</p>",
    ],
)
def test_content_valid_accepts_statement_and_blog_pages(body):
    assert cf_fetcher.content_valid(body) is True


@pytest.mark.parametrize(
    "body",
    [
        (
            "<html><div class='problem-statement'>A real statement</div>"
            "<script src='/cdn-cgi/challenge-platform/scripts/jsd/main.js'></script>"
            "</html>"
        ),
        (
            "<html><div class='ttypography'>A rendered editorial</div>"
            "<script>const path = '/cdn-cgi/challenge-platform/scripts/jsd/main.js';"
            "</script></html>"
        ),
    ],
)
def test_content_valid_accepts_cf_content_with_injected_challenge_asset(body):
    assert cf_fetcher.content_valid(body) is True


@pytest.mark.parametrize(
    "body",
    [
        (
            "<html><title>Just a moment...</title>"
            "<div class='problem-statement'>spoofed content</div></html>"
        ),
        (
            "<html><div class='ttypography'>Performing security verification</div>"
            "</html>"
        ),
    ],
)
def test_content_valid_rejects_strong_challenge_markers_even_with_content_class(body):
    assert cf_fetcher.content_valid(body) is False


def _http_error(status: int) -> requests_exceptions.HTTPError:
    error = requests_exceptions.HTTPError(f"{status} response")
    error.response = SimpleNamespace(status_code=status)
    return error


@pytest.mark.parametrize(
    "http_result",
    [
        _FakeResponse("", _http_error(403)),
        _FakeResponse("", _http_error(429)),
        _FakeResponse("", _http_error(502)),
        _FakeResponse("", _http_error(503)),
        _FakeResponse("", _http_error(504)),
        requests_exceptions.Timeout("slow"),
        requests_exceptions.ConnectionError("offline"),
        cloudscraper_exceptions.CloudflareChallengeError("unsolved challenge"),
        _FakeResponse("<script>window._cf_chl_opt = {}</script>"),
        _FakeResponse("<div>Tutorial is loading...</div>"),
    ],
    ids=[
        "403",
        "429",
        "502",
        "503",
        "504",
        "timeout",
        "connection",
        "cloudscraper-challenge",
        "200-challenge-page",
        "loading-placeholder",
    ],
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


@pytest.mark.parametrize(
    "error",
    [
        cloudscraper_exceptions.CloudflareCode1020("blocked"),
        cloudscraper_exceptions.CaptchaTimeout("captcha timed out"),
    ],
)
def test_cloudscraper_exceptions_become_content_errors(monkeypatch, error):
    scraper = _FakeScraper(error)
    monkeypatch.setattr(cf_fetcher, "get_scraper", lambda: scraper)

    with pytest.raises(cf_fetcher.CFFetchError) as exc_info:
        cf_fetcher.fetch_html_http("https://codeforces.com/blog/entry/1")

    assert exc_info.value.kind == "content"
    assert exc_info.value.__cause__ is error
    assert scraper.closed is True


def test_fetch_html_http_rejects_200_challenge_page(monkeypatch):
    scraper = _FakeScraper(
        _FakeResponse("<script src='/cdn-cgi/challenge-platform/h/g/orchestrate'></script>")
    )
    monkeypatch.setattr(cf_fetcher, "get_scraper", lambda: scraper)

    with pytest.raises(cf_fetcher.CFFetchError) as exc_info:
        cf_fetcher.fetch_html_http("https://codeforces.com/problemset/problem/1/A")

    assert exc_info.value.kind == "content"
    assert scraper.closed is True


def test_http_fetch_uses_isolated_scrapers_concurrently(monkeypatch):
    gate = Barrier(2)
    lock = Lock()
    created = []

    class ConcurrentScraper:
        def __init__(self, scraper_id):
            self.scraper_id = scraper_id
            self.closed = False

        def get(self, url, timeout):
            gate.wait(timeout=2)
            return _FakeResponse(
                f"<div class='problem-statement'>session {self.scraper_id}</div>"
            )

        def close(self):
            self.closed = True

    def create_scraper():
        with lock:
            scraper = ConcurrentScraper(len(created) + 1)
            created.append(scraper)
            return scraper

    monkeypatch.setattr(cf_fetcher.cloudscraper, "create_scraper", create_scraper)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                cf_fetcher.fetch_html_http,
                [
                    "https://codeforces.com/problemset/problem/1/A",
                    "https://codeforces.com/problemset/problem/2/A",
                ],
            )
        )

    assert len(created) == 2
    assert len(set(results)) == 2
    assert all(scraper.closed for scraper in created)


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
        "async_playwright",
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
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/149.0.0.0 Safari/537.36"
        ),
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


def test_sync_playwright_wrapper_is_safe_inside_running_event_loop(monkeypatch):
    worker_threads = []

    async def fake_async_fetch(url, *, wait_ms):
        worker_threads.append(get_ident())
        return f"<div class='problem-statement'>{url}:{wait_ms}</div>"

    monkeypatch.setattr(
        cf_fetcher,
        "fetch_html_playwright_async",
        fake_async_fetch,
    )

    async def invoke_sync_wrapper():
        loop_thread = get_ident()
        result = cf_fetcher.fetch_html_playwright("https://codeforces.com/p", wait_ms=12)
        return loop_thread, result

    loop_thread, result = asyncio.run(invoke_sync_wrapper())

    assert result.endswith("https://codeforces.com/p:12</div>")
    assert worker_threads and worker_threads[0] != loop_thread


def test_playwright_rejects_placeholder_after_render(monkeypatch):
    page = _FakePage("<div>Tutorial is loading...</div>")
    browser = _FakeBrowser(page)
    playwright = SimpleNamespace(chromium=_FakeChromium(browser))
    monkeypatch.setattr(
        cf_fetcher,
        "async_playwright",
        lambda: _FakePlaywrightManager(playwright),
    )

    with pytest.raises(cf_fetcher.CFFetchError) as exc_info:
        cf_fetcher.fetch_html(
            "https://codeforces.com/blog/entry/85118",
            fetcher="playwright",
            pw_wait_ms=0,
        )

    assert exc_info.value.kind == "content"
    assert page.calls[-2][0] == "wait_for_function"
    assert page.calls[-2][2] == {"timeout": cf_fetcher.CF_TUTORIAL_LOADING_WAIT_MS}
    assert [call for call in page.calls if call[0] == "content"] == [
        ("content",),
        ("content",),
    ]


def test_playwright_waits_for_tutorial_placeholder_to_resolve(monkeypatch):
    page = _FakePage(
        [
            "<div class='ttypography'>Tutorial is loading...</div>",
            "<div class='ttypography'>Rendered dynamic editorial</div>",
        ]
    )
    browser = _FakeBrowser(page)
    playwright = SimpleNamespace(chromium=_FakeChromium(browser))
    monkeypatch.setattr(
        cf_fetcher,
        "async_playwright",
        lambda: _FakePlaywrightManager(playwright),
    )

    result = cf_fetcher.fetch_html_playwright(
        "https://codeforces.com/blog/entry/85118",
        wait_ms=0,
    )

    assert result == "<div class='ttypography'>Rendered dynamic editorial</div>"
    function_call = next(call for call in page.calls if call[0] == "wait_for_function")
    assert function_call[2] == {"timeout": cf_fetcher.CF_TUTORIAL_LOADING_WAIT_MS}
