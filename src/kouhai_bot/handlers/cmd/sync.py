"""/sync — copy current-problem history between group and private judge."""

from __future__ import annotations

import logging
import re
from collections import Counter

from .. import registry
from ..registry import CommandDef
from ...eventlog import EVENT_META_KEY, log_command_finished
from ...napcat.client import build_at, build_plain_message, build_text, react_emoji, send_group_msg, send_private_msg
from ...private_judge import (
    GROUP_SCOPE,
    PRIVATE_SCOPE,
    copy_records,
    current_group_pid,
    get_private_current_pid,
    group_problem_history,
    is_group_problem_solved,
    load_private_problem_history,
    mark_private_solved,
    private_record_has_correct,
    replace_group_problem_clarifies,
    replace_group_problem_history,
    replace_private_problem_clarifies,
    replace_private_problem_history,
    send_history_card,
)
from ...user_groups import get_user_group, is_dynamic_submit_delay_enabled, submit_remaining_sec
from ..shared import (
    fetch_group_member_nickname_map,
    format_points,
    get_today_problem,
    load_known_problem_ratings,
    load_scoreboard,
    rating_to_points,
)
from .submit import (
    _reveal_problem_source,
    _update_scoreboard_for_pid,
    has_active_problem_request,
    run_group_state_update,
)
from ...editorial_followup import schedule_post_solve_editorial_followup

logger = logging.getLogger("kouhai-bot.cmd.sync")
OK_REACTION_ID = "128076"


def _is_starred_limited(user_id: int, group_id: int) -> bool:
    return is_dynamic_submit_delay_enabled(get_user_group(user_id)) and submit_remaining_sec(user_id, group_id) > 0


def _only_clarifies(records: list[dict]) -> list[dict]:
    return [item for item in records if item.get("type") == "clarify"]


async def _history_display_name(group_id: int, user_id: int, sender: dict) -> str:
    try:
        nickname_map = await fetch_group_member_nickname_map(group_id)
        name = nickname_map.get(str(user_id))
        if name:
            return name
    except Exception:
        pass
    return sender.get("card") or sender.get("nickname") or str(user_id)


async def _send_text(scope: str, group_id: int, user_id: int, text: str) -> None:
    if scope == PRIVATE_SCOPE:
        await send_private_msg(user_id, build_plain_message(text))
    else:
        await send_group_msg(group_id, [build_at(user_id), build_text(f" {text}")])


async def _react_group_success(message_id: str) -> None:
    try:
        await react_emoji(message_id, OK_REACTION_ID)
    except Exception as e:
        logger.warning("failed to react to successful sync %s: %s", message_id, e)


def _synced_record_counts(records: list[dict], *, scored_correct: bool) -> dict[str, int]:
    counter = Counter(str(item.get("type", "") or "") for item in records if isinstance(item, dict))
    return {
        "synced_submit_count": int(counter.get("submit", 0)),
        "synced_clarify_count": int(counter.get("clarify", 0)),
        "synced_review_count": int(counter.get("review", 0)),
        "synced_correct_count": 1 if scored_correct else 0,
    }


async def _score_synced_private_ac(group_id: int, user_id: int, sender: dict, pid: str) -> tuple[str, bool]:
    problem = get_today_problem(group_id)
    if not problem or str(problem.get("today", "") or "") != pid:
        return "", False
    if is_group_problem_solved(group_id, pid):
        return "群里已经有人先通过这题了，本次只同步记录，不再加分。", False

    nickname = sender.get("card") or sender.get("nickname") or str(user_id)

    def _update():
        sb = load_scoreboard(group_id)
        if any(str(item.get("problem", "") or "") == pid for item in sb.get("solves", [])):
            return None
        return _update_scoreboard_for_pid(group_id, user_id, nickname, pid, problem)

    updated = await run_group_state_update(group_id, _update)
    if updated is None:
        return "群里已经有人先通过这题了，本次只同步记录，不再加分。", False
    is_fb, solved, top5, sb = updated
    ranked = []
    try:
        user_group = get_user_group(user_id)
        from ..shared import build_scoreboard_entries
        ranked = build_scoreboard_entries(group_id, sb, user_group_name=user_group.name)
    except Exception:
        ranked = []
    current_entry = next((entry for entry in ranked if str(entry["user_id"]) == str(user_id)), None)
    rating = load_known_problem_ratings(group_id, {pid}).get(pid)
    rank = int(current_entry["rank"]) if current_entry else 1
    total_score = format_points(float(current_entry["score"])) if current_entry else "0"
    score_gain = format_points(rating_to_points(rating)) if rating is not None else "0"
    cheer = (
        f"恭喜拿下本题一血！🎉 本题 +{score_gain} 分（共 {solved} 题，总分 {total_score}），当前第 {rank}"
        if is_fb
        else f"做对了！🎉 本题 +{score_gain} 分（共 {solved} 题，总分 {total_score}），当前第 {rank}"
    )
    lines = [cheer]
    if top5:
        nickname_map = await fetch_group_member_nickname_map(group_id)
        lines.extend(["", "🏆 Top 5："])
        for entry in top5:
            uid = str(entry["user_id"])
            name = nickname_map.get(uid) or entry["nickname"] or uid
            lines.append(
                f"{entry['rank']}. {name} ({entry['solved']} 题，{format_points(entry['score'])} 分)"
            )
    reveal = await _reveal_problem_source(group_id)
    if reveal:
        lines.extend(["", reveal])
    schedule_post_solve_editorial_followup(group_id, pid)
    return "\n".join(lines), True


