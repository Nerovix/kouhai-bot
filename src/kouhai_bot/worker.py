"""Single-process official-API bot runtime (multi-group).

One outbound WebSocket gateway feeds normalized group events. Each group the
bot hears from gets its own next-problem prefetch slot and editorial
maintenance loop (group = view), started lazily on first sight. A shared
scheduler runs per-group jobs; the SQLite database and official sender are
process-wide runtime handles.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress

from .config import get_config
from .db.database import get_database, set_database
from .editorial_followup import (
    editorial_prefetch_maintenance_loop,
    ensure_editorial_prefetch,
)
from .handlers import process_event
from .handlers.runtime import Runtime, set_runtime
from .official.budget import ReplyBudget
from .official.gateway import OfficialGateway
from .official.rest import OfficialRestClient
from .official.sender import OfficialSender
from .official.tokens import AccessTokenManager
from .problem_prefetch import get_next_problem_prefetcher
from .render.avatars import get_avatar_cache
from .render.browser import get_render_browser
from .runtime import bootstrap_runtime, setup_logging
from .scheduler.engine import scheduler_loop

logger = logging.getLogger("kouhai-bot.worker")


class GroupServices:
    """Per-group background preparation (prefetch slot + editorial maintenance)."""

    def __init__(self, group_openid: str) -> None:
        self.group_openid = group_openid
        self._stop = asyncio.Event()
        self._prefetcher = get_next_problem_prefetcher(group_openid)
        self._prefetcher.set_ready_observer(ensure_editorial_prefetch)
        self._prefetch_task = asyncio.create_task(
            self._prefetcher.run(stop_event=self._stop),
            name=f"next_problem_prefetch_{group_openid}",
        )
        self._editorial_task = asyncio.create_task(
            editorial_prefetch_maintenance_loop(
                group_openid,
                get_next_problem_pid=self._prefetcher.peek_pid,
                stop_event=self._stop,
            ),
            name=f"editorial_prefetch_{group_openid}",
        )

    async def stop(self) -> None:
        self._stop.set()
        await self._prefetcher.shutdown()
        for task in (self._prefetch_task, self._editorial_task):
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task


class WorkerRuntime:
    def __init__(self) -> None:
        self.cfg = get_config()
        self.cfg.validate_official()
        self._shutdown = asyncio.Event()
        self._gateway_stop = asyncio.Event()
        self._scheduler_stop = asyncio.Event()
        self._groups: dict[str, GroupServices] = {}
        self._gateway_task: asyncio.Task | None = None
        self._scheduler_task: asyncio.Task | None = None
        self._db = get_database()
        self._tokens = AccessTokenManager(
            self.cfg.app_id, self.cfg.app_secret, token_base=self.cfg.official_token_base,
        )
        self._rest = OfficialRestClient(self._tokens, api_base=self.cfg.official_api_base)
        self._sender = OfficialSender(
            self._rest, ReplyBudget(self._db), bot_name=self.cfg.official_bot_name,
        )
        self._browser = get_render_browser()
        self._gateway = OfficialGateway(
            self._rest, self._tokens,
            on_event=self._on_event, intents=self.cfg.official_intents,
        )

    async def run(self) -> None:
        bootstrap_runtime()
        await self._db.open()
        set_database(self._db)
        set_runtime(Runtime(db=self._db, sender=self._sender, browser=self._browser))
        self._gateway_task = asyncio.create_task(
            self._gateway.run(self._gateway_stop), name="worker_gateway",
        )
        self._scheduler_task = asyncio.create_task(
            scheduler_loop(get_groups=self._active_groups, stop_event=self._scheduler_stop),
            name="worker_scheduler",
        )
        self._install_signal_handlers()
        logger.info("Official worker running (app_id=%s). Press Ctrl+C to stop.", self.cfg.app_id)
        try:
            await self._shutdown.wait()
        finally:
            await self.stop()

    async def _active_groups(self) -> list[str]:
        return list(self._groups)

    def _ensure_group(self, group_openid: str) -> None:
        if group_openid and group_openid not in self._groups:
            logger.info("[group_%s] discovered; starting group services", group_openid)
            self._groups[group_openid] = GroupServices(group_openid)

    async def _on_event(self, event: dict) -> None:
        if event.get("type") == "message" and event.get("message_type") == "group":
            self._ensure_group(str(event.get("group_openid", "") or ""))
        await process_event(event, spawn_handlers=True)

    async def stop(self) -> None:
        self._shutdown.set()
        self._gateway_stop.set()
        self._scheduler_stop.set()
        for services in self._groups.values():
            await services.stop()
        if self._gateway_task is not None:
            self._gateway_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._gateway_task
        if self._scheduler_task is not None:
            with suppress(asyncio.CancelledError, Exception):
                await self._scheduler_task
        await self._wait_for_background_tasks()
        await self._browser.close()
        await get_avatar_cache().close()
        await self._rest.close()
        await self._tokens.close()
        await self._db.close()
        set_runtime(None)

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._shutdown.set)

    async def _wait_for_background_tasks(self, timeout_sec: float = 30.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_sec
        current = asyncio.current_task()
        owned = {self._gateway_task, self._scheduler_task}
        while True:
            pending = [
                task for task in asyncio.all_tasks()
                if task is not current and task not in owned and not task.done()
            ]
            if not pending:
                return
            if asyncio.get_running_loop().time() >= deadline:
                logger.warning(
                    "worker exiting with %s background task(s) still pending", len(pending),
                )
                return
            await asyncio.sleep(0.2)


async def main_async() -> None:
    setup_logging()
    cfg = get_config()
    logger.info("Official app_id: %s", cfg.app_id)
    runtime = WorkerRuntime()
    await runtime.run()


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()