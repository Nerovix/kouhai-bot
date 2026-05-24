"""Daily achievement reports computed from command event logs."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from .eventlog import TZ, load_events
from .napcat.client import build_plain_message, send_group_msg


ACHIEVEMENT_CUTOFF_HOUR = 4


@dataclass(frozen=True)
class AchievementWindow:
    start: datetime
    end: datetime


@dataclass
class CommandRecord:
    request_id: str
    command: str
    user_id: int
    nickname: str
    received_at: datetime
    problem: str = ""
    status: str = ""


def achievement_window(now: datetime | None = None) -> AchievementWindow:
    """Return the previous 04:00-to-04:00 reporting window."""
    current = (now or datetime.now(TZ)).astimezone(TZ)
    end = datetime.combine(current.date(), time(ACHIEVEMENT_CUTOFF_HOUR), tzinfo=TZ)
    if current < end:
        end -= timedelta(days=1)
    return AchievementWindow(start=end - timedelta(days=1), end=end)


def _parse_timestamp(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value).astimezone(TZ)
    except Exception:
        return None


def _dates_between(start: datetime, end: datetime) -> list[str]:
    dates: list[str] = []
    day = start.date()
    last = end.date()
    while day <= last:
        dates.append(day.isoformat())
        day += timedelta(days=1)
    return dates


def _load_window_events(group_id: int, window: AchievementWindow, now: datetime) -> list[dict[str, Any]]:
    # Include files through report time so late finishes can be joined to in-window receives.
    dates = _dates_between(window.start, now.astimezone(TZ))
    events: list[dict[str, Any]] = []
    for date in dates:
        events.extend(load_events(group_id, date))
    return events


def _command_records(
    group_id: int,
    window: AchievementWindow,
    now: datetime,
) -> list[CommandRecord]:
    by_id: dict[str, CommandRecord] = {}
    finished: dict[str, dict[str, Any]] = {}

    for item in _load_window_events(group_id, window, now):
        request_id = str(item.get("request_id", ""))
        if not request_id:
            continue

        if item.get("type") == "received":
            received_at = _parse_timestamp(str(item.get("timestamp", "")))
            if not received_at or not (window.start <= received_at < window.end):
                continue
            by_id[request_id] = CommandRecord(
                request_id=request_id,
                command=str(item.get("command", "")),
                user_id=int(item.get("user_id", 0) or 0),
                nickname=str(item.get("nickname", "")) or str(item.get("user_id", "")),
                received_at=received_at,
                problem=str(item.get("problem", "")),
            )
            if request_id in finished:
                _apply_finished(by_id[request_id], finished[request_id])

        elif item.get("type") == "finished":
            finished[request_id] = item
            if request_id in by_id:
                _apply_finished(by_id[request_id], item)

    return sorted(by_id.values(), key=lambda record: record.received_at)


def _apply_finished(record: CommandRecord, item: dict[str, Any]) -> None:
    record.status = str(item.get("status", ""))
    record.problem = str(item.get("problem", "")) or record.problem


def _name(record: CommandRecord) -> str:
    return record.nickname or str(record.user_id)


def _format_time(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%H:%M")


def _format_window(window: AchievementWindow) -> str:
    start = window.start.strftime("%m-%d %H:%M")
    end = window.end.strftime("%m-%d %H:%M")
    return f"{start} ~ {end}"


def _top_users(counter: Counter[int], names: dict[int, str]) -> str:
    if not counter:
        return "暂无"
    best = max(counter.values())
    if best <= 0:
        return "暂无"
    winners = [names.get(uid, str(uid)) for uid, count in counter.items() if count == best]
    return f"{'、'.join(winners)}（{best} 次）"


def build_achievement_report(
    group_id: int,
    now: datetime | None = None,
) -> str:
    """Build the previous day's achievement report for a group."""
    current = (now or datetime.now(TZ)).astimezone(TZ)
    window = achievement_window(current)
    records = _command_records(group_id, window, current)
    names = {record.user_id: _name(record) for record in records}

    submits = [record for record in records if record.command == "submit"]
    clarifies = [record for record in records if record.command == "clarify"]
    reviews = [record for record in records if record.command == "review"]

    submit_attempts = Counter(record.user_id for record in submits)
    solves = Counter(
        record.user_id
        for record in submits
        if record.status == "correct"
    )
    clarify_counts = Counter(record.user_id for record in clarifies)
    review_counts = Counter(record.user_id for record in reviews)

    title_date = window.start.strftime("%Y-%m-%d")
    lines = [
        f"昨日成就榜（{title_date}）",
        f"统计窗口：{_format_window(window)}",
        "",
    ]

    if not records:
        lines.append("昨日还没有可统计的指令记录。")
        return "\n".join(lines)

    if submits:
        first = submits[0]
        last = submits[-1]
        lines.append(f"最早 submit：{_name(first)}（{_format_time(first.received_at)}）")
        lines.append(f"最晚 submit：{_name(last)}（{_format_time(last.received_at)}）")
    else:
        lines.append("最早 submit：暂无")
        lines.append("最晚 submit：暂无")

    lines.extend([
        f"通过题目最多：{_top_users(solves, names)}",
        f"submit 尝试最多：{_top_users(submit_attempts, names)}",
        f"review 最多：{_top_users(review_counts, names)}",
        f"clarify 最多：{_top_users(clarify_counts, names)}",
    ])
    return "\n".join(lines)


async def post_daily_achievements(group_id: int) -> None:
    """Send yesterday's achievement report to a group."""
    report = build_achievement_report(group_id)
    await send_group_msg(group_id, build_plain_message(report))
