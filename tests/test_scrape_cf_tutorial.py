"""Tests for low-level Codeforces tutorial scraping helpers."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import scrape_cf_tutorial


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


class _FakePlaywright:
    def __init__(self, browser):
        self.chromium = _FakeChromium(browser)


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
    def __init__(
        self,
        *,
        html="<html><body><div class='problem-statement'>ok</div></body></html>",
        title="Problem - 601D - Codeforces",
        selector_exc=None,
    ):
        self.html = html
        self.title_text = title
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

    def title(self):
        self.calls.append(("title",))
        return self.title_text

    def content(self):
        self.calls.append(("content",))
        return self.html


def test_fetch_html_auto_falls_back_on_any_scrape_error(monkeypatch):
    def fake_http(url):
        raise scrape_cf_tutorial.ScrapeError(f"forbidden: {url}", 2)

    def fake_playwright(url, wait_ms):
        assert url == "https://codeforces.com/blog/entry/1"
        assert wait_ms == 123
        return "<html>ok</html>"

    monkeypatch.setattr(scrape_cf_tutorial, "fetch_html_http", fake_http)
    monkeypatch.setattr(scrape_cf_tutorial, "fetch_html_playwright", fake_playwright)

    html = scrape_cf_tutorial.fetch_html(
        "https://codeforces.com/blog/entry/1",
        fetcher="auto",
        pw_wait_ms=123,
    )

    assert html == "<html>ok</html>"


def test_fetch_html_auto_falls_back_on_non_scrape_error(monkeypatch):
    def fake_http(url):
        raise OSError(f"network down: {url}")

    def fake_playwright(url, wait_ms):
        assert url == "https://codeforces.com/blog/entry/2"
        assert wait_ms == 456
        return "<html>browser ok</html>"

    monkeypatch.setattr(scrape_cf_tutorial, "fetch_html_http", fake_http)
    monkeypatch.setattr(scrape_cf_tutorial, "fetch_html_playwright", fake_playwright)

    html = scrape_cf_tutorial.fetch_html(
        "https://codeforces.com/blog/entry/2",
        fetcher="auto",
        pw_wait_ms=456,
    )

    assert html == "<html>browser ok</html>"


def test_fetch_html_playwright_waits_for_cf_content(monkeypatch):
    page = _FakePage()
    browser = _FakeBrowser(page)
    fake_playwright = _FakePlaywright(browser)
    monkeypatch.setattr(
        scrape_cf_tutorial,
        "sync_playwright",
        lambda: _FakePlaywrightManager(fake_playwright),
    )

    html = scrape_cf_tutorial.fetch_html_playwright(
        "https://codeforces.com/problemset/problem/601/D",
        wait_ms=321,
    )

    assert html == page.html
    assert fake_playwright.chromium.launch_kwargs == {"headless": True}
    assert browser.context_kwargs == {
        "user_agent": scrape_cf_tutorial.CF_PLAYWRIGHT_USER_AGENT,
        "viewport": scrape_cf_tutorial.CF_PLAYWRIGHT_VIEWPORT,
    }
    assert page.calls == [
        (
            "goto",
            "https://codeforces.com/problemset/problem/601/D",
            {"wait_until": "domcontentloaded", "timeout": 45000},
        ),
        ("wait_for_timeout", 321),
        (
            "wait_for_selector",
            scrape_cf_tutorial.CF_CONTENT_SELECTOR,
            {"timeout": scrape_cf_tutorial.CF_CONTENT_WAIT_MS},
        ),
        ("content",),
    ]
    assert browser.closed is True


def test_fetch_html_playwright_selector_timeout_checks_challenge_title(monkeypatch):
    class FakeTimeout(Exception):
        pass

    page = _FakePage(selector_exc=FakeTimeout(), title="Just a moment...")
    browser = _FakeBrowser(page)
    fake_playwright = _FakePlaywright(browser)
    monkeypatch.setattr(scrape_cf_tutorial, "PlaywrightTimeoutError", FakeTimeout)
    monkeypatch.setattr(
        scrape_cf_tutorial,
        "sync_playwright",
        lambda: _FakePlaywrightManager(fake_playwright),
    )

    try:
        scrape_cf_tutorial.fetch_html_playwright("https://codeforces.com/blog/entry/1")
    except scrape_cf_tutorial.ScrapeError as exc:
        assert exc.code == 9
    else:
        raise AssertionError("expected ScrapeError")

    assert page.calls[-1] == ("title",)


def test_fetch_html_playwright_selector_timeout_allows_non_challenge_title(monkeypatch):
    class FakeTimeout(Exception):
        pass

    page = _FakePage(
        html="<html><title>Other Codeforces page</title><body>ok</body></html>",
        title="Other Codeforces page",
        selector_exc=FakeTimeout(),
    )
    browser = _FakeBrowser(page)
    fake_playwright = _FakePlaywright(browser)
    monkeypatch.setattr(scrape_cf_tutorial, "PlaywrightTimeoutError", FakeTimeout)
    monkeypatch.setattr(
        scrape_cf_tutorial,
        "sync_playwright",
        lambda: _FakePlaywrightManager(fake_playwright),
    )

    html = scrape_cf_tutorial.fetch_html_playwright("https://codeforces.com/blog/entry/1")

    assert html == page.html
    assert page.calls[-2:] == [("title",), ("content",)]
    assert browser.closed is True


def test_fetch_html_playwright_keeps_final_challenge_body_check(monkeypatch):
    page = _FakePage(html="<html><body>Please wait.</body></html>")
    browser = _FakeBrowser(page)
    fake_playwright = _FakePlaywright(browser)
    monkeypatch.setattr(
        scrape_cf_tutorial,
        "sync_playwright",
        lambda: _FakePlaywrightManager(fake_playwright),
    )

    try:
        scrape_cf_tutorial.fetch_html_playwright("https://codeforces.com/blog/entry/1")
    except scrape_cf_tutorial.ScrapeError as exc:
        assert exc.code == 9
    else:
        raise AssertionError("expected ScrapeError")
