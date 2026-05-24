"""Built-in scheduled jobs."""

from .engine import register_job, JobDef


async def _daily_post(group_id: int) -> None:
    """Daily problem post at noon — checks should_post_today.

    - Already solved → post new problem
    - Unsolved → send reminder
    - No problem yet → post first problem
    """
    from ..handlers.shared import get_today_problem, is_already_solved
    from ..napcat.client import build_plain_message, send_group_msg

    problem = get_today_problem(group_id)

    if problem and not is_already_solved(group_id):
        # Has unsolved problem → remind, don't post new
        await send_group_msg(group_id, build_plain_message(
            "中午好呀☀️ 今天中午先不更新新题～\n"
            "pending 里的那道题还没有人拿下呢，先去试试？\n"
            "发 /problem 可以随时回看题目，期待你来一血！💪"
        ))
        return

    from ..handlers.cmd.newproblem import do_daily_post
    await do_daily_post(group_id, prefix=None)


async def _contest_check(group_id: int) -> None:
    """Check upcoming CF contests and notify group."""
    from ..handlers.shared import check_contests_for_group
    await check_contests_for_group(group_id)


async def _daily_achievements(group_id: int) -> None:
    """Post yesterday's achievement report."""
    from ..achievements import post_daily_achievements
    await post_daily_achievements(group_id)


def register_builtin_jobs() -> None:
    register_job(JobDef(
        name="daily_achievements",
        fn=_daily_achievements,
        schedule="12:00",
        description="每日中午公布昨日成就榜",
    ))
    register_job(JobDef(
        name="daily_post",
        fn=_daily_post,
        schedule="12:00",
        description="每日中午发题（已解则刷新，未解则提醒）",
    ))
    register_job(JobDef(
        name="contest_check",
        fn=_contest_check,
        schedule="12:01",
        description="比赛提醒（每天发题后检查 24h 内 CF 比赛）",
    ))
