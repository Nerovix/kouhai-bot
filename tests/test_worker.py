"""Official-worker lifecycle coverage: lazy per-group services + runtime.

No real gateway/WS/HTTP: every transport and background-loop dependency is
replaced with deterministic fakes. Group identity is an opaque openid string.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kouhai_bot import worker

APP_ID = "1900000000"


def _fake_cfg(data_dir: str, *, validate_error: Exception | None = None):
    def _validate() -> None:
        if validate_error is not None:
            raise validate_error

    return SimpleNamespace(
        app_id=APP_ID,
        app_secret="secret",
        official_token_base="https://token.example.test",
        official_api_base="https://api.example.test",
        official_bot_name="助手",
        official_intents=(1 << 25) | (1 << 26),
        data_dir=data_dir,
        validate_official=_validate,
    )


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Patch every WorkerRuntime/GroupServices dependency with fakes."""
    calls: list[str] = []
    prefetchers: dict[str, SimpleNamespace] = {}
    prefetch_started: dict[str, asyncio.Event] = {}
    gateways: list[SimpleNamespace] = []
    gateway_started = asyncio.Event()
    scheduler_state: dict = {}

    def _prefetcher_factory(group_openid: str):
        started = asyncio.Event()
        prefetch_started[group_openid] = started

        async def _run(*, stop_event):
            calls.append(f"prefetch_start:{group_openid}")
            started.set()
            await stop_event.wait()
            calls.append(f"prefetch_stop:{group_openid}")

        async def _shutdown():
            calls.append(f"prefetch_shutdown:{group_openid}")
            # Let the stop_event waiters resume before the tasks are cancelled.
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        prefetcher = SimpleNamespace(
            group_openid=group_openid,
            run=_run,
            peek_pid=AsyncMock(return_value="542D"),
            shutdown=_shutdown,
            set_ready_observer=MagicMock(),
        )
        prefetchers[group_openid] = prefetcher
        return prefetcher

    prefetcher_factory = MagicMock(side_effect=_prefetcher_factory)

    async def _editorial_loop(
        group_openid, *, get_next_problem_pid, stop_event, interval_seconds=60.0
    ):
        assert await get_next_problem_pid() == "542D"
        calls.append(f"editorial_start:{group_openid}")
        await stop_event.wait()
        calls.append(f"editorial_stop:{group_openid}")

    class _FakeGateway:
        def __init__(self, rest, tokens, *, on_event, intents, shard=(0, 1)):
            self.rest = rest
            self.tokens = tokens
            self.on_event = on_event
            self.intents = intents
            gateways.append(self)

        async def run(self, stop_event):
            calls.append("gateway_start")
            gateway_started.set()
            await stop_event.wait()
            calls.append("gateway_stop")

    async def _scheduler_loop(*, get_groups, stop_event, tick_seconds=60.0):
        scheduler_state["get_groups"] = get_groups
        calls.append("scheduler_start")
        await stop_event.wait()
        calls.append("scheduler_stop")

    fake_db = SimpleNamespace(open=AsyncMock(), close=AsyncMock())
    tokens = SimpleNamespace(close=AsyncMock())
    rest = SimpleNamespace(close=AsyncMock())
    browser = SimpleNamespace(close=AsyncMock())
    sender = SimpleNamespace()
    process_event = AsyncMock()
    set_database = MagicMock()
    set_runtime = MagicMock()

    monkeypatch.setattr(worker, "get_config", lambda: _fake_cfg(str(tmp_path)))
    monkeypatch.setattr(worker, "get_database", lambda: fake_db)
    monkeypatch.setattr(worker, "set_database", set_database)
    monkeypatch.setattr(worker, "set_runtime", set_runtime)
    monkeypatch.setattr(worker, "AccessTokenManager", MagicMock(return_value=tokens))
    monkeypatch.setattr(worker, "OfficialRestClient", MagicMock(return_value=rest))
    monkeypatch.setattr(worker, "OfficialSender", MagicMock(return_value=sender))
    monkeypatch.setattr(worker, "ReplyBudget", MagicMock())
    monkeypatch.setattr(worker, "get_render_browser", lambda: browser)
    monkeypatch.setattr(worker, "OfficialGateway", _FakeGateway)
    monkeypatch.setattr(worker, "scheduler_loop", _scheduler_loop)
    monkeypatch.setattr(worker, "bootstrap_runtime", lambda: calls.append("bootstrap"))
    monkeypatch.setattr(worker, "setup_logging", lambda: calls.append("setup_logging"))
    monkeypatch.setattr(worker, "process_event", process_event)
    monkeypatch.setattr(worker, "get_next_problem_prefetcher", prefetcher_factory)
    monkeypatch.setattr(
        worker, "editorial_prefetch_maintenance_loop", _editorial_loop
    )

    return SimpleNamespace(
        calls=calls,
        prefetchers=prefetchers,
        prefetch_started=prefetch_started,
        prefetcher_factory=prefetcher_factory,
        gateways=gateways,
        gateway_started=gateway_started,
        scheduler_state=scheduler_state,
        fake_db=fake_db,
        tokens=tokens,
        rest=rest,
        browser=browser,
        sender=sender,
        process_event=process_event,
        set_database=set_database,
        set_runtime=set_runtime,
    )


