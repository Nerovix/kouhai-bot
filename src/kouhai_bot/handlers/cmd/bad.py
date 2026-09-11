"""/bad — mark the AI's latest replied interaction as unsatisfactory for replay/debugging."""

from __future__ import annotations

import logging
import re
from datetime import datetime

from .. import registry
from ..registry import CommandDef
from ..shared import append_bad_report, get_today_problem, record_kind
from ...napcat.client import (
    build_at,
    build_plain_message,
    build_private_reaction_message,
    build_text,
    react_emoji,
    send_group_msg,
    send_private_msg,
)
from ...private_judge import (
    GROUP_SCOPE,
    PRIVATE_SCOPE,
    TZ,
    append_private_bad_report,
    get_private_current_pid,
    group_problem_history,
    load_private_problem_history,
)
from .submit import run_group_state_update

logger = logging.getLogger("kouhai-bot.cmd.bad")

OK_REACTION_ID = "128076"
NOTE_MAX_LEN = 500

# Results whose live message reached the user: real LLM answers (an
# incorrect verdict with an empty reply fell back to the reason live, see
# format_history_records) AND failure notices — timeout /
# service_unavailable / no_statement / image_unsupported all delivered a
# message before the record was saved. Only pending and superseded never
# delivered anything the user could complain about.
_REPLIED_RESULTS = {
    "correct", "incorrect", "clarify", "review",
    "timeout", "service_unavailable", "no_statement", "image_unsupported",
}


def _received_reply(record: dict) -> bool:
    kind = record_kind(record)
    if not kind:
        return False
    result = str(record.get("result", "") or "")
    if result not in _REPLIED_RESULTS:
        # pending (still in flight) / superseded (dropped without a reply).
        return False
    if result in {"clarify", "review"} and not str(record.get("reply", "") or "").strip():
        # Defensive: an LLM-answer result with an empty reply never reached
        # the user (failure paths store their notice outside the record).
        return False
    return True


def _find_bad_target(history: list[dict]) -> tuple[int, dict] | None:
    """Latest record that actually received a reply (history is timestamp-ascending)."""
    for idx in range(len(history) - 1, -1, -1):
        record = history[idx]
        if isinstance(record, dict) and _received_reply(record):
            return idx, record
    return None


async def _send_text(scope: str, group_id: int, user_id: int, text: str) -> None:
    if scope == PRIVATE_SCOPE:
        await send_private_msg(user_id, build_plain_message(text))
    else:
        await send_group_msg(group_id, [build_at(user_id), build_text(f" {text}")])


async def _ack_recorded(scope: str, group_id: int, user_id: int, message_id: str) -> None:
    """Same ack as /clear's success: 👌 reaction in group, fallback message in private."""
    if scope == PRIVATE_SCOPE:
        await send_private_msg(user_id, build_private_reaction_message(OK_REACTION_ID))
        return
    try:
        await react_emoji(message_id, OK_REACTION_ID)
    except Exception as e:
        logger.warning("failed to react to recorded /bad feedback %s: %s", message_id, e)


async def handle(group_id: int, user_id: int, sender: dict,
                 message_id: str, raw_text: str, segments: list,
                 event: dict) -> None:
    scope = PRIVATE_SCOPE if event.get("message_type") == "private" else GROUP_SCOPE

    match = re.fullmatch(r"/bad(?:\s+(.+))?", raw_text.strip(), re.DOTALL)
    if match is None:
        await _send_text(scope, group_id, user_id, "用法：/bad [简短备注]～")
        return
    note = (match.group(1) or "").strip()
    if len(note) > NOTE_MAX_LEN:
        logger.warning(
            "truncating /bad note from %d to %d chars (user %s)",
            len(note), NOTE_MAX_LEN, user_id,
        )
        note = note[:NOTE_MAX_LEN]

    try:
        if scope == PRIVATE_SCOPE:
            pid = get_private_current_pid(user_id)
        else:
            problem = get_today_problem(group_id)
            pid = str(problem.get("today", "") or "") if problem else ""

        if not pid:
            if scope == PRIVATE_SCOPE:
                await _send_text(
                    scope, group_id, user_id,
                    "当前还没有 private judge 题目～先发 /setproblem 设置一道题吧。",
                )
            else:
                await _send_text(scope, group_id, user_id, "还没有今日题目哦～")
            return

        if scope == PRIVATE_SCOPE:
            history = load_private_problem_history(user_id, pid)
        else:
            history = group_problem_history(group_id, user_id, pid)

        if not history:
            await _send_text(
                scope, group_id, user_id,
                "这题这边还没有和 AI 的交流记录哦～先 /submit、/clarify 或 /review 一次再 /bad 吧。",
            )
            return

        target = _find_bad_target(history)
        if target is None:
            await _send_text(
                scope, group_id, user_id,
                "这题最近的交互还没有收到 AI 回复（可能还在处理中），等回复后再 /bad 哦～",
            )
            return
        record_index, record = target

        report = {
            "scope": scope,
            "group_id": int(group_id),
            "user_id": int(user_id),
            "reported_at": datetime.now(TZ).isoformat(),
            "note": note,
            "cmd_message_id": str(message_id or ""),
            "target": {
                "problem": pid,
                "record_index": int(record_index),
                "record": dict(record),
            },
        }

        if scope == PRIVATE_SCOPE:
            # Pure sync load→append→atomic-write; single asyncio loop means
            # no interleaving is possible and users/<uid>.json is untouched.
            append_private_bad_report(user_id, report)
        else:
            # Group JSON writes go through the per-group state lock.
            await run_group_state_update(
                group_id, lambda: append_bad_report(group_id, report),
            )
    except Exception:
        logger.error(
            "failed to record /bad feedback (scope=%s user=%s)",
            scope, user_id, exc_info=True,
        )
        await _send_text(scope, group_id, user_id, "记录反馈失败了，联系一下管理员帮帮忙吧～")
        return

    await _ack_recorded(scope, group_id, user_id, message_id)


def register() -> None:
    registry.register(CommandDef(
        name="bad",
        aliases=[],
        description="标记对AI最新回复不满意，记录反馈供维护复盘",
        usage="[简短备注]",
        handler=handle,
        cooldown=3,
    ))
