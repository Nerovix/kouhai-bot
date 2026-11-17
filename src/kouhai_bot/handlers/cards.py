"""Assemble and deliver the problem card as a long image (official API).

The legacy merged-forward card becomes one rendered long image: greeting /
summary text, one bubble per sample, the optional translated notes, then the
decorative snake image (embedded as a data URI so rendering needs no network).
Plain text is the fallback. Shared by /newproblem publication and /problem.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from ..official.sender import OfficialSender
from ..render.browser import RenderBrowser
from ..render.forward_card import send_forward_card
from ..render.html import text_to_html
from .shared import append_model_tag, record_kind, snake_replace, strip_model_tags

logger = logging.getLogger("kouhai-bot.cards")

_SNAKE_PATH = Path(__file__).resolve().parents[3] / "snake_trio.jpg"


def _snake_node(bot_name: str) -> dict | None:
    if not _SNAKE_PATH.is_file():
        return None
    try:
        encoded = base64.b64encode(_SNAKE_PATH.read_bytes()).decode()
    except OSError as exc:
        logger.warning("snake image node skipped: %s", exc)
        return None
    return {
        "nickname": bot_name,
        "content_html": f'<img src="data:image/jpeg;base64,{encoded}">',
        "content_text": "🐍",
    }


def build_problem_card_nodes(
    *,
    post_msg: str,
    sample_messages: list[str],
    notes_message: str = "",
    snake_enabled: bool = True,
    bot_name: str = "算法助手",
) -> list[dict]:
    nodes = [
        {"nickname": bot_name, "content_html": text_to_html(post_msg), "content_text": post_msg},
    ]
    for sample in sample_messages:
        text = str(sample)
        nodes.append(
            {"nickname": bot_name, "content_html": text_to_html(text), "content_text": text}
        )
    if notes_message:
        nodes.append(
            {"nickname": bot_name, "content_html": text_to_html(notes_message), "content_text": notes_message}
        )
    if snake_enabled:
        snake = _snake_node(bot_name)
        if snake is not None:
            nodes.append(snake)
    return nodes


def problem_card_text(
    *, post_msg: str, sample_messages: list[str], notes_message: str = "",
) -> str:
    parts = [post_msg, *[str(s) for s in sample_messages]]
    if notes_message:
        parts.append(notes_message)
    return "\n\n".join(parts)


def apply_snake(text: str) -> str:
    return snake_replace(text)


async def send_problem_card(
    sender: OfficialSender,
    group_openid: str,
    *,
    post_msg: str,
    sample_messages: list[str],
    notes_message: str = "",
    snake_enabled: bool = True,
    msg_id: str | None = None,
    browser: RenderBrowser | None = None,
) -> str | None:
    bot_name = sender.bot_name or "算法助手"
    nodes = build_problem_card_nodes(
        post_msg=post_msg,
        sample_messages=list(sample_messages),
        notes_message=notes_message,
        snake_enabled=snake_enabled,
        bot_name=bot_name,
    )
    fallback = problem_card_text(
        post_msg=post_msg, sample_messages=list(sample_messages), notes_message=notes_message,
    )
    return await send_forward_card(
        sender, group_openid, nodes, title="题目",
        msg_id=msg_id, text_fallback=fallback, browser=browser,
    )


def _bot_reply_text(record: dict) -> str:
    result = str(record.get("result", "") or "")
    reply = strip_model_tags(str(record.get("reply", "") or ""))
    reason = strip_model_tags(str(record.get("reason", "") or ""))
    tag = str(record.get("model_tag", "") or "")
    if reply:
        return append_model_tag(reply, tag)
    if result == "incorrect":
        return append_model_tag(f"{reason or '做法不太对呢'}。再想想？🤔", tag)
    if reason:
        return append_model_tag(reason, tag)
    return ""


def build_history_card_nodes(
    records: list[dict], *, user_name: str, bot_name: str, user_avatar_url: str = "",
) -> list[dict]:
    """Per-sender chat-record nodes: user bubble + bot bubble per interaction.

    Source group is deliberately not shown (cross-group sync is identity-blind).
    Unanswered pending/superseded records render the user bubble only.
    """
    nodes: list[dict] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        content = str(record.get("content", "") or "")
        if content:
            nodes.append(
                {
                    "nickname": user_name,
                    "content_html": text_to_html(content),
                    "content_text": content,
                    "avatar_url": user_avatar_url,
                }
            )
        if str(record.get("result", "") or "") in {"pending", "superseded"}:
            continue
        bot_text = _bot_reply_text(record)
        if bot_text:
            nodes.append(
                {"nickname": bot_name, "content_html": text_to_html(bot_text), "content_text": bot_text}
            )
    return nodes


def history_card_text(records: list[dict], *, user_name: str, bot_name: str) -> str:
    lines: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        content = str(record.get("content", "") or "")
        if content:
            lines.append(f"{user_name}：{content}")
        if str(record.get("result", "") or "") in {"pending", "superseded"}:
            continue
        bot_text = _bot_reply_text(record)
        if bot_text:
            lines.append(f"{bot_name}：{bot_text}")
    return "\n\n".join(lines)


async def send_history_card(
    sender: OfficialSender,
    group_openid: str,
    records: list[dict],
    *,
    user_name: str,
    user_avatar_url: str = "",
    msg_id: str | None = None,
    browser: RenderBrowser | None = None,
) -> str | None:
    bot_name = sender.bot_name or "算法助手"
    nodes = build_history_card_nodes(
        records, user_name=user_name, bot_name=bot_name, user_avatar_url=user_avatar_url,
    )
    if not nodes:
        return None
    fallback = history_card_text(records, user_name=user_name, bot_name=bot_name)
    return await send_forward_card(
        sender, group_openid, nodes, title="聊天记录",
        msg_id=msg_id, text_fallback=fallback, browser=browser,
    )