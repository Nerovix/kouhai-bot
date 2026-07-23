"""/newproblem command — post a new problem on demand."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from .. import registry
from ..registry import CommandDef
from ..shared import (
    get_today_problem,
    high_difficulty_notice,
    is_already_solved,
    load_scoreboard,
    save_problem_card_ref,
    save_problem_summary,
    save_scoreboard,
    snake_replace,
)
from ...config import get_config
from ...context import append_group_ctx
from ...editorial_followup import schedule_prefetch_editorial
from ...napcat.client import (
    build_plain_message,
    react_emoji,
    send_group_msg,
    send_group_forward_msg,
    send_private_msg,
)
from ...problem_prefetch import get_next_problem_prefetcher
from ...problem_preparation import (
    PICKER_PATH,
    ProblemPreparationError,
    format_previous_problem_reveal,
)
from ...user_groups import settle_dynamic_submit_wait_for_problem
from .submit import run_group_state_update

logger = logging.getLogger("kouhai-bot.cmd.newproblem")
TZ = timezone(timedelta(hours=8))

# ── Cooldown ────────────────────────────────────────────────────────────

_cooldowns: dict[int, float] = {}
_newproblem_locks: dict[int, asyncio.Lock] = {}
_newproblem_active: dict[int, dict] = {}


def _has_unsolved_problem(group_id: int) -> bool:
    problem = get_today_problem(group_id)
    return bool(problem) and not is_already_solved(group_id)


def _nickname(sender: dict) -> str:
    return sender.get("card") or sender.get("nickname") or str(sender.get("user_id", "?"))


async def _send_high_difficulty_notice_group(group_id: int, problem: dict | None) -> None:
    notice = high_difficulty_notice(problem)
    if not notice:
        return
    try:
        await send_group_msg(group_id, build_plain_message(notice))
    except Exception as e:
        logger.warning("[group_%s] Failed to send high-difficulty notice: %s", group_id, e)


async def enqueue_new_problem(
    group_id: int,
    user_id: int,
    sender: dict | None,
    message_id: str,
    *,
    command: str,
    force: bool = True,
    quiet: bool = False,
    prefix: str = "刷新了一道新题🌟",
) -> bool:
    """Admit, pick, and post a new problem for commands or quiet triggers."""
    cfg = get_config()
    nickname = _nickname(sender or {})
    lock = _newproblem_lock(group_id)
    if lock.locked():
        if not quiet:
            await send_group_msg(group_id, build_plain_message(
                f"@{nickname} 新的题目正在准备中，别急～"
            ))
        return False

    await lock.acquire()
    try:
        now = time.monotonic()
        last = _cooldowns.get(group_id, 0)
        if now - last < cfg.newproblem_cooldown:
            if not quiet:
                remaining = int(cfg.newproblem_cooldown - (now - last))
                await send_group_msg(group_id, build_plain_message(
                    f"@{nickname} 刷新太频繁啦，等 {remaining} 秒再试哦～"
                ))
            return False

        if not force and _has_unsolved_problem(group_id):
            if not quiet:
                await send_group_msg(group_id, build_plain_message(
                    f"@{nickname} 当前题目还没有人解出来呢～不能直接刷题哦。\n"
                    f"可使用 /problem 查看当前题目。\n"
                    f"如果确定要换题，请发 /newproblem --force"
                ))
            return False

        logger.info(f"[group_{group_id}] {command} triggered")

        _newproblem_active[group_id] = {
            "group_id": group_id,
            "user_id": user_id,
            "message_id": message_id,
            "command": command,
            "admitted_at": now,
        }
        try:
            posted = await _post_new_problem_locked(
                group_id,
                prefix=prefix,
                notify_group=not quiet,
            )
        except Exception:
            if not quiet:
                raise
            logger.exception("[group_%s] %s post failed", group_id, command)
            posted = False
        finally:
            _newproblem_active.pop(group_id, None)
        if posted:
            _cooldowns[group_id] = time.monotonic()
        return posted
    finally:
        lock.release()

def _newproblem_lock(group_id: int) -> asyncio.Lock:
    lock = _newproblem_locks.get(group_id)
    if lock is None:
        lock = asyncio.Lock()
        _newproblem_locks[group_id] = lock
    return lock


def get_newproblem_status(group_id: int) -> dict | None:
    return _newproblem_active.get(group_id)


async def _commit_problem_state(group_id: int, state: dict) -> None:
    cfg = get_config()
    state_dir = os.path.join(cfg.data_dir, "groups", str(group_id))
    os.makedirs(state_dir, exist_ok=True)
    committed = dict(state)
    committed["posted_at"] = int(time.time())

    def _write() -> None:
        try:
            previous = get_today_problem(group_id)
            previous_pid = str(previous.get("today", "") or "") if previous else ""
            if previous_pid:
                sb = load_scoreboard(group_id)
                if settle_dynamic_submit_wait_for_problem(sb, previous_pid):
                    save_scoreboard(group_id, sb)
            with open(os.path.join(state_dir, "state.json"), "w", encoding="utf-8") as f:
                json.dump(committed, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.error("[group_%s] Failed to write state.json: %s", group_id, e)

    await run_group_state_update(group_id, _write)


def _save_daily_msg(
    state_dir: str,
    *,
    pid: str,
    post_msg: str,
    sample_messages: list[str],
    notes_message: str,
    snake_enabled: bool,
    node_payload: dict | None = None,
    fwd_message_id: int | None = None,
) -> None:
    daily_msg = {
        **(node_payload or {}),
        "pid": pid,
        "post_msg": post_msg,
        "sample_messages": sample_messages,
        "notes_message": notes_message,
        "snake_enabled": snake_enabled,
    }
    if fwd_message_id is not None:
        daily_msg["fwd_message_id"] = fwd_message_id
    daily_msg_path = os.path.join(state_dir, "daily_msg.json")
    with open(daily_msg_path, "w", encoding="utf-8") as f:
        json.dump(daily_msg, f, ensure_ascii=False, indent=2)


async def _send_problem_forward_card(
    group_id: int,
    post_msg: str,
    sample_messages: list[str],
    notes_message: str = "",
    snake_enabled: bool = True,
) -> tuple[int | None, dict]:
    cfg = get_config()
    self_resp = await send_private_msg(cfg.bot_qq, build_plain_message(post_msg))
    if not self_resp:
        return None, {}

    snake_msg_id = None
    if snake_enabled:
        snake_path = str(PICKER_PATH.parent / "snake_trio.jpg")
        if os.path.exists(snake_path):
            try:
                import base64
                with open(snake_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                snake_resp = await send_private_msg(cfg.bot_qq, [
                    {"type": "image", "data": {"file": f"base64://{b64}"}},
                ])
                if snake_resp:
                    snake_msg_id = snake_resp
            except Exception as e:
                logger.warning(f"[group_{group_id}] Snake image self-send failed: {e}")

    sample_msg_ids: list[int] = []
    for i, sample_msg in enumerate(sample_messages, 1):
        sample_resp = await send_private_msg(cfg.bot_qq, build_plain_message(sample_msg))
        if sample_resp:
            sample_msg_ids.append(sample_resp)
        else:
            logger.warning(f"[group_{group_id}] Sample {i} self-send failed")

    note_msg_id = None
    if notes_message:
        note_resp = await send_private_msg(cfg.bot_qq, build_plain_message(notes_message))
        if note_resp:
            note_msg_id = note_resp
        else:
            logger.warning(f"[group_{group_id}] Notes self-send failed")

    await asyncio.sleep(0.5)
    fwd_nodes = [{"type": "node", "data": {"id": str(self_resp)}}]
    for sample_msg_id in sample_msg_ids:
        fwd_nodes.append({"type": "node", "data": {"id": str(sample_msg_id)}})
    if note_msg_id:
        fwd_nodes.append({"type": "node", "data": {"id": str(note_msg_id)}})
    if snake_msg_id:
        fwd_nodes.append({"type": "node", "data": {"id": str(snake_msg_id)}})
    fwd_resp = await send_group_forward_msg(group_id, fwd_nodes)
    payload = {
        "msg_id": self_resp,
        "sample_msg_ids": sample_msg_ids,
        "note_msg_id": note_msg_id,
        "snake_msg_id": snake_msg_id,
    }
    return fwd_resp, payload

# ── New problem posting ─────────────────────────────────────────────────


async def _post_new_problem_locked(
    group_id: int, prefix: str | None = None, *, notify_group: bool = False,
) -> bool:
    cfg = get_config()
    state_dir = os.path.join(cfg.data_dir, "groups", str(group_id))
    os.makedirs(state_dir, exist_ok=True)
    prefetcher = get_next_problem_prefetcher(group_id)
    try:
        slot = await prefetcher.claim()
    except ProblemPreparationError as exc:
        if notify_group:
            await send_group_msg(group_id, build_plain_message(
                f"刷题失败了…{exc.user_message}，连试 3 次都没成功，等一会儿再试试吧😢"
            ))
        return False

    try:
        prepared = slot.problem
        picked_state = dict(prepared.state)
        picked_state["date"] = datetime.now(TZ).strftime("%Y-%m-%d")
        pid = prepared.pid
        desc = prepared.summary
        model_tag = prepared.model_tag
        sample_messages = list(prepared.sample_messages)
        notes_message = prepared.notes_message
        reveal_text = format_previous_problem_reveal(get_today_problem(group_id))

        # Preserve the original publication-time trigger.  Normally this
        # deduplicates against preparation; after a process restart it resumes
        # a crawler that may have died while the durable slot survived.
        try:
            schedule_prefetch_editorial(pid)
        except Exception as exc:
            logger.warning(
                "[group_%s] Failed to schedule editorial prefetch for %s: %s",
                group_id,
                pid,
                exc,
            )

        if desc:
            try:
                save_problem_summary(
                    group_id,
                    pid,
                    desc,
                    source_sha256=prepared.statement_sha256,
                )
            except Exception as exc:
                # Summary persistence was best-effort in the original path;
                # a cache write must not prevent an otherwise ready card.
                logger.warning(
                    "[group_%s] Failed to save problem summary for %s: %s",
                    group_id,
                    pid,
                    exc,
                )

        greeting = prefix if prefix else "来看看这道新题吧！"
        post_msg = f"{greeting}\n\n{desc}" if desc else greeting
        if model_tag:
            post_msg += model_tag
        if reveal_text and "还没有发过题哦" not in reveal_text:
            post_msg += "\n\n" + reveal_text
        post_msg = snake_replace(post_msg)

        fwd_resp, node_payload = await _send_problem_forward_card(
            group_id=group_id,
            post_msg=post_msg,
            sample_messages=sample_messages,
            notes_message=notes_message,
            snake_enabled=True,
        )
        if not fwd_resp:
            logger.error(
                "[group_%s] Problem forward-card send failed, falling back to direct",
                group_id,
            )
            ok = await send_group_msg(group_id, build_plain_message(post_msg))
            if not ok:
                logger.error("[group_%s] New problem post send failed", group_id)
                return False

            await _send_high_difficulty_notice_group(group_id, picked_state)
            await _commit_problem_state(group_id, picked_state)
            try:
                _save_daily_msg(
                    state_dir,
                    pid=pid,
                    post_msg=post_msg,
                    sample_messages=sample_messages,
                    notes_message=notes_message,
                    snake_enabled=True,
                    node_payload=node_payload,
                )
            except Exception as exc:
                logger.warning(
                    "[group_%s] Failed to save fallback daily_msg.json: %s",
                    group_id,
                    exc,
                )
            append_group_ctx(group_id, {"role": "assistant", "content": post_msg})
            logger.info("[group_%s] New problem post sent (fallback) ✓", group_id)
            return True

        append_group_ctx(group_id, {"role": "assistant", "content": post_msg})
        logger.info(
            "[group_%s] New problem post forwarded ✓ (%s msgs)",
            group_id,
            1
            + len(sample_messages)
            + (1 if node_payload.get("note_msg_id") else 0)
            + (1 if node_payload.get("snake_msg_id") else 0),
        )
        await _commit_problem_state(group_id, picked_state)
        if pid:
            save_problem_card_ref(group_id, fwd_resp, pid, "newproblem")
        await _send_high_difficulty_notice_group(group_id, picked_state)

        try:
            _save_daily_msg(
                state_dir,
                pid=pid,
                post_msg=post_msg,
                sample_messages=sample_messages,
                notes_message=notes_message,
                snake_enabled=True,
                node_payload=node_payload,
                fwd_message_id=fwd_resp,
            )
        except Exception as exc:
            logger.warning(
                "[group_%s] Failed to save daily_msg.json: %s",
                group_id,
                exc,
            )
        return True
    finally:
        # A cancelled command must not strand the coordinator in CLAIMED.
        release_task = asyncio.create_task(prefetcher.release(slot.slot_id))
        try:
            await asyncio.shield(release_task)
        except asyncio.CancelledError:
            with suppress(asyncio.CancelledError):
                await release_task
            raise


# ── Command handler ─────────────────────────────────────────────────────

_CMD_NEWPROBLEM = "/newproblem"
_CMD_NEWPROBLEM_FORCE = "/newproblem --force"


async def handle(group_id: int, user_id: int, sender: dict,
                 message_id: str, raw_text: str, segments: list,
                 event: dict) -> None:
    """Handle /newproblem and exact ``/newproblem --force``."""
    import random
    await react_emoji(message_id, random.choice(["128064", "289"]))

    text = raw_text.lstrip()
    if text == _CMD_NEWPROBLEM_FORCE:
        await enqueue_new_problem(
            group_id, user_id, sender, message_id, command="newproblem --force",
        )
        return

    if text == _CMD_NEWPROBLEM:
        await enqueue_new_problem(
            group_id, user_id, sender, message_id, command="newproblem", force=False,
        )
        return

    parts = text.split()
    head = parts[0] if parts else ""
    if head == _CMD_NEWPROBLEM and len(parts) > 1:
        nickname = _nickname(sender)
        await send_group_msg(group_id, build_plain_message(
            f"@{nickname} 用法：/newproblem 刷题，/newproblem --force 强制换题"
        ))
    elif head[: len(_CMD_NEWPROBLEM)] == _CMD_NEWPROBLEM and head != _CMD_NEWPROBLEM:
        nickname = _nickname(sender)
        await send_group_msg(group_id, build_plain_message(
            f"@{nickname} 用法：/newproblem 刷题，/newproblem --force 强制换题"
        ))


def register() -> None:
    registry.register(CommandDef(
        name="newproblem",
        aliases=["np"],
        description="刷一道新题（未解须 --force；冷却见配置）",
        usage="[--force]",
        handler=handle,
        cooldown=0,
    ))
