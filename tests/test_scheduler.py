"""Tests for scheduler control behavior."""

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.scheduler.engine import scheduler_loop


def test_scheduler_loop_stops_when_stop_event_is_set():
    seen: list[int] = []

    async def _fake_run(group_id: int, _now) -> None:
        seen.append(group_id)

    async def _run() -> None:
        stop_event = asyncio.Event()
        stop_event.set()
        with patch("kouhai_bot.scheduler.engine.get_config", return_value=SimpleNamespace(current_group=2468)), \
                patch("kouhai_bot.scheduler.engine._run_jobs_for_group", side_effect=_fake_run):
            await scheduler_loop(stop_event=stop_event, tick_seconds=0.01)

    asyncio.run(_run())
    assert seen == [2468]
