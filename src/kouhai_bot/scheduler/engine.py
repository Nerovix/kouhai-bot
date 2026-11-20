"""Job scheduler — cron-like task execution across served groups.

Jobs are defined in jobs.py and run for every group the worker has discovered
(group = view). Per-group overrides (enabled jobs / schedule) live in
``scheduler_config.json`` keyed by group_openid; contest_check is on by default.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Awaitable, Callable

from ..config import get_config

logger = logging.getLogger("kouhai-bot.scheduler")
DEFAULT_ENABLED_JOBS = ["contest_check"]
REMOVED_JOBS = {"daily_achievements", "daily_post"}


@dataclass
class JobDef:
    name: str
    fn: Callable[[str], Awaitable[None]]  # async(group_openid) -> None
    schedule: str  # "HH:MM"
    description: str = ""


_registry: dict[str, JobDef] = {}


def register_job(job: JobDef) -> None:
    _registry[job.name] = job


@dataclass
class GroupSchedulerConfig:
    group_openid: str
    enabled_jobs: list[str] = field(default_factory=lambda: list(DEFAULT_ENABLED_JOBS))
    job_schedules: dict[str, str] = field(default_factory=dict)


def _config_path() -> str:
    return os.path.join(get_config().data_dir, "scheduler_config.json")


def _normalize_enabled_jobs(
    enabled_jobs: list[str],
    disabled_jobs: list[str] | None = None,
) -> list[str]:
    disabled = set(disabled_jobs or [])
    return [
        job for job in enabled_jobs
        if job not in REMOVED_JOBS and job not in disabled
    ]


def load_group_config(group_openid: str) -> GroupSchedulerConfig:
    """Per-group config with contest_check enabled by default."""
    path = _config_path()
    val: dict = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                val = data.get(group_openid, {}) or {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("scheduler_config unreadable, using defaults: %s", exc)
    if not isinstance(val, dict):
        val = {}
    return GroupSchedulerConfig(
        group_openid=group_openid,
        enabled_jobs=_normalize_enabled_jobs(
            val.get("enabled_jobs", list(DEFAULT_ENABLED_JOBS)),
            val.get("disabled_jobs", []),
        ),
        job_schedules=val.get("job_schedules", {}),
    )


TZ = timezone(timedelta(hours=8))  # Asia/Shanghai


def _parse_time(schedule: str) -> tuple[int, int] | None:
    try:
        parts = schedule.strip().split(":")
        return int(parts[0]), int(parts[1])
    except Exception:
        return None


_last_run: dict[str, str] = {}  # "group_openid:job_name" → "YYYY-MM-DD HH:MM"


async def _run_jobs_for_group(group_openid: str, now: datetime) -> None:
    gcfg = load_group_config(group_openid)
    minute_key = now.strftime("%Y-%m-%d %H:%M")
    for job_name in gcfg.enabled_jobs:
        job = _registry.get(job_name)
        if not job:
            continue
        schedule = gcfg.job_schedules.get(job_name, job.schedule)
        target = _parse_time(schedule)
        if not target:
            continue
        if now.hour != target[0] or now.minute != target[1]:
            continue
        dedup_key = f"{group_openid}:{job_name}"
        if _last_run.get(dedup_key) == minute_key:
            continue
        _last_run[dedup_key] = minute_key
        logger.info("[group_%s] Running job: %s", group_openid, job_name)
        try:
            await job.fn(group_openid)
        except Exception as exc:
            logger.error("Job %s failed for group %s: %s", job_name, group_openid, exc)


async def scheduler_loop(
    *,
    get_groups: Callable[[], Awaitable[list[str]]],
    stop_event: asyncio.Event | None = None,
    tick_seconds: float = 60.0,
) -> None:
    """Check every ``tick_seconds`` and run due jobs for each served group."""
    logger.info("Scheduler started")
    while True:
        try:
            now = datetime.now(TZ)
            for group_openid in await get_groups():
                await _run_jobs_for_group(group_openid, now)
        except Exception as exc:
            logger.error("Scheduler loop error: %s", exc)

        if stop_event is None:
            await asyncio.sleep(tick_seconds)
            continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=tick_seconds)
            break
        except asyncio.TimeoutError:
            continue
    logger.info("Scheduler stopped")