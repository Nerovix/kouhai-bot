"""/problem, /tag, /reveal, /scoreboard — real implementations."""

from .. import registry
from ..registry import CommandDef
from ..shared import (
    build_scoreboard_entries,
    fetch_group_member_nickname_map,
    format_points,
    get_today_problem,
    high_difficulty_notice,
    is_already_solved,
    load_scoreboard,
    sanitize_cached_problem_card_payload,
)
from ...napcat.client import (
    build_plain_message,
    send_group_msg,
    send_private_msg,
)
from ...private_judge import (
    get_private_current_problem,
    send_problem_card_private,
)


def _nick(sender: dict) -> str:
    return sender.get("card") or sender.get("nickname") or str(sender.get("user_id", "?"))


async def _send_solved_problem_hint(group_id: int, nickname: str) -> None:
    try:
        solved = is_already_solved(group_id)
    except Exception:
        return
    if not solved:
        return
    try:
        await send_group_msg(group_id, build_plain_message(
            f"@{nickname} 这题已经通过啦，可以发 /newproblem 挑战下一题～"
        ))
    except Exception:
        return


async def _send_high_difficulty_notice_group(group_id: int, problem: dict | None) -> None:
    notice = high_difficulty_notice(problem)
    if not notice:
        return
    try:
        await send_group_msg(group_id, build_plain_message(notice))
    except Exception:
        return


# ── /problem ────────────────────────────────────────────────────────────

async def handle_problem(group_id: int, user_id: int, sender: dict,
                         message_id: str, raw_text: str, segments: list,
                         event: dict) -> None:
    """Resend today's problem as the merged-forward card.
    Falls back to a hint when daily_msg.json is missing."""
    if raw_text.lstrip() != "/problem":
        return
    if event.get("message_type") == "private":
        problem = get_private_current_problem(user_id)
        if not problem:
            await send_private_msg(user_id, build_plain_message(
                "你还没有 private judge 题目～先发 /setproblem 设置一道吧。"
            ))
            return
        ok = await send_problem_card_private(
            user_id,
            group_id,
            problem,
            prefer_group_card=True,
        )
        if not ok:
            await send_private_msg(user_id, build_plain_message(
                "暂时无法重新发送题目，稍后再试试？"
            ))
        return

    await resend_current_problem_group(group_id, sender)


async def resend_current_problem_group(group_id: int, sender: dict) -> None:
    """Resend today's problem card to the group (shared by /pb and poke).

    Rebuilds the merged-forward card from cached content in daily_msg.json;
    falls back to a friendly hint when no usable cache exists.
    """
    from ...config import get_config
    from ...napcat.client import send_group_forward_msg
    from ..shared import save_problem_card_ref
    from .newproblem import _send_problem_forward_card
    import json
    import os

    cfg = get_config()
    state_dir = os.path.join(cfg.data_dir, "groups", str(group_id))
    daily_msg_path = os.path.join(state_dir, "daily_msg.json")
    nickname = _nick(sender)

    current_problem = get_today_problem(group_id)
    current_pid = str(current_problem.get("today", "") or "") if current_problem else ""
    if not current_problem:
        await send_group_msg(group_id, build_plain_message(
            f"@{nickname} 暂时不能查看当前题目，可能是暂时还不存在当前题目，试试 /newproblem？"
        ))
        return

    # Rebuild the card from cached content (legacy node-id fields are ignored).
    if os.path.exists(daily_msg_path):
        try:
            with open(daily_msg_path) as f:
                daily_msg = json.load(f)
            pid = str(daily_msg.get("pid", "") or "")
            if pid != current_pid:
                raise ValueError(f"stale daily_msg pid={pid}, current={current_pid}")
            daily_msg = sanitize_cached_problem_card_payload(daily_msg)[0]
            post_msg = daily_msg.get("post_msg")
            sample_messages = daily_msg.get("sample_messages")
            notes_message = daily_msg.get("notes_message")
            snake_enabled = bool(daily_msg.get("snake_enabled", True))
            if isinstance(post_msg, str) and isinstance(sample_messages, list):
                fwd_resp, _node_payload = await _send_problem_forward_card(
                    group_id=group_id,
                    post_msg=post_msg,
                    sample_messages=[str(item) for item in sample_messages],
                    notes_message=str(notes_message) if isinstance(notes_message, str) else "",
                    snake_enabled=snake_enabled,
                )
                if fwd_resp:
                    if pid:
                        save_problem_card_ref(group_id, fwd_resp, pid, "problem_resend")
                    # Persist the sanitized cache (legacy ids dropped, thinking
                    # tags stripped) so later rebuilds start from clean content.
                    with open(daily_msg_path, "w", encoding="utf-8") as f:
                        json.dump(daily_msg, f, ensure_ascii=False, indent=2)
                    await _send_high_difficulty_notice_group(group_id, current_problem)
                    await _send_solved_problem_hint(group_id, nickname)
                    return
        except Exception:
            pass

    # Fallback: forward card failed or daily_msg.json missing
    await send_group_msg(group_id, build_plain_message(
        f"@{nickname} 暂时无法重新发送题目（明天开始就正常啦），试试 /tag 查看标签吧～"
    ))


# ── /tag ────────────────────────────────────────────────────────────────

