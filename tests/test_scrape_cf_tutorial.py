"""Tests for tutorial wrappers around the shared Codeforces transport."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import scrape_cf_tutorial


@pytest.mark.parametrize("fetcher", ["http", "playwright"])
def test_fetch_html_delegates_transport_to_shared_fetcher(monkeypatch, fetcher):
    calls = []

    def fake_fetch(url, *, fetcher, pw_wait_ms):
        calls.append((url, fetcher, pw_wait_ms))
        return "<html>ok</html>"

    monkeypatch.setattr(scrape_cf_tutorial.cf_fetcher, "fetch_html", fake_fetch)

    html = scrape_cf_tutorial.fetch_html(
        "https://codeforces.com/blog/entry/1",
        fetcher=fetcher,
        pw_wait_ms=123,
    )

    assert html == "<html>ok</html>"
    assert calls == [("https://codeforces.com/blog/entry/1", fetcher, 123)]


def test_fetch_html_http_delegates_to_shared_http_mode(monkeypatch):
    calls = []

    def fake_fetch(url, *, fetcher, pw_wait_ms):
        calls.append((url, fetcher, pw_wait_ms))
        return "<html>http</html>"

    monkeypatch.setattr(scrape_cf_tutorial.cf_fetcher, "fetch_html", fake_fetch)

    assert (
        scrape_cf_tutorial.fetch_html_http("https://codeforces.com/blog/entry/2")
        == "<html>http</html>"
    )
    assert calls == [("https://codeforces.com/blog/entry/2", "http", 7000)]


def test_fetch_html_playwright_delegates_with_wait(monkeypatch):
    calls = []

    def fake_fetch(url, *, fetcher, pw_wait_ms):
        calls.append((url, fetcher, pw_wait_ms))
        return "<html>browser</html>"

    monkeypatch.setattr(scrape_cf_tutorial.cf_fetcher, "fetch_html", fake_fetch)

    html = scrape_cf_tutorial.fetch_html_playwright(
        "https://codeforces.com/problemset/problem/601/D",
        wait_ms=321,
    )

    assert html == "<html>browser</html>"
    assert calls == [
        ("https://codeforces.com/problemset/problem/601/D", "playwright", 321)
    ]


def test_fetch_html_preserves_scrape_error_contract(monkeypatch):
    def fake_fetch(url, *, fetcher, pw_wait_ms):
        raise scrape_cf_tutorial.cf_fetcher.CFFetchError(
            "placeholder body",
            kind="content",
        )

    monkeypatch.setattr(scrape_cf_tutorial.cf_fetcher, "fetch_html", fake_fetch)

    with pytest.raises(scrape_cf_tutorial.ScrapeError) as exc_info:
        scrape_cf_tutorial.fetch_html("https://codeforces.com/blog/entry/3")

    assert exc_info.value.code == 9


def test_http_mode_falls_back_to_m1_mirror_after_primary_403(monkeypatch):
    calls = []

    def fake_fetch(url, *, fetcher, pw_wait_ms):
        calls.append((url, fetcher, pw_wait_ms))
        if url.startswith("https://codeforces.com/"):
            raise scrape_cf_tutorial.cf_fetcher.CFFetchError(
                "403 response",
                kind="forbidden",
            )
        return "<html><div class='ttypography'>mirror editorial</div></html>"

    monkeypatch.setattr(scrape_cf_tutorial.cf_fetcher, "fetch_html", fake_fetch)

    result = scrape_cf_tutorial.fetch_html(
        "https://codeforces.com/blog/entry/123?locale=en",
        fetcher="http",
    )

    assert result == "<html><div class='ttypography'>mirror editorial</div></html>"
    assert calls == [
        ("https://codeforces.com/blog/entry/123?locale=en", "http", 7000),
        ("https://m1.codeforces.com/blog/entry/123?locale=en", "http", 7000),
    ]


def test_auto_mode_uses_m1_mirror_before_playwright_after_primary_403(monkeypatch):
    calls = []

    def fake_fetch(url, *, fetcher, pw_wait_ms):
        calls.append((url, fetcher, pw_wait_ms))
        if fetcher == "http":
            raise scrape_cf_tutorial.cf_fetcher.CFFetchError(
                "403 response",
                kind="forbidden",
            )
        return "<html><div class='ttypography'>browser editorial</div></html>"

    monkeypatch.setattr(scrape_cf_tutorial.cf_fetcher, "fetch_html", fake_fetch)

    result = scrape_cf_tutorial.fetch_html(
        "https://codeforces.com/blog/entry/123?locale=en",
        fetcher="auto",
    )

    assert result == "<html><div class='ttypography'>browser editorial</div></html>"
    assert calls == [
        ("https://codeforces.com/blog/entry/123?locale=en", "http", 7000),
        ("https://m1.codeforces.com/blog/entry/123?locale=en", "http", 7000),
        ("https://codeforces.com/blog/entry/123?locale=en", "playwright", 7000),
    ]
