"""Multi-group scheduler engine: per-group config, dedup, and loop control."""

import asyncio
import json
import os
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from kouhai_bot.config import BotConfig
from kouhai_bot.scheduler import engine
from kouhai_bot.scheduler.engine import (
    DEFAULT_ENABLED_JOBS,
    TZ,
    JobDef,
    _normalize_enabled_jobs,
    _run_jobs_for_group,
    load_group_config,
    register_job,
    scheduler_loop,
)
from kouhai_bot.scheduler.jobs import register_builtin_jobs

G1 = "G1"
G2 = "G2"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.config._config", cfg)
    return cfg


@pytest.fixture(autouse=True)
def _clean_registry():
    engine._registry.clear()
    engine._last_run.clear()
    yield
    engine._registry.clear()
    engine._last_run.clear()


def _write_scheduler_config(cfg, data: dict | str):
    path = os.path.join(cfg.data_dir, "scheduler_config.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(data if isinstance(data, str) else json.dumps(data))


# --- defaults / normalization ------------------------------------------------


def test_scheduler_defaults_to_contest_check_only():
    assert DEFAULT_ENABLED_JOBS == ["contest_check"]


def test_scheduler_filters_removed_daily_jobs_from_old_configs():
    jobs = _normalize_enabled_jobs(["daily_achievements", "daily_post", "contest_check"])
    assert jobs == ["contest_check"]

    disabled = _normalize_enabled_jobs(["daily_post", "contest_check"], ["contest_check"])
    assert disabled == []


def test_builtin_jobs_register_contest_check():
    register_builtin_jobs()
    job = engine._registry["contest_check"]
    assert job.schedule == "12:01"
    assert asyncio.iscoroutinefunction(job.fn)


# --- per-group config ----------------------------------------------------------


def test_load_group_config_defaults_without_file(cfg):
    gcfg = load_group_config(G1)
    assert gcfg.group_openid == G1
    assert gcfg.enabled_jobs == ["contest_check"]
    assert gcfg.job_schedules == {}


def test_load_group_config_per_group_overrides(cfg):
    _write_scheduler_config(cfg, {
        G1: {"enabled_jobs": ["contest_check", "custom"], "job_schedules": {"contest_check": "09:30"}},
        G2: {"enabled_jobs": []},
    })
    c1 = load_group_config(G1)
    assert c1.enabled_jobs == ["contest_check", "custom"]
    assert c1.job_schedules == {"contest_check": "09:30"}
    c2 = load_group_config(G2)
    assert c2.enabled_jobs == []
    # Unknown group falls back to defaults.
    assert load_group_config("OTHER").enabled_jobs == ["contest_check"]


def test_load_group_config_corrupt_file_uses_defaults(cfg):
    _write_scheduler_config(cfg, "{not json")
    assert load_group_config(G1).enabled_jobs == ["contest_check"]


def test_load_group_config_strips_removed_and_disabled_jobs(cfg):
    _write_scheduler_config(cfg, {
        G1: {
            "enabled_jobs": ["daily_post", "daily_achievements", "contest_check", "custom"],
            "disabled_jobs": ["custom"],
        },
    })
    assert load_group_config(G1).enabled_jobs == ["contest_check"]


# --- job execution ---------------------------------------------------------------


async def test_due_job_runs_with_group_openid(cfg):
    fn = AsyncMock()
    register_job(JobDef(name="contest_check", fn=fn, schedule="12:01"))
    now = datetime(2026, 1, 1, 12, 1, tzinfo=TZ)
    await _run_jobs_for_group(G1, now)
    fn.assert_awaited_once_with(G1)


async def test_job_not_due_is_skipped(cfg):
    fn = AsyncMock()
    register_job(JobDef(name="contest_check", fn=fn, schedule="12:01"))
    await _run_jobs_for_group(G1, datetime(2026, 1, 1, 12, 2, tzinfo=TZ))
    fn.assert_not_awaited()


async def test_same_minute_dedup_per_group(cfg):
    fn = AsyncMock()
    register_job(JobDef(name="contest_check", fn=fn, schedule="12:01"))
    now = datetime(2026, 1, 1, 12, 1, tzinfo=TZ)
    await _run_jobs_for_group(G1, now)
    await _run_jobs_for_group(G1, now)
    fn.assert_awaited_once_with(G1)
    # Dedup is per group: another group still runs in the same minute.
    await _run_jobs_for_group(G2, now)
    assert fn.await_count == 2
    fn.assert_awaited_with(G2)
    # Next day's same minute runs again.
    await _run_jobs_for_group(G1, datetime(2026, 1, 2, 12, 1, tzinfo=TZ))
    assert fn.await_count == 3


async def test_schedule_override_from_group_config(cfg):
    _write_scheduler_config(cfg, {G1: {"job_schedules": {"contest_check": "09:30"}}})
    fn = AsyncMock()
    register_job(JobDef(name="contest_check", fn=fn, schedule="12:01"))
    await _run_jobs_for_group(G1, datetime(2026, 1, 1, 12, 1, tzinfo=TZ))
    fn.assert_not_awaited()
    await _run_jobs_for_group(G1, datetime(2026, 1, 1, 9, 30, tzinfo=TZ))
    fn.assert_awaited_once_with(G1)


async def test_unknown_job_and_invalid_schedule_are_skipped(cfg):
    _write_scheduler_config(cfg, {
        G1: {"enabled_jobs": ["missing_job", "contest_check"],
             "job_schedules": {"contest_check": "not-a-time"}},
    })
    fn = AsyncMock()
    register_job(JobDef(name="contest_check", fn=fn, schedule="12:01"))
    await _run_jobs_for_group(G1, datetime(2026, 1, 1, 12, 1, tzinfo=TZ))
    fn.assert_not_awaited()


async def test_job_exception_does_not_block_other_jobs(cfg):
    _write_scheduler_config(cfg, {G1: {"enabled_jobs": ["boom", "ok"]}})
    boom = AsyncMock(side_effect=RuntimeError("job crashed"))
    ok = AsyncMock()
    register_job(JobDef(name="boom", fn=boom, schedule="12:01"))
    register_job(JobDef(name="ok", fn=ok, schedule="12:01"))
    await _run_jobs_for_group(G1, datetime(2026, 1, 1, 12, 1, tzinfo=TZ))
    boom.assert_awaited_once()
    ok.assert_awaited_once_with(G1)


async def test_disabled_group_runs_nothing(cfg):
    _write_scheduler_config(cfg, {G1: {"enabled_jobs": []}})
    fn = AsyncMock()
    register_job(JobDef(name="contest_check", fn=fn, schedule="12:01"))
    await _run_jobs_for_group(G1, datetime(2026, 1, 1, 12, 1, tzinfo=TZ))
    fn.assert_not_awaited()


# --- loop control -----------------------------------------------------------------


async def test_scheduler_loop_runs_one_pass_for_all_groups_then_stops(cfg, monkeypatch):
    seen: list[str] = []

    async def fake_run(group_openid, _now):
        seen.append(group_openid)

    monkeypatch.setattr(engine, "_run_jobs_for_group", fake_run)

    async def get_groups():
        return [G1, G2]

    stop = asyncio.Event()
    stop.set()
    await scheduler_loop(get_groups=get_groups, stop_event=stop, tick_seconds=0.01)
    assert seen == [G1, G2]


async def test_scheduler_loop_ticks_until_stopped(cfg, monkeypatch):
    passes = 0
    enough = asyncio.Event()

    async def fake_run(_group_openid, _now):
        nonlocal passes
        passes += 1
        if passes >= 4:
            enough.set()

    monkeypatch.setattr(engine, "_run_jobs_for_group", fake_run)

    async def get_groups():
        return [G1]

    stop = asyncio.Event()
    task = asyncio.create_task(
        scheduler_loop(get_groups=get_groups, stop_event=stop, tick_seconds=0.01)
    )
    await asyncio.wait_for(enough.wait(), timeout=2)
    stop.set()
    await task
    assert passes >= 4


async def test_scheduler_loop_survives_group_listing_error(cfg, monkeypatch):
    calls = 0

    async def get_groups():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("db hiccup")
        return []

    stop = asyncio.Event()

    async def stop_soon():
        await asyncio.sleep(0.05)
        stop.set()

    stopper = asyncio.create_task(stop_soon())
    await scheduler_loop(get_groups=get_groups, stop_event=stop, tick_seconds=0.01)
    await stopper
    assert calls >= 2