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


def _mathjax_formula(tex: str, rendered: str, element_id: int) -> str:
    """One MathJax v2 formula exactly as served on rendered CF blog pages."""
    return (
        '<span class="MathJax_Preview" style="color: inherit;"></span>'
        f'<span class="MathJax" id="MathJax-Element-{element_id}-Frame" tabindex="0" '
        f'data-mathml="&lt;math&gt;&lt;mn&gt;{rendered}&lt;/mn&gt;&lt;/math&gt;" '
        'role="presentation" style="position: relative;">'
        '<nobr aria-hidden="true">'
        f'<span class="math" id="MathJax-Span-{element_id}">'
        f'<span class="mrow"><span class="mn" style="font-family: MathJax_Main;">{rendered}</span></span>'
        "</span></nobr>"
        f'<span class="MJX_Assistive_MathML" role="presentation">'
        f'<math xmlns="http://www.w3.org/1998/Math/MathML"><mn>{rendered}</mn></math></span>'
        "</span>"
        f'<script type="math/tex" id="MathJax-Element-{element_id}">{tex}</script>'
    )


def test_html_to_markdownish_does_not_duplicate_mathjax_digits():
    """Rendered blog pages carry each formula twice (nobr + assistive MathML)
    after the TeX script is dropped; digits must not be duplicated."""
    html = (
        "<p>Since the coin moves down by "
        + _mathjax_formula("1", "1", 1)
        + " each time, but by "
        + _mathjax_formula("2", "2", 2)
        + " in the other case.</p>"
    )
    text = scrape_cf_tutorial.html_to_markdownish(html)
    assert "moves down by 1 each time, but by 2 in the other case" in text
    assert "by 11 each" not in text


def test_html_to_markdownish_plain_markup_unchanged():
    """Unrendered blog pages keep their $$$...$$$ markup as-is (unchanged
    behaviour: the markdownish output intentionally keeps the markup)."""
    html = "<p>cost is $$$10$$$ per item, at most $$$9$$$ times</p>"
    text = scrape_cf_tutorial.html_to_markdownish(html)
    assert text == "cost is $$$10$$$ per item, at most $$$9$$$ times"
