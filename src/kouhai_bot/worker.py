"""Single-process bot runtime."""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress

from .config import get_config
from .editorial_followup import (
    editorial_prefetch_maintenance_loop,
    ensure_editorial_prefetch,
)
from .friend_requests import doubt_friend_request_loop
from .handlers import process_event
from .napcat.client import NapCatServer
from .problem_prefetch import get_next_problem_prefetcher
from .runtime import bootstrap_runtime, setup_logging
from .scheduler.engine import scheduler_loop

logger = logging.getLogger("kouhai-bot.worker")


class WorkerRuntime:
    def __init__(self) -> None:
        self.cfg = get_config()
        self.napcat = NapCatServer(on_event=self._on_event)
        self._shutdown = asyncio.Event()
        self._scheduler_stop = asyncio.Event()
        self._friend_request_stop = asyncio.Event()
        self._problem_prefetch_stop = asyncio.Event()
        self._editorial_prefetch_stop = asyncio.Event()
        self._scheduler_task: asyncio.Task | None = None
        self._friend_request_task: asyncio.Task | None = None
        self._problem_prefetch_task: asyncio.Task | None = None
        self._editorial_prefetch_task: asyncio.Task | None = None
        self._problem_prefetcher = get_next_problem_prefetcher(self.cfg.current_group)
        self._problem_prefetcher.set_ready_observer(ensure_editorial_prefetch)

    async def run(self) -> None:
        bootstrap_runtime()
        await self.napcat.start()
        self._scheduler_task = asyncio.create_task(
            scheduler_loop(stop_event=self._scheduler_stop),
            name="worker_scheduler",
        )
        self._friend_request_task = asyncio.create_task(
            doubt_friend_request_loop(stop_event=self._friend_request_stop),
            name="worker_doubt_friend_requests",
        )
        self._problem_prefetch_task = asyncio.create_task(
            self._problem_prefetcher.run(stop_event=self._problem_prefetch_stop),
            name="worker_next_problem_prefetch",
        )
        self._editorial_prefetch_task = asyncio.create_task(
            editorial_prefetch_maintenance_loop(
                self.cfg.current_group,
                get_next_problem_pid=self._problem_prefetcher.peek_pid,
                stop_event=self._editorial_prefetch_stop,
            ),
            name="worker_editorial_prefetch_maintenance",
        )
        self._install_signal_handlers()
        logger.info("Worker runtime is running. Press Ctrl+C to stop.")
        try:
            await self._shutdown.wait()
        finally:
            await self.stop()

    async def stop(self) -> None:
        self._shutdown.set()
        self._scheduler_stop.set()
        self._friend_request_stop.set()
        self._problem_prefetch_stop.set()
        self._editorial_prefetch_stop.set()
        await self.napcat.stop()
        await self._problem_prefetcher.shutdown()
        if self._scheduler_task is not None:
            with suppress(asyncio.CancelledError):
                await self._scheduler_task
        if self._friend_request_task is not None:
            with suppress(asyncio.CancelledError):
                await self._friend_request_task
        if self._problem_prefetch_task is not None:
            with suppress(asyncio.CancelledError):
                await self._problem_prefetch_task
        if self._editorial_prefetch_task is not None:
            with suppress(asyncio.CancelledError):
                await self._editorial_prefetch_task
        await self._wait_for_background_tasks()

    async def _on_event(self, event: dict) -> None:
        await process_event(event, spawn_handlers=True)

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._shutdown.set)

    async def _wait_for_background_tasks(self, timeout_sec: float = 30.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_sec
        current = asyncio.current_task()
        while True:
            pending = [
                task
                for task in asyncio.all_tasks()
                if task is not current
                and task is not self._scheduler_task
                and task is not self._friend_request_task
                and task is not self._problem_prefetch_task
                and task is not self._editorial_prefetch_task
                and not task.done()
            ]
            if not pending:
                return
            if asyncio.get_running_loop().time() >= deadline:
                logger.warning(
                    "worker exiting with %s background task(s) still pending",
                    len(pending),
                )
                return
            await asyncio.sleep(0.2)


async def main_async() -> None:
    setup_logging()
    cfg = get_config()
    logger.info("Bot QQ: %s", cfg.bot_qq)
    logger.info("Current group: %s", cfg.current_group)
    runtime = WorkerRuntime()
    await runtime.run()


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
