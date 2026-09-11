"""/bad — mark the AI's latest delivered reply as unsatisfactory for replay/debugging.

Targeting reads the per-user last-interaction cache only (written by
_save_context_record whenever a replied record is persisted): the cache is
scope-local, crosses problem switches, survives /clear, and /sync carries it
across sides together with the history.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from .. import registry
from ..registry import CommandDef
from ..shared import append_bad_report, load_group_last_interaction
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
    load_private_last_interaction,
)
from .submit import run_group_state_update

logger = logging.getLogger("kouhai-bot.cmd.bad")

OK_REACTION_ID = "128076"
NOTE_MAX_LEN = 500


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
        entry = (
            load_private_last_interaction(user_id)
            if scope == PRIVATE_SCOPE
            else load_group_last_interaction(group_id, user_id)
        )
        record = entry.get("record") if entry else None
        if not isinstance(record, dict):
            await _send_text(
                scope, group_id, user_id,
                "这边还没有可以标注的 AI 回复哦～先 /submit、/clarify 或 /review 一次，"
                "收到回复后再来 /bad 吧。",
            )
            return

        report = {
            "scope": scope,
            "group_id": int(group_id),
            "user_id": int(user_id),
            "reported_at": datetime.now(TZ).isoformat(),
            "note": note,
            "cmd_message_id": str(message_id or ""),
            "target": {
                "problem": str(record.get("problem", "") or ""),
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