async def handle(group_id: int, user_id: int, sender: dict,
                 message_id: str, raw_text: str, segments: list,
                 event: dict) -> None:
    if not re.fullmatch(r"/sync(?:\s+)?", raw_text.strip()):
        await _send_text(
            PRIVATE_SCOPE if event.get("message_type") == "private" else GROUP_SCOPE,
            group_id,
            user_id,
            "用法：/sync",
        )
        return

    target_scope = PRIVATE_SCOPE if event.get("message_type") == "private" else GROUP_SCOPE
    source_scope = GROUP_SCOPE if target_scope == PRIVATE_SCOPE else PRIVATE_SCOPE
    pid = get_private_current_pid(user_id) if target_scope == PRIVATE_SCOPE else current_group_pid(group_id)
    group_pid = current_group_pid(group_id)

    if not pid:
        await _send_text(target_scope, group_id, user_id, "当前这边还没有题目，不能 sync。")
        return
    if not group_pid or pid != group_pid:
        await _send_text(
            target_scope,
            group_id,
            user_id,
            "只有当前服务群题目可以 sync；其他 private 题会一直留在 private judge 里。",
        )
        return

    if has_active_problem_request(scope=GROUP_SCOPE, group_id=group_id, user_id=user_id, pid=pid) \
            or has_active_problem_request(scope=PRIVATE_SCOPE, group_id=group_id, user_id=user_id, pid=pid):
        await _send_text(target_scope, group_id, user_id, "这题还有请求正在处理中，等它结束后再 /sync 吧。")
        return

    if source_scope == PRIVATE_SCOPE:
        source_records = load_private_problem_history(user_id, pid)
    else:
        source_records = group_problem_history(group_id, user_id, pid)

    starred_limited = _is_starred_limited(user_id, group_id)
    if starred_limited:
        source_records = _only_clarifies(source_records)

    if not source_records:
        if starred_limited:
            await _send_text(
                target_scope,
                group_id,
                user_id,
                "你是打星用户且目前还在提交 CD 内，仅能同步 clarify 记录；"
                "对面没有可同步的 clarify 记录，本次 sync 已取消，没有覆盖任何历史。",
            )
        else:
            await _send_text(
                target_scope,
                group_id,
                user_id,
                "对面没有这道题的历史记录，本次 sync 已取消，没有覆盖任何历史。",
            )
        return

    records_to_copy = copy_records(source_records)
    if target_scope == GROUP_SCOPE:
        def _replace_group_records() -> None:
            if starred_limited:
                replace_group_problem_clarifies(group_id, user_id, pid, records_to_copy)
            else:
                replace_group_problem_history(group_id, user_id, pid, records_to_copy)

        await run_group_state_update(group_id, _replace_group_records)
    else:
        if starred_limited:
            replace_private_problem_clarifies(user_id, pid, records_to_copy)
        else:
            replace_private_problem_history(user_id, pid, records_to_copy)
        if source_scope == GROUP_SCOPE and is_group_problem_solved(group_id, pid):
            mark_private_solved(user_id, pid, source="group")

    await send_history_card(
        destination=target_scope,
        user_id=user_id,
        group_id=group_id,
        records=source_records,
        user_display_name=await _history_display_name(group_id, user_id, sender),
    )

    extra = ""
    scored_correct = False
    if target_scope == GROUP_SCOPE and source_scope == PRIVATE_SCOPE and not starred_limited:
        if private_record_has_correct(source_records, pid):
            extra, scored_correct = await _score_synced_private_ac(group_id, user_id, sender, pid)
    elif starred_limited:
        extra = "你是打星用户且目前还在提交 CD 内，本次仅同步 clarify，submit/review/通过记录已忽略。"

    if target_scope == GROUP_SCOPE:
        await _react_group_success(message_id)
        log_command_finished(
            event.get(EVENT_META_KEY),
            status="correct" if scored_correct else "synced",
            problem=pid,
            extra={
                **_synced_record_counts(source_records, scored_correct=scored_correct),
                "source_scope": source_scope,
                "target_scope": target_scope,
            },
        )
        if extra:
            await _send_text(target_scope, group_id, user_id, extra)
    elif extra:
        await _send_text(target_scope, group_id, user_id, extra)


def register() -> None:
    registry.register(CommandDef(
        name="sync",
        aliases=[],
        description="在群聊和 private judge 间同步当前群题记录（另一侧覆盖当前侧）",
        usage="",
        handler=handle,
        cooldown=3,
    ))
