"""Built-in scheduled jobs."""

from .engine import register_job, JobDef


async def _contest_check(group_id: int) -> None:
    """Check upcoming CF contests and notify group."""
    from ..handlers.shared import check_contests_for_group
    await check_contests_for_group(group_id)


def register_builtin_jobs() -> None:
    register_job(JobDef(
        name="contest_check",
        fn=_contest_check,
        schedule="12:01",
        description="比赛提醒（检查 24h 内 CF 比赛）",
    ))
