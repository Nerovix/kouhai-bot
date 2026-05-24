"""Job scheduler — cron-like task execution for daily posts and checks.

Jobs are defined in jobs.py and configured for CURRENT_GROUP via scheduler_config.json.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Awaitable, Callable

from ..config import get_config

logger = logging.getLogger("kouhai-bot.scheduler")
DEFAULT_ENABLED_JOBS = ["daily_achievements", "daily_post", "contest_check"]

# ── Job registry ────────────────────────────────────────────────────────

@dataclass
class JobDef:
    """Definition of a scheduled job."""
    name: str
    fn: Callable[[int], Awaitable[None]]  # async(group_id) -> None
    schedule: str  # cron expression or interval like "12:00"
    description: str = ""


_registry: dict[str, JobDef] = {}


def register_job(job: JobDef) -> None:
    _registry[job.name] = job


# ── Per-group config ────────────────────────────────────────────────────

@dataclass
class GroupSchedulerConfig:
    group_id: int
    enabled_jobs: list[str] = field(default_factory=lambda: list(DEFAULT_ENABLED_JOBS))
    # Problem difficulty range
    min_rating: int | None = None
    max_rating: int | None = None
    # Custom cron overrides per job
    job_schedules: dict[str, str] = field(default_factory=dict)


def _config_path() -> str:
    return os.path.join(get_config().data_dir, "scheduler_config.json")


def _normalize_enabled_jobs(
    enabled_jobs: list[str],
    disabled_jobs: list[str] | None = None,
) -> list[str]:
    """Keep old configs getting the new daily achievement report by default."""
    disabled = set(disabled_jobs or [])
    jobs = list(enabled_jobs)
    if "daily_achievements" in disabled:
        return [job for job in jobs if job != "daily_achievements"]
    if "daily_post" in jobs and "daily_achievements" not in jobs:
        jobs.insert(jobs.index("daily_post"), "daily_achievements")
    return jobs


def load_group_configs() -> dict[int, GroupSchedulerConfig]:
    """Load scheduler configs. Creates a default for CURRENT_GROUP if missing."""
    path = _config_path()
    if not os.path.exists(path):
        # Default: the current group gets achievements, daily_post, and contest checks.
        cfg = get_config()
        gid = cfg.current_group
        defaults = {
            str(gid): {
                "group_id": gid,
                "enabled_jobs": DEFAULT_ENABLED_JOBS,
            }
        }
        with open(path, "w") as f:
            json.dump(defaults, f, indent=2, ensure_ascii=False)
        return {
            gid: GroupSchedulerConfig(
                group_id=gid,
                enabled_jobs=list(DEFAULT_ENABLED_JOBS),
            )
        }

    with open(path) as f:
        data = json.load(f)
    result = {}
    for key, val in data.items():
        gid = val.get("group_id", int(key))
        result[gid] = GroupSchedulerConfig(
            group_id=gid,
            enabled_jobs=_normalize_enabled_jobs(
                val.get("enabled_jobs", list(DEFAULT_ENABLED_JOBS)),
                val.get("disabled_jobs", []),
            ),
            min_rating=val.get("min_rating"),
            max_rating=val.get("max_rating"),
            job_schedules=val.get("job_schedules", {}),
        )
    cfg = get_config()
    if cfg.current_group not in result:
        result[cfg.current_group] = GroupSchedulerConfig(
            group_id=cfg.current_group,
            enabled_jobs=list(DEFAULT_ENABLED_JOBS),
        )
    return result


# ── Engine ──────────────────────────────────────────────────────────────

TZ = timezone(timedelta(hours=8))  # Asia/Shanghai


def _parse_time(schedule: str) -> tuple[int, int] | None:
    """Parse 'HH:MM' schedule into (hour, minute). Returns None if invalid."""
    try:
        parts = schedule.strip().split(":")
        return int(parts[0]), int(parts[1])
    except Exception:
        return None


async def _run_jobs_for_group(group_id: int, now: datetime) -> None:
    """Run all enabled jobs for a group if scheduled."""
    configs = load_group_configs()
    gcfg = configs.get(group_id)
    if not gcfg:
        return

    for job_name in gcfg.enabled_jobs:
        job = _registry.get(job_name)
        if not job:
            continue

        schedule = gcfg.job_schedules.get(job_name, job.schedule)
        target = _parse_time(schedule)
        if not target:
            continue

        if now.hour == target[0] and now.minute == target[1]:
            logger.info(f"[group_{group_id}] Running job: {job_name}")
            try:
                await job.fn(group_id)
            except Exception as e:
                logger.error(f"Job {job_name} failed for group {group_id}: {e}")


_last_run: dict[str, str] = {}  # "group_id:job_name" → "YYYY-MM-DD HH:MM"


async def scheduler_loop(
    *,
    stop_event: asyncio.Event | None = None,
    tick_seconds: float = 60.0,
) -> None:
    """Main scheduler loop — checks every ``tick_seconds`` and runs due jobs."""
    logger.info("Scheduler started")
    while True:
        try:
            now = datetime.now(TZ)
            cfg = get_config()

            await _run_jobs_for_group(cfg.current_group, now)

        except Exception as e:
            logger.error(f"Scheduler loop error: {e}")

        if stop_event is None:
            await asyncio.sleep(tick_seconds)
            continue

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=tick_seconds)
            break
        except asyncio.TimeoutError:
            continue

    logger.info("Scheduler stopped")
