"""Tests for low-level Codeforces tutorial scraping helpers."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import scrape_cf_tutorial


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
