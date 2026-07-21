"""Shared Codeforces HTML transport with a browser fallback.

Problem statements and tutorial blogs use the same fetch policy: try the cheap
HTTP client first, then retry with headless Chromium when Codeforces blocks the
request or returns HTML that still contains a client-side loading placeholder.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

import cloudscraper
from requests import exceptions as requests_exceptions

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - playwright is a runtime dependency
    sync_playwright = None
    PlaywrightError = Exception
    PlaywrightTimeoutError = TimeoutError

logger = logging.getLogger("kouhai-bot.cf_fetcher")

CF_PLAYWRIGHT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
CF_PLAYWRIGHT_VIEWPORT = {"width": 1365, "height": 768}
CF_CONTENT_SELECTOR = ".problem-statement, .ttypography"
CF_CONTENT_WAIT_MS = 45_000

_CHALLENGE_RE = re.compile(
    r"browser is being checked|please wait\.|just a moment|cf-mitigated",
    re.I,
)
_TUTORIAL_LOADING_RE = re.compile(r"tutorial\s+is\s+loading(?:\.\.\.)?", re.I)
_SCRAPER = None


class CFFetchError(RuntimeError):
    """A Codeforces fetch failed or returned unusable content."""

    def __init__(self, message: str, *, kind: str = "fetch"):
        super().__init__(message)
        self.kind = kind


def get_scraper():
    """Return the process-wide cloudscraper session."""
    global _SCRAPER
    if _SCRAPER is None:
        _SCRAPER = cloudscraper.create_scraper()
    return _SCRAPER


def content_valid(body: str) -> bool:
    """Return whether a fetched CF document is usable by HTML clients.

    Besides empty and Cloudflare challenge pages, this rejects legacy blog
    pages whose HTTP response only exposes ``Tutorial is loading...`` content.
    Chromium renders those lazy-loaded tutorial fragments before returning.
    """
    if not isinstance(body, str) or not body.strip():
        return False
    if _CHALLENGE_RE.search(body):
        return False
    if _TUTORIAL_LOADING_RE.search(body):
        return False
    return True


def fetch_html_http(url: str, *, timeout: float = 30) -> str:
    """Fetch a Codeforces HTML page with cloudscraper only."""
    try:
        response = get_scraper().get(url, timeout=timeout)
        response.raise_for_status()
    except requests_exceptions.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        kind = "forbidden" if status == 403 else "http"
        raise CFFetchError(
            f"Codeforces HTTP fetch failed for {url}: {exc}",
            kind=kind,
        ) from exc
    except requests_exceptions.Timeout as exc:
        raise CFFetchError(
            f"Codeforces HTTP fetch timed out for {url}: {exc}",
            kind="timeout",
        ) from exc
    except requests_exceptions.ConnectionError as exc:
        raise CFFetchError(
            f"Codeforces HTTP connection failed for {url}: {exc}",
            kind="connection",
        ) from exc

    body = response.text
    if not content_valid(body):
        raise CFFetchError(f"Codeforces returned unusable HTML for {url}", kind="content")
    return body


def fetch_html_playwright(url: str, *, wait_ms: int = 7000) -> str:
    """Fetch a rendered Codeforces HTML page with headless Chromium."""
    if sync_playwright is None:
        raise CFFetchError(
            "Playwright is not installed; install Playwright and Chromium first",
            kind="dependency",
        )

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent=CF_PLAYWRIGHT_USER_AGENT,
                    viewport=CF_PLAYWRIGHT_VIEWPORT,
                )
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                if wait_ms > 0:
                    page.wait_for_timeout(wait_ms)
                try:
                    page.wait_for_selector(CF_CONTENT_SELECTOR, timeout=CF_CONTENT_WAIT_MS)
                except PlaywrightTimeoutError:
                    # Some valid CF pages do not use either common content class.
                    # The final content check below still rejects challenge pages.
                    pass
                body = page.content()
            finally:
                browser.close()
    except PlaywrightTimeoutError as exc:
        raise CFFetchError(
            f"Playwright timed out fetching {url}: {exc}",
            kind="timeout",
        ) from exc
    except PlaywrightError as exc:
        raise CFFetchError(
            f"Playwright failed fetching {url}: {exc}",
            kind="browser",
        ) from exc

    if not content_valid(body):
        raise CFFetchError(
            f"Playwright returned unusable Codeforces HTML for {url}",
            kind="content",
        )
    return body


def fetch_html(
    url: str,
    *,
    fetcher: Literal["auto", "http", "playwright"] = "auto",
    timeout: float = 30,
    pw_wait_ms: int = 7000,
) -> str:
    """Fetch CF HTML using HTTP, Playwright, or automatic fallback."""
    if fetcher == "http":
        return fetch_html_http(url, timeout=timeout)
    if fetcher == "playwright":
        return fetch_html_playwright(url, wait_ms=pw_wait_ms)
    if fetcher != "auto":
        raise ValueError(f"unknown Codeforces fetcher: {fetcher}")

    try:
        body = fetch_html_http(url, timeout=timeout)
        if not content_valid(body):
            raise CFFetchError(
                f"Codeforces returned unusable HTML for {url}",
                kind="content",
            )
        return body
    except CFFetchError as exc:
        if exc.kind not in {"forbidden", "timeout", "connection", "content"}:
            raise
        logger.info(
            "CF HTTP fetch failed (%s); falling back to Playwright for %s",
            exc.kind,
            url,
        )
        return fetch_html_playwright(url, wait_ms=pw_wait_ms)
