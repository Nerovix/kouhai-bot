"""Background editorial prefetch (on new problem) and delivery (on first AC).

Group-only, DB-backed. Neither path uses the group coordinator queue. Delivery
renders the cached Chinese editorial as a long image (official API has no
merged-forward primitive); plain text is the fallback.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress

from .db.database import get_database
from .db.repos import posts
from .handlers.runtime import get_runtime
from .render.figures import (
    figure_data_uris,
    figure_rows_html,
    strip_figure_markers,
    split_figure_markers,
)
from .render.forward_card import send_forward_card
from .render.html import text_to_html
from .tutorials import (
    MIN_EDITORIAL_LEN,
    get_verified_official_editorial,
    has_cached_editorial_zh,
    is_no_official_editorial,
    load_cached_editorial_zh,
    prefetch_editorial_zh,
)

logger = logging.getLogger("kouhai-bot.editorial_followup")

_TUTORIAL_CARD_CHUNK_SIZE = 5000
_PREFETCH_WAIT_TIMEOUT_SEC = 600
_PREFETCH_BACKOFF_BASE_SEC = 60.0
_PREFETCH_BACKOFF_MAX_SEC = 6 * 3600.0

_background_tasks: set[asyncio.Task] = set()
_prefetch_tasks: dict[str, asyncio.Task] = {}
_prefetch_attempts: dict[str, int] = {}
_prefetch_last_attempt_at: dict[str, float] = {}


def _prefetch_backoff_remaining(pid: str) -> float:
    attempts = _prefetch_attempts.get(pid, 0)
    if attempts <= 0:
        return 0.0
    delay = min(
        _PREFETCH_BACKOFF_BASE_SEC * (2 ** (attempts - 1)),
        _PREFETCH_BACKOFF_MAX_SEC,
    )
    elapsed = time.monotonic() - _prefetch_last_attempt_at.get(pid, 0.0)
    return max(0.0, delay - elapsed)


def _mark_prefetch_failed(pid: str) -> None:
    _prefetch_attempts[pid] = _prefetch_attempts.get(pid, 0) + 1
    _prefetch_last_attempt_at[pid] = time.monotonic()


def _mark_prefetch_succeeded(pid: str) -> None:
    _prefetch_attempts.pop(pid, None)
    _prefetch_last_attempt_at.pop(pid, None)


def _track_task(task: asyncio.Task) -> None:
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def cancel_background_tasks() -> None:
    """Cancel and drain all tracked editorial prefetch/delivery tasks.

    Called during worker shutdown before closing the browser/DB/REST handles,
    so a long crawl+translation cannot outlive the resources it writes through.
    """
    tasks = list(_background_tasks)
    for task in tasks:
        task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError, Exception):
            await task
    _background_tasks.clear()
    _prefetch_tasks.clear()


def _chunk_text(text: str, chunk_size: int) -> list[str]:
    if chunk_size <= 0:
        return [text]
    return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)] or [""]


async def _prefetch_needed(pid: str) -> bool:
    if await has_cached_editorial_zh(pid):
        return False
    if await is_no_official_editorial(pid):
        return False
    return True


def schedule_prefetch_editorial(pid: str, *, run_agent: bool = True) -> None:
    pid = (pid or "").strip()
    if not pid:
        return
    if _prefetch_backoff_remaining(pid) > 0:
        return
    existing = _prefetch_tasks.get(pid)
    if existing is not None and not existing.done():
        return
    logger.info("editorial prefetch scheduled for %s", pid)
    task = asyncio.create_task(
        _run_prefetch_editorial(pid, run_agent=run_agent),
        name=f"editorial_prefetch_{pid}",
    )
    _prefetch_tasks[pid] = task
    _track_task(task)

    def _drop(done: asyncio.Task) -> None:
        if _prefetch_tasks.get(pid) is done:
            _prefetch_tasks.pop(pid, None)

    task.add_done_callback(_drop)


def ensure_editorial_prefetch(pid: str) -> None:
    """Idempotent maintenance entry: dedup in-flight, terminal states stop retries."""
    pid = (pid or "").strip()
    if not pid:
        return
    schedule_prefetch_editorial(pid, run_agent=True)


async def _current_pid(group_openid: str) -> str:
    post = await posts.get_current_post(get_database(), group_openid)
    return str(post["pid"]) if post else ""


async def editorial_prefetch_maintenance_loop(
    group_openid: str,
    *,
    get_next_problem_pid: Callable[[], Awaitable[str]],
    stop_event: asyncio.Event,
    interval_seconds: float = 60.0,
) -> None:
    """Continuously maintain editorial work for both current and READY problems."""
    logger.info("[group_%s] editorial prefetch maintenance started", group_openid)
    while not stop_event.is_set():
        try:
            current_pid = await _current_pid(group_openid)
            next_pid = (await get_next_problem_pid()).strip()
            for pid in dict.fromkeys([current_pid, next_pid]):
                if pid:
                    ensure_editorial_prefetch(pid)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[group_%s] editorial prefetch maintenance failed: %s",
                group_openid, exc, exc_info=True,
            )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            continue
    logger.info("[group_%s] editorial prefetch maintenance stopped", group_openid)


async def _run_prefetch_editorial(pid: str, *, run_agent: bool) -> None:
    started = time.monotonic()
    try:
        if not await _prefetch_needed(pid):
            _mark_prefetch_succeeded(pid)
            return
        await prefetch_editorial_zh(pid, run_agent=run_agent)
        elapsed = time.monotonic() - started
        if await has_cached_editorial_zh(pid):
            _mark_prefetch_succeeded(pid)
            logger.info("editorial prefetch ready for %s in %.1fs", pid, elapsed)
        elif await is_no_official_editorial(pid):
            _mark_prefetch_succeeded(pid)
            logger.info("editorial prefetch: no editorial for %s (%.1fs)", pid, elapsed)
        else:
            _mark_prefetch_failed(pid)
            logger.warning(
                "editorial prefetch finished for %s in %.1fs without cache", pid, elapsed
            )
    except Exception as e:
        _mark_prefetch_failed(pid)
        logger.warning("editorial prefetch failed for %s: %s", pid, e, exc_info=True)


async def _await_prefetch_if_running(pid: str) -> None:
    task = _prefetch_tasks.get(pid)
    if task is None or task.done():
        return
    logger.info("editorial delivery waiting for in-flight prefetch of %s", pid)
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=_PREFETCH_WAIT_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        logger.warning("editorial prefetch still running for %s after AC", pid)
    except Exception as e:
        logger.warning("editorial prefetch await failed for %s: %s", pid, e)


def schedule_post_solve_editorial_followup(group_openid: str, pid: str) -> None:
    """Fire-and-forget deliver on first AC; translation should already be cached."""
    task = asyncio.create_task(
        run_post_solve_editorial_followup(group_openid, pid),
        name=f"editorial_deliver_{group_openid}_{pid}",
    )
    _track_task(task)


async def run_post_solve_editorial_followup(group_openid: str, pid: str) -> None:
    started = time.monotonic()
    try:
        editorial = await get_verified_official_editorial(pid)
        if editorial:
            if await deliver_official_tutorial(group_openid, pid):
                logger.info(
                    "[group_%s] editorial delivered from cache for %s in %.1fs",
                    group_openid, pid, time.monotonic() - started,
                )
            return
        if await is_no_official_editorial(pid):
            return

        await _await_prefetch_if_running(pid)
        if await is_no_official_editorial(pid):
            return
        editorial = await get_verified_official_editorial(pid)
        if not editorial:
            logger.info(
                "[group_%s] editorial for %s remains incomplete, skipping delivery",
                group_openid, pid,
            )
            return
        if await deliver_official_tutorial(group_openid, pid):
            logger.info(
                "[group_%s] editorial delivered for %s in %.1fs",
                group_openid, pid, time.monotonic() - started,
            )
    except Exception as e:
        logger.warning(
            "[group_%s] post-solve editorial delivery failed for %s: %s",
            group_openid, pid, e, exc_info=True,
        )


async def _editorial_payload(pid: str) -> str | None:
    zh_text = await load_cached_editorial_zh(pid)
    if len(zh_text) < MIN_EDITORIAL_LEN:
        return None
    header = f"📖 {pid} 官方题解"
    return f"{header}\n\n{zh_text}"


def _text_nodes(text: str, bot_name: str) -> list[dict]:
    return [
        {"nickname": bot_name, "content_html": text_to_html(chunk), "content_text": chunk}
        for chunk in _chunk_text(text, _TUTORIAL_CARD_CHUNK_SIZE)
        if chunk.strip()
    ]


def _inline_figure_nodes(text: str, figure_data: list[tuple[int, str]], bot_name: str) -> list[dict]:
    """Interleave editorial figures with the prose that refers to them."""
    available = dict(figure_data)
    captions = {number: f"图 {number}" for number, _uri in figure_data}
    nodes: list[dict] = []
    pending: list[tuple[int, str]] = []

    def flush_figures() -> None:
        if not pending:
            return
        nodes.append({
            "nickname": bot_name,
            "content_html": figure_rows_html(list(pending), captions),
            "content_text": "、".join(f"图 {number}" for number, _uri in pending),
        })
        pending.clear()

    for kind, value in split_figure_markers(text):
        if kind == "text":
            prose = value.strip()
            if prose:
                flush_figures()
                nodes.extend(_text_nodes(prose, bot_name))
            continue
        number = int(value)
        if number in available:
            pending.append((number, available[number]))
    flush_figures()
    return nodes


async def deliver_official_tutorial(group_openid: str, pid: str) -> bool:
    """Deliver one verified, cached Chinese editorial as a long-image card."""
    payload = await _editorial_payload(pid)
    if payload is None:
        logger.warning(
            "[group_%s] tutorial delivery skipped for %s (missing translation)",
            group_openid, pid,
        )
        return False
    rt = get_runtime()
    bot_name = rt.sender.bot_name or "KouhaiBot"
    editorial = await get_verified_official_editorial(pid)
    figure_data = await figure_data_uris(editorial.images if editorial else [])
    marked = any(kind == "figure" for kind, _value in split_figure_markers(payload))
    if marked and figure_data:
        nodes = _inline_figure_nodes(payload, figure_data, bot_name)
        payload = strip_figure_markers(payload)
    else:
        nodes = _text_nodes(payload, bot_name)
        if marked:
            payload = strip_figure_markers(payload)
        if figure_data:
            captions = {number: f"图 {number}" for number, _uri in figure_data}
            nodes.append({
                "nickname": bot_name,
                "content_html": "<h3>题解配图</h3>" + figure_rows_html(figure_data, captions),
                "content_text": f"题解配图（{len(figure_data)} 张，见题解长图）",
            })
            payload = f"{payload}\n\n（本题解含 {len(figure_data)} 张配图，请在题解长图中查看）"
    mid = await send_forward_card(
        rt.sender,
        group_openid,
        nodes,
        title=f"📖 {pid} 官方题解",
        msg_id=None,
        text_fallback=payload,
        browser=rt.browser,
    )
    return bool(mid)