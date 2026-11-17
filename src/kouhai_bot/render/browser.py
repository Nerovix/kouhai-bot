"""Persistent headless Chromium for long-image card rendering.

Launches once and reuses the browser; each render gets a fresh context/page
(mobile width, 2x scale for crisp text). Pages are closed after screenshot.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("kouhai-bot.render.browser")

try:
    from playwright.async_api import async_playwright
except Exception:  # pragma: no cover - playwright optional at import time
    async_playwright = None


class RenderBrowser:
    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> None:
        if self._browser is not None:
            return
        if async_playwright is None:
            raise RuntimeError("playwright is not installed; cannot render cards")
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        logger.info("render browser launched (chromium %s)", self._browser.version)

    async def screenshot_html(
        self,
        html: str,
        *,
        width: int = 720,
        device_scale_factor: int = 2,
    ) -> bytes:
        async with self._lock:
            await self._ensure()
            context = await self._browser.new_context(
                viewport={"width": width, "height": 800},
                device_scale_factor=device_scale_factor,
            )
            try:
                page = await context.new_page()
                await page.set_content(html, wait_until="load", timeout=20000)
                await page.wait_for_timeout(150)
                return await page.screenshot(full_page=True, type="png")
            finally:
                await context.close()

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None


_browser: RenderBrowser | None = None


def get_render_browser() -> RenderBrowser:
    global _browser
    if _browser is None:
        _browser = RenderBrowser()
    return _browser


def set_render_browser(browser: RenderBrowser | None) -> None:
    global _browser
    _browser = browser