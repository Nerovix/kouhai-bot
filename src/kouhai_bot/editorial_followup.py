"""Background editorial prefetch (on new problem) and delivery (on first AC).

Neither path uses the group coordinator queue.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from .config import get_config
from .napcat.client import (
    build_plain_message,
    send_group_forward_msg,
    send_private_msg,
)
from .tutorials import (
    MIN_EDITORIAL_LEN,
    OfficialEditorial,
    get_editorial_zh_for_group,
    get_official_editorial,
    has_cached_editorial_zh,
    is_no_official_editorial,
    load_cached_editorial_zh,
    prefetch_editorial_zh,
)

logger = logging.getLogger("kouhai-bot.editorial_followup")

_TUTORIAL_FORWARD_CHUNK_SIZE = 5000
_PREFETCH_WAIT_TIMEOUT_SEC = 600

_background_tasks: set[asyncio.Task] = set()
_prefetch_tasks: dict[str, asyncio.Task] = {}


def _track_task(task: asyncio.Task) -> None:
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _chunk_text(text: str, chunk_size: int) -> list[str]:
    if chunk_size <= 0:
        return [text]
    return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)] or [""]


def _prefetch_needed(pid: str, *, run_agent: bool) -> bool:
    if has_cached_editorial_zh(pid):
        return False
    if (
        is_no_official_editorial(pid)
        and not run_agent
        and get_official_editorial(pid) is None
    ):
        return False
    return True


def schedule_prefetch_editorial(pid: str, *, run_agent: bool = True) -> None:
    """Start translating editorial when a new problem is set."""
    pid = (pid or "").strip()
    if not pid or not _prefetch_needed(pid, run_agent=run_agent):
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
    """Keep full editorial prefetch active for an unpublished READY problem.

    This entry point is safe to call from a maintenance loop: an in-flight task
    is deduplicated by ``schedule_prefetch_editorial``, while a verified cache
    or a confirmed no-editorial marker is treated as terminal.  If a task dies
    before reaching either terminal state, the next maintenance pass retries
    the complete crawler + translation pipeline.
    """
    pid = (pid or "").strip()
    if (
        not pid
        or has_cached_editorial_zh(pid)
        or is_no_official_editorial(pid)
    ):
        return
    schedule_prefetch_editorial(pid, run_agent=True)


def schedule_prefetch_for_group_today(group_id: int) -> None:
    """Resume full editorial prefetch for the current problem on bot startup."""
    state_path = os.path.join(get_config().data_dir, "groups", str(group_id), "state.json")
    if not os.path.isfile(state_path):
        return
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    pid = str(state.get("today", "") or "").strip()
    if pid:
        ensure_editorial_prefetch(pid)


def schedule_prefetch_for_current_group() -> None:
    cfg = get_config()
    if cfg.current_group:
        schedule_prefetch_for_group_today(cfg.current_group)


async def _run_prefetch_editorial(pid: str, *, run_agent: bool) -> None:
    started = time.monotonic()
    try:
        await prefetch_editorial_zh(pid, run_agent=run_agent)
        elapsed = time.monotonic() - started
        if has_cached_editorial_zh(pid):
            logger.info("editorial prefetch ready for %s in %.1fs (cached)", pid, elapsed)
        elif is_no_official_editorial(pid):
            logger.info("editorial prefetch: no editorial for %s (%.1fs)", pid, elapsed)
        else:
            logger.warning(
                "editorial prefetch finished for %s in %.1fs without cache",
                pid,
                elapsed,
            )
    except Exception as e:
        logger.warning(
            "editorial prefetch failed for %s: %s",
            pid,
            e,
            exc_info=True,
        )


async def _await_prefetch_if_running(pid: str) -> None:
    task = _prefetch_tasks.get(pid)
    if task is None or task.done():
        return
    started = time.monotonic()
    logger.info("editorial delivery waiting for in-flight prefetch of %s", pid)
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=_PREFETCH_WAIT_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        logger.warning("editorial prefetch still running for %s after AC", pid)
    except Exception as e:
        logger.warning("editorial prefetch await failed for %s: %s", pid, e)
    else:
        logger.info(
            "editorial prefetch wait for %s done in %.1fs",
            pid,
            time.monotonic() - started,
        )


def schedule_post_solve_editorial_followup(group_id: int, pid: str) -> None:
    """Fire-and-forget deliver on first AC; translation should already be cached."""
    task = asyncio.create_task(
        run_post_solve_editorial_followup(group_id, pid),
        name=f"editorial_deliver_{group_id}_{pid}",
    )
    _track_task(task)


async def run_post_solve_editorial_followup(group_id: int, pid: str) -> None:
    started = time.monotonic()
    try:
        if has_cached_editorial_zh(pid):
            editorial = get_official_editorial(pid)
            if editorial:
                await deliver_official_tutorial_forward(group_id, pid, editorial)
                logger.info(
                    "[group_%s] editorial delivered from cache for %s in %.1fs",
                    group_id,
                    pid,
                    time.monotonic() - started,
                )
                return
            if is_no_official_editorial(pid):
                logger.info(
                    "[group_%s] no official editorial for %s, skipping delivery",
                    group_id,
                    pid,
                )
                return

        await _await_prefetch_if_running(pid)
        if is_no_official_editorial(pid):
            logger.info(
                "[group_%s] no official editorial for %s, skipping delivery",
                group_id,
                pid,
            )
            return
        editorial = get_official_editorial(pid)
        if not editorial:
            logger.info(
                "[group_%s] editorial for %s remains incomplete, skipping delivery",
                group_id,
                pid,
            )
            return
        await deliver_official_tutorial_forward(group_id, pid, editorial)
        logger.info(
            "[group_%s] editorial delivered for %s in %.1fs",
            group_id,
            pid,
            time.monotonic() - started,
        )
    except Exception as e:
        logger.warning(
            "[group_%s] post-solve editorial delivery failed for %s: %s",
            group_id,
            pid,
            e,
            exc_info=True,
        )


async def deliver_official_tutorial_forward(
    group_id: int,
    pid: str,
    editorial: OfficialEditorial,
) -> None:
    """Send cached Chinese editorial; translate only if prefetch did not finish."""
    zh_text = load_cached_editorial_zh(pid)
    if len(zh_text) < MIN_EDITORIAL_LEN:
        logger.info(
            "[group_%s] editorial cache miss for %s, translating on delivery",
            group_id,
            pid,
        )
        zh_text, _model_tag = await get_editorial_zh_for_group(editorial, pid)
        zh_text = zh_text or ""
    if len(zh_text) < MIN_EDITORIAL_LEN:
        logger.warning(
            "[group_%s] official tutorial translation unavailable for %s",
            group_id,
            pid,
        )
        return

    cfg = get_config()
    header = f"📖 {pid} 官方题解"
    if editorial.tutorial_url:
        header = f"{header}\n来源: {editorial.tutorial_url}"
    payload = f"{header}\n\n{zh_text}"
    chunks = _chunk_text(payload, _TUTORIAL_FORWARD_CHUNK_SIZE)
    node_ids: list[str] = []
    for chunk in chunks:
        self_resp = await send_private_msg(cfg.bot_qq, build_plain_message(chunk))
        if not self_resp:
            node_ids = []
            break
        node_ids.append(str(self_resp))
    if not node_ids:
        logger.warning(
            "[group_%s] failed to self-send official tutorial for %s",
            group_id,
            pid,
        )
        return
    await asyncio.sleep(0.5)
    fwd_resp = await send_group_forward_msg(
        group_id,
        [{"type": "node", "data": {"id": node_id}} for node_id in node_ids],
    )
    if not fwd_resp:
        logger.warning(
            "[group_%s] failed to forward official tutorial for %s",
            group_id,
            pid,
        )