async def test_group_services_created_lazily_and_stopped(env):
    runtime = worker.WorkerRuntime()
    assert runtime._groups == {}
    env.prefetcher_factory.assert_not_called()

    runtime._ensure_group("G1")
    services = runtime._groups["G1"]
    await asyncio.wait_for(env.prefetch_started["G1"].wait(), timeout=1)

    # READY observer hooks editorial maintenance into the prefetch slot.
    services._prefetcher.set_ready_observer.assert_called_once_with(
        worker.ensure_editorial_prefetch
    )
    assert "prefetch_start:G1" in env.calls
    assert "editorial_start:G1" in env.calls

    # Idempotent: same group is not recreated; empty openid is ignored.
    runtime._ensure_group("G1")
    runtime._ensure_group("")
    assert runtime._groups["G1"] is services
    assert set(runtime._groups) == {"G1"}
    env.prefetcher_factory.assert_called_once_with("G1")

    await services.stop()
    assert services._stop.is_set()
    assert "prefetch_shutdown:G1" in env.calls
    assert "prefetch_stop:G1" in env.calls
    assert "editorial_stop:G1" in env.calls
    assert services._prefetch_task.done()
    assert services._editorial_task.done()


async def test_on_event_routes_group_messages_and_skips_private_groups(env):
    runtime = worker.WorkerRuntime()
    runtime._wait_for_background_tasks = AsyncMock()

    group_event = {
        "type": "message",
        "message_type": "group",
        "group_openid": "G1",
        "user_openid": "U1",
        "content": "/help",
    }
    await runtime._on_event(group_event)
    assert set(runtime._groups) == {"G1"}
    env.process_event.assert_awaited_once_with(group_event, spawn_handlers=True)

    # Private (C2C) messages never create group services but still dispatch.
    private_event = {
        "type": "message",
        "message_type": "private",
        "group_openid": "",
        "user_openid": "U1",
        "content": "/help",
    }
    await runtime._on_event(private_event)
    assert set(runtime._groups) == {"G1"}

    # Group message without an openid cannot start services.
    await runtime._on_event(
        {"type": "message", "message_type": "group", "group_openid": ""}
    )
    assert set(runtime._groups) == {"G1"}

    # Non-message events dispatch without creating services either.
    await runtime._on_event({"type": "member_add", "group_openid": "G2"})
    assert set(runtime._groups) == {"G1"}
    assert env.process_event.await_count == 4
    assert all(
        call.kwargs == {"spawn_handlers": True}
        for call in env.process_event.await_args_list
    )

    await runtime.stop()


async def test_worker_runtime_run_lifecycle(env):
    runtime = worker.WorkerRuntime()
    runtime._install_signal_handlers = lambda: None
    runtime._wait_for_background_tasks = AsyncMock()

    # Gateway is wired to the runtime's event hook and configured intents.
    assert len(env.gateways) == 1
    assert env.gateways[0].on_event == runtime._on_event
    assert env.gateways[0].intents == (1 << 25) | (1 << 26)

    task = asyncio.create_task(runtime.run())
    await asyncio.wait_for(env.gateway_started.wait(), timeout=1)

    assert env.calls[0] == "bootstrap"
    env.fake_db.open.assert_awaited_once()
    env.set_database.assert_called_once_with(env.fake_db)
    env.set_runtime.assert_called_once()
    handles = env.set_runtime.call_args.args[0]
    assert handles.db is env.fake_db
    assert handles.sender is env.sender
    assert handles.browser is env.browser
    assert "scheduler_start" in env.calls

    # The scheduler only sees groups the worker has actually heard from.
    assert await env.scheduler_state["get_groups"]() == []
    runtime._ensure_group("G1")
    await asyncio.wait_for(env.prefetch_started["G1"].wait(), timeout=1)
    assert await env.scheduler_state["get_groups"]() == ["G1"]

    runtime._shutdown.set()
    await asyncio.wait_for(task, timeout=2)

    assert runtime._gateway_stop.is_set()
    assert runtime._scheduler_stop.is_set()
    assert "gateway_stop" in env.calls
    assert "scheduler_stop" in env.calls
    assert "prefetch_stop:G1" in env.calls
    assert "editorial_stop:G1" in env.calls
    assert runtime._gateway_task.done()
    assert runtime._scheduler_task.done()
    env.browser.close.assert_awaited_once()
    env.rest.close.assert_awaited_once()
    env.tokens.close.assert_awaited_once()
    env.fake_db.close.assert_awaited_once()
    env.set_runtime.assert_called_with(None)


async def test_worker_runtime_requires_official_credentials(env, monkeypatch):
    monkeypatch.setattr(
        worker,
        "get_config",
        lambda: _fake_cfg("/tmp/data", validate_error=RuntimeError("app_id is required")),
    )
    with pytest.raises(RuntimeError, match="app_id"):
        worker.WorkerRuntime()
    assert env.gateways == []


async def test_main_async_sets_up_logging_and_runs_runtime(env):
    fake_runtime = SimpleNamespace(run=AsyncMock())
    constructor = MagicMock(return_value=fake_runtime)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(worker, "WorkerRuntime", constructor)
        await worker.main_async()

    assert "setup_logging" in env.calls
    constructor.assert_called_once_with()
    fake_runtime.run.assert_awaited_once()