async def handle_tag(group_id: int, user_id: int, sender: dict,
                     message_id: str, raw_text: str, segments: list,
                     event: dict) -> None:
    nickname = _nick(sender)
    is_private = event.get("message_type") == "private"
    problem = get_private_current_problem(user_id) if is_private else get_today_problem(group_id)
    if not problem:
        if is_private:
            await send_private_msg(user_id, build_plain_message("你还没有 private judge 题目～"))
        else:
            await send_group_msg(group_id, build_plain_message(f"@{nickname} 还没有今日题目～"))
        return
    tags = problem.get("tags", [])
    if not tags:
        if is_private:
            await send_private_msg(user_id, build_plain_message("当前题目没有 tag 信息～"))
        else:
            await send_group_msg(group_id, build_plain_message(f"@{nickname} 当前题目没有 tag 信息～"))
        return
    text = f"当前题目的 tags：{'、'.join(tags)}"
    if is_private:
        await send_private_msg(user_id, build_plain_message(text))
    else:
        await send_group_msg(group_id, build_plain_message(f"@{nickname} {text}"))


# ── /scoreboard ─────────────────────────────────────────────────────────

async def handle_scoreboard(group_id: int, user_id: int, sender: dict,
                            message_id: str, raw_text: str, segments: list,
                            event: dict) -> None:
    from ...config import get_config
    from ...user_groups import DEFAULT_GROUP, configured_user_groups
    from ...napcat.client import (
        build_node,
        build_plain_message,
        resolve_bot_display_name,
        send_group_msg,
        send_group_forward_msg,
    )

    cfg = get_config()
    nickname = _nick(sender)
    sb = load_scoreboard(group_id)
    solves = sb.get("solves", [])
    if not solves:
        await send_group_msg(group_id, build_plain_message(
            f"@{nickname} 还没有人解出过题目呢，来做第一人吧！🚀"
        ))
        return

    all_ranked = build_scoreboard_entries(group_id, sb)
    default_ranked = build_scoreboard_entries(group_id, sb, user_group_name=DEFAULT_GROUP)
    nickname_map = await fetch_group_member_nickname_map(group_id)

    lines = [
        f"📊 累计解题排行榜（共 {len(all_ranked)} 人）",
        "计分公式：2000=1 分，每 +300 分翻倍（2^((rating-2000)/300)）",
        "",
    ]

    def _append_entries(entries: list[dict]) -> None:
        for entry in entries:
            uid = str(entry["user_id"])
            name = nickname_map.get(uid) or entry["nickname"] or uid
            lines.append(
                f"#{entry['rank']} {name} — {entry['solved']} 题 / {format_points(entry['score'])} 分"
            )

    _append_entries(default_ranked)
    for user_group in configured_user_groups():
        ranked = build_scoreboard_entries(group_id, sb, user_group_name=user_group.name)
        if not ranked:
            continue
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(f"📊 {user_group.display_name}排行榜")
        _append_entries(ranked)

    score_text = "\n".join(lines)

    # Merged-forward card assembled from custom nodes (no self-send)
    bot_name = await resolve_bot_display_name(group_id)
    fwd_resp = await send_group_forward_msg(group_id, [
        build_node(
            user_id=cfg.bot_qq,
            nickname=bot_name,
            content=build_plain_message(score_text),
        ),
    ])
    if not fwd_resp:
        await send_group_msg(group_id, build_plain_message(score_text))


# ── /status ──────────────────────────────────────────────────────────────

async def handle_status(group_id: int, user_id: int, sender: dict,
                        message_id: str, raw_text: str, segments: list,
                        event: dict) -> None:
    """Show bot's current busy/idle status."""
    from .newproblem import get_newproblem_status
    from .submit import get_group_lock_status, get_private_lock_status
    from ...napcat.client import build_plain_message, build_reply, send_group_msg, send_private_msg

    is_private = event.get("message_type") == "private"
    status = get_private_lock_status(user_id, group_id) if is_private else (
        get_newproblem_status(group_id) or get_group_lock_status(group_id)
    )
    if status is None:
        text = "🟢 bot 当前空闲，没有在处理请求～"
        if is_private:
            await send_private_msg(user_id, build_plain_message(text))
        else:
            await send_group_msg(group_id, build_plain_message(text))
        return

    cmd = status["command"]
    if is_private:
        await send_private_msg(user_id, build_plain_message(
            f"🔴 private judge 当前有 {cmd} 请求还在处理中～"
        ))
        return
    if status.get("message_id"):
        await send_group_msg(group_id, build_reply(
            f"🔴 bot 当前有 {cmd} 请求还在处理中～",
            status["message_id"],
        ))
    else:
        await send_group_msg(group_id, build_plain_message(
            f"🔴 bot 当前有 {cmd} 请求还在处理中～"
        ))

# ── Registration ────────────────────────────────────────────────────────

def register() -> None:
    registry.register(CommandDef(
        name="problem", aliases=["pb"],
        description="重新查看当前题目", usage="", handler=handle_problem,
    ))
    registry.register(CommandDef(
        name="tag", aliases=[],
        description="查看当前题目的算法标签", usage="", handler=handle_tag,
    ))
    registry.register(CommandDef(
        name="scoreboard", aliases=[],
        description="累计解题排行", usage="", handler=handle_scoreboard,
    ))
    registry.register(CommandDef(
        name="status", aliases=[],
        description="查看bot当前是否空闲", usage="", handler=handle_status,
    ))
