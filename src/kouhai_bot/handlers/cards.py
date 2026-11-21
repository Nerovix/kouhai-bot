"""Assemble and deliver the problem card (official API).

The legacy merged-forward card becomes one rendered long image (greeting /
summary text, one bubble per sample, the optional translated notes, then the
decorative snake image embedded as a data URI so rendering needs no network).
It is delivered as ONE markdown message: a small fixed-height inline thumbnail
of that image plus the action buttons in the same bubble. Plain text is the
fallback. Shared by /newproblem publication and /problem.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import struct
from pathlib import Path

from ..config import get_config
from ..official.keyboard import problem_action_keyboard
from ..official.sender import OfficialSender
from ..render.browser import RenderBrowser
from ..render.figures import (
    figure_data_uris,
    figure_label,
    figure_rows_html,
    figures_html,
    split_figure_markers,
    strip_figure_markers,
)
from ..render.forward_card import render_card_png, send_forward_card
from ..render.html import text_to_html
from .shared import append_model_tag, record_kind, snake_replace, strip_model_tags

logger = logging.getLogger("kouhai-bot.cards")

_SNAKE_PATH = Path(__file__).resolve().parents[3] / "snake_trio.jpg"

# Bump when the card template/render pipeline changes so stale cached PNGs
# (keyed below) are ignored and rebuilt instead of served forever.
_PROBLEM_CARD_RENDER_VERSION = "4"
_PROBLEM_CARD_TITLE = "题目"

# The card is sent as ONE markdown message: a small inline thumbnail of the
# long image + the action buttons in the same bubble (media images cannot carry
# buttons). The thumbnail keeps a FIXED display height so a longer statement
# does not take more vertical space — the scale shrinks proportionally with the
# image height (owner-approved ~132px tall). Source renders at dsf=2, so the
# natural display size is source/2; we scale that down to the target height.
_PROBLEM_CARD_THUMB_HEIGHT = 132
_THUMB_DEFAULT_HINT = (45, 132)


def _png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        if w > 0 and h > 0:
            return w, h
    return None


def _thumb_hint(w: int, h: int) -> tuple[int, int]:
    """Fixed display height, width proportional to the image aspect ratio."""
    scale = _PROBLEM_CARD_THUMB_HEIGHT / (h / 2)
    return max(1, round((w / 2) * scale)), max(1, round((h / 2) * scale))


def build_problem_post_msg(summary: str, model_tag: str) -> str:
    """The greeting-less problem card lead text: summary + tag, snake applied.

    snake_replace is random, so this is computed ONCE (during background
    pre-render) and the exact result is reused at publication via the cache,
    keeping the rendered image and the persisted card text identical.
    """
    text = summary
    if model_tag:
        text += model_tag
    return snake_replace(text)


def _problem_card_cache_key(
    pid: str, statement_sha256: str, *, bot_name: str, bot_avatar_url: str,
) -> str:
    raw = json.dumps(
        [_PROBLEM_CARD_RENDER_VERSION, pid, statement_sha256, bot_name, bot_avatar_url],
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _problem_card_cache_dir(key: str) -> Path:
    return Path(get_config().data_dir) / "render_cache" / "problem_cards" / key


def load_prerendered_problem_card(
    pid: str, statement_sha256: str, *, bot_name: str, bot_avatar_url: str,
) -> tuple[bytes, str] | None:
    """Return (png_bytes, frozen post_msg) for a pre-rendered card, or None."""
    directory = _problem_card_cache_dir(
        _problem_card_cache_key(pid, statement_sha256, bot_name=bot_name, bot_avatar_url=bot_avatar_url)
    )
    png_path = directory / "card.png"
    meta_path = directory / "meta.json"
    if not png_path.is_file() or not meta_path.is_file():
        return None
    try:
        png = png_path.read_bytes()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("problem card cache unreadable for %s: %s", pid, exc)
        return None
    if not png or not isinstance(meta, dict):
        return None
    return png, str(meta.get("post_msg", "") or "")


async def prerender_problem_card(
    *,
    pid: str,
    statement_sha256: str,
    summary: str,
    model_tag: str,
    sample_messages: list[str] | tuple[str, ...],
    notes_message: str,
    bot_name: str,
    bot_avatar_url: str,
    snake_enabled: bool = True,
    figure_srcs: list[str] | tuple[str, ...] | None = None,
    figure_captions: list[str] | tuple[str, ...] | None = None,
    figure_passage: str = "",
    browser: RenderBrowser | None = None,
) -> bool:
    """Render the problem card long image ahead of time and cache it.

    Best-effort background work: the frozen post_msg (random snake applied
    once) is stored alongside the PNG so publication reuses both verbatim.
    Returns True when a cache entry was written.
    """
    post_msg = build_problem_post_msg(summary, model_tag)
    figure_data = await figure_data_uris(list(figure_srcs or ()))
    nodes = build_problem_card_nodes(
        post_msg=post_msg,
        sample_messages=list(sample_messages),
        notes_message=notes_message,
        snake_enabled=snake_enabled,
        bot_name=bot_name,
        figure_data=figure_data,
        figure_captions=figure_captions,
        figure_passage=figure_passage,
    )
    if not nodes:
        return False
    png = await render_card_png(
        nodes, title=_PROBLEM_CARD_TITLE, browser=browser,
        bot_name=bot_name, bot_avatar_url=bot_avatar_url,
    )
    if not png:
        return False
    directory = _problem_card_cache_dir(
        _problem_card_cache_key(pid, statement_sha256, bot_name=bot_name, bot_avatar_url=bot_avatar_url)
    )
    directory.mkdir(parents=True, exist_ok=True)
    _atomic_write(directory / "card.png", png)
    _atomic_write(
        directory / "meta.json",
        json.dumps({"post_msg": post_msg, "snake_enabled": snake_enabled}, ensure_ascii=False).encode("utf-8"),
    )
    return True


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        with tmp.open("wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


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


def _figure_passage_nodes(
    *,
    passage: str,
    figure_data: list[tuple[int, str]],
    captions: dict[int, str],
    bot_name: str,
) -> list[dict]:
    """Interleave figure images with the prose that refers to them.

    A standalone gallery forced readers to scroll between the explanation and
    the pictures; here each run of ``[[FIG_n]]`` markers renders exactly where
    the statement mentions the figure.
    """
    available = dict(figure_data)
    nodes: list[dict] = []
    pending: list[tuple[int, str]] = []

    def flush_figures() -> None:
        if not pending:
            return
        labels = "、".join(
            figure_label(captions.get(number, ""), number) for number, _uri in pending
        )
        nodes.append({
            "nickname": bot_name,
            "content_html": figure_rows_html(list(pending), captions),
            "content_text": f"配图：{labels}" if labels else "配图",
        })
        pending.clear()

    for kind, value in split_figure_markers(passage):
        if kind == "text":
            text = value.strip()
            if text:
                flush_figures()
                nodes.append(
                    {"nickname": bot_name, "content_html": text_to_html(text), "content_text": text}
                )
            continue
        number = int(value)
        if number in available:
            pending.append((number, available[number]))
    flush_figures()
    return nodes


def build_problem_card_nodes(
    *,
    post_msg: str,
    sample_messages: list[str],
    notes_message: str = "",
    snake_enabled: bool = True,
    bot_name: str = "KouhaiBot",
    figure_data: list[tuple[int, str]] | tuple[tuple[int, str], ...] | None = None,
    figure_captions: list[str] | tuple[str, ...] | None = None,
    figure_passage: str = "",
) -> list[dict]:
    nodes = []
    if post_msg.strip():
        nodes.append(
            {"nickname": bot_name, "content_html": text_to_html(post_msg), "content_text": post_msg}
        )
    figure_data = list(figure_data or [])
    captions = {
        index: str(caption)
        for index, caption in enumerate(list(figure_captions or ()), 1)
        if str(caption).strip()
    }
    notes_has_figures = bool(figure_data) and any(
        kind == "figure" for kind, _value in split_figure_markers(notes_message)
    )
    if figure_data and figure_passage.strip():
        nodes.extend(
            _figure_passage_nodes(
                passage=figure_passage,
                figure_data=figure_data,
                captions=captions,
                bot_name=bot_name,
            )
        )
    elif figure_data and not notes_has_figures:
        # Legacy fallback: figures without a placeable passage cannot sit next
        # to their context, so keep them in one trailing block.
        nodes.append(
            {
                "nickname": bot_name,
                "content_html": "<h3>题目配图</h3>" + figures_html(figure_data),
                "content_text": f"题目配图（{len(figure_data)} 张，见卡片长图）",
            }
        )
    for sample in sample_messages:
        text = str(sample)
        nodes.append(
            {"nickname": bot_name, "content_html": text_to_html(text), "content_text": text}
        )
    if notes_message:
        if notes_has_figures:
            # Sample explanations can embed their own figures ("The tree in
            # the first test: <img>"); render them where the text says so.
            nodes.extend(
                _figure_passage_nodes(
                    passage=notes_message,
                    figure_data=figure_data,
                    captions=captions,
                    bot_name=bot_name,
                )
            )
        else:
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
    figure_count: int = 0, figure_passage: str = "",
) -> str:
    parts = [post_msg]
    notes_marked = any(
        kind == "figure" for kind, _value in split_figure_markers(notes_message)
    )
    if figure_passage.strip():
        parts.append(strip_figure_markers(figure_passage))
    elif figure_count > 0 and not notes_marked:
        parts.append(f"（本题含 {figure_count} 张配图，请在题目卡片长图中查看）")
    parts.extend(str(s) for s in sample_messages)
    if notes_message:
        notes_text = strip_figure_markers(notes_message)
        if notes_text.strip():
            parts.append(notes_text)
    return "\n\n".join(p for p in parts if p.strip())


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
    figure_srcs: list[str] | tuple[str, ...] | None = None,
    figure_captions: list[str] | tuple[str, ...] | None = None,
    figure_passage: str = "",
    msg_id: str | None = None,
    browser: RenderBrowser | None = None,
    prerendered_png: bytes | None = None,
) -> str | None:
    bot_name = sender.bot_name or "KouhaiBot"
    figure_data = await figure_data_uris(list(figure_srcs or ()))
    nodes = build_problem_card_nodes(
        post_msg=post_msg,
        sample_messages=list(sample_messages),
        notes_message=notes_message,
        snake_enabled=snake_enabled,
        bot_name=bot_name,
        figure_data=figure_data,
        figure_captions=figure_captions,
        figure_passage=figure_passage,
    )
    fallback = problem_card_text(
        post_msg=post_msg, sample_messages=list(sample_messages),
        notes_message=notes_message, figure_count=len(figure_data),
        figure_passage=figure_passage,
    )
    png = prerendered_png
    if png is None:
        try:
            png = await render_card_png(
                nodes, title=_PROBLEM_CARD_TITLE, browser=browser,
                bot_name=bot_name, bot_avatar_url=sender.bot_avatar_url,
            )
        except Exception as e:
            logger.warning("problem card render failed; using text fallback: %s", e)
    if png:
        uploaded = await sender.upload_markdown_image(png, file_name="problem_card.png")
        if uploaded:
            url, image_id = uploaded
            size = _png_size(png)
            hw, hh = _thumb_hint(*size) if size else _THUMB_DEFAULT_HINT
            # No CF pid/title in the markdown: the card must not leak the
            # original problem identity (anti-spoiler). The rendered header
            # already carries the generic title.
            md = f"![{_PROBLEM_CARD_TITLE} #{hw}px #{hh}px]({url})"
            try:
                mid = await sender.send_group_markdown(
                    group_openid, md, keyboard=problem_action_keyboard(), msg_id=msg_id,
                )
            except Exception as e:
                logger.warning("problem card markdown send failed: %s", e)
                mid = None
            if mid:
                # Delivered messages keep a platform copy; deleting the source
                # frees the 100/bot media quota without affecting the card.
                await sender.delete_markdown_image(image_id)
                return mid
    return await sender.send_group_text(group_openid, fallback, msg_id=msg_id)


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
    bot_name = sender.bot_name or "KouhaiBot"
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