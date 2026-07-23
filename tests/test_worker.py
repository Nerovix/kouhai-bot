"""Worker lifecycle coverage for the next-problem prefetch loop."""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot import worker


def test_worker_owns_next_problem_prefetch_lifecycle(monkeypatch):
    calls: list[str] = []
    prefetch_started = asyncio.Event()

    class FakeNapCat:
        def __init__(self, *, on_event):
            self.on_event = on_event

        async def start(self):
            calls.append("napcat_start")

        async def stop(self):
            calls.append("napcat_stop")

    async def wait_for_stop(*, stop_event):
        await stop_event.wait()

    async def run_prefetch(*, stop_event):
        calls.append("prefetch_start")
        prefetch_started.set()
        await stop_event.wait()
        calls.append("prefetch_stop")

    prefetcher = SimpleNamespace(
        run=run_prefetch,
        shutdown=AsyncMock(),
    )

    monkeypatch.setattr(
        worker,
        "get_config",
        lambda: SimpleNamespace(current_group=123),
    )
    monkeypatch.setattr(worker, "NapCatServer", FakeNapCat)
    monkeypatch.setattr(worker, "bootstrap_runtime", lambda: calls.append("bootstrap"))
    monkeypatch.setattr(worker, "scheduler_loop", wait_for_stop)
    monkeypatch.setattr(worker, "doubt_friend_request_loop", wait_for_stop)
    monkeypatch.setattr(
        worker,
        "get_next_problem_prefetcher",
        lambda group_id: prefetcher if group_id == 123 else None,
    )

    async def run():
        runtime = worker.WorkerRuntime()
        monkeypatch.setattr(runtime, "_install_signal_handlers", lambda: None)
        monkeypatch.setattr(runtime, "_wait_for_background_tasks", AsyncMock())
        task = asyncio.create_task(runtime.run())
        await asyncio.wait_for(prefetch_started.wait(), timeout=1)
        runtime._shutdown.set()
        await asyncio.wait_for(task, timeout=1)
        return runtime

    runtime = asyncio.run(run())

    assert calls == [
        "bootstrap",
        "napcat_start",
        "prefetch_start",
        "napcat_stop",
        "prefetch_stop",
    ]
    assert runtime._scheduler_stop.is_set()
    assert runtime._friend_request_stop.is_set()
    assert runtime._problem_prefetch_stop.is_set()
    prefetcher.shutdown.assert_awaited_once()
