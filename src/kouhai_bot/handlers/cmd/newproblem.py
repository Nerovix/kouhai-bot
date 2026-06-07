"""/newproblem command — force post a new daily problem.

Also imported by scheduler for the daily 12:00 post (via do_daily_post).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time

from .. import registry
from ..registry import CommandDef
from ..shared import (
    get_today_problem,
    is_already_solved,
    load_scoreboard,
    save_problem_card_ref,
    save_problem_summary,
    save_scoreboard,
    snake_replace,
    summarize_problem,
    translate_sample_notes,
)
from ...config import get_config
from ...context import append_group_ctx
from ...editorial_followup import schedule_prefetch_editorial
from ...user_groups import settle_dynamic_submit_wait_for_problem
from ...napcat.client import (
    build_plain_message,
    react_emoji,
    send_group_msg,
    send_group_forward_msg,
    send_private_msg,
)
from ...problems.picker import _normalize_sample_block
from .submit import run_group_state_update

logger = logging.getLogger("kouhai-bot.cmd.newproblem")

# ── Cooldown ────────────────────────────────────────────────────────────

_cooldowns: dict[int, float] = {}
_newproblem_locks: dict[int, asyncio.Lock] = {}
_newproblem_active: dict[int, dict] = {}


def _has_unsolved_problem(group_id: int) -> bool:
    problem = get_today_problem(group_id)
    return bool(problem) and not is_already_solved(group_id)


def _nickname(sender: dict) -> str:
    return sender.get("card") or sender.get("nickname") or str(sender.get("user_id", "?"))


async def enqueue_force_new_problem(
    group_id: int,
    user_id: int,
    sender: dict,
    message_id: str,
    *,
    command: str,
    force: bool = True,
) -> None:
    """Pick and post a new problem (shared by /newproblem and /newproblem --force)."""
    cfg = get_config()
    nickname = _nickname(sender)
    lock = _newproblem_lock(group_id)
    if lock.locked():
        await send_group_msg(group_id, build_plain_message(
            f"@{nickname} 新的题目正在准备中，别急～"
        ))
        return

    await lock.acquire()
    try:
        now = time.monotonic()
        last = _cooldowns.get(group_id, 0)
        if now - last < cfg.newproblem_cooldown:
            remaining = int(cfg.newproblem_cooldown - (now - last))
            await send_group_msg(group_id, build_plain_message(
                f"@{nickname} 刷新太频繁啦，等 {remaining} 秒再试哦～"
            ))
            return

        if not force and _has_unsolved_problem(group_id):
            await send_group_msg(group_id, build_plain_message(
                f"@{nickname} 当前题目还没有人解出来呢～不能直接刷题哦。\n"
                f"可使用 /problem 查看当前题目。\n"
                f"如果确定要换题，请发 /newproblem --force"
            ))
            return

        logger.info(f"[group_{group_id}] {command} triggered")

        _newproblem_active[group_id] = {
            "group_id": group_id,
            "user_id": user_id,
            "message_id": message_id,
            "command": command,
            "admitted_at": now,
        }
        try:
            posted = await _do_daily_post_locked(
                group_id,
                prefix="刷新了一道新题🌟",
                notify_group=True,
            )
        finally:
            _newproblem_active.pop(group_id, None)
        if posted:
            _cooldowns[group_id] = time.monotonic()
    finally:
        lock.release()

# ── Picker path ─────────────────────────────────────────────────────────

_PICKER_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "kouhai_bot", "problems", "picker.py",
)
_PICKER_PATH = os.path.abspath(os.path.normpath(_PICKER_PATH))
_STATEMENTS_FALLBACK_DIR = os.path.expanduser("~/.kouhai-bot/statements")


def _effective_rating_range(group_id: int) -> tuple[int, int]:
    cfg = get_config()
    min_rating = int(cfg.min_rating)
    max_rating = int(cfg.max_rating)
    try:
        from ...scheduler.engine import load_group_configs

        group_cfg = load_group_configs().get(group_id)
        if group_cfg:
            if group_cfg.min_rating is not None:
                min_rating = int(group_cfg.min_rating)
            if group_cfg.max_rating is not None:
                max_rating = int(group_cfg.max_rating)
    except Exception:
        logger.warning(
            f"[group_{group_id}] Failed to load scheduler rating overrides; using config range",
            exc_info=True,
        )
    return min_rating, max_rating


def _picker_args(command: str, group_id: int, *extra: str) -> list[str]:
    min_rating, max_rating = _effective_rating_range(group_id)
    args = [
        _PICKER_PATH,
        command,
        "--group",
        str(group_id),
        "--min-rating",
        str(min_rating),
        "--max-rating",
        str(max_rating),
    ]
    args.extend(extra)
    return args


def _classify_pick_error(stderr_text: str) -> str:
    """Classify picker stderr into a user-friendly Chinese message."""
    lower = stderr_text.lower()
    # CF-specific connectivity issues
    if any(kw in stderr_text for kw in (
        "codeforces.com", "SSL", "SSLEOFError", "SSLError",
        "ConnectionError", "Max retries exceeded", "RemoteDisconnected",
    )):
        return "Codeforces 连接失败"
    if any(kw in lower for kw in ("timeout", "timed out")):
        return "Codeforces 请求超时"
    if any(kw in lower for kw in ("permission", "access denied", "ioerror", "filenotfound")):
        return "本地数据读取异常"
    # fallback — still log the raw text above
    return "题目选取失败，稍后再试"


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


def _load_statement_json(cfg, group_id: int, pid: str) -> dict:
    """Load statement JSON from runtime dir, then fallback dir."""
    candidate_paths = [
        os.path.join(cfg.data_dir, "statements", f"{pid}.json"),
        os.path.join(_STATEMENTS_FALLBACK_DIR, f"{pid}.json"),
    ]
    for path in candidate_paths:
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                stmt = json.load(f)
            if isinstance(stmt, dict):
                return stmt
            logger.warning(f"[group_{group_id}] Statement at {path} is not a dict")
        except Exception as e:
            logger.warning(f"[group_{group_id}] Failed to load statement {path}: {e}")
    return {}


def _build_sample_messages(stmt: dict) -> list[str]:
    """Build one message per sample."""
    samples = stmt.get("samples")
    if not isinstance(samples, list):
        return []
    lines: list[str] = []
    for idx, sample in enumerate(samples, 1):
        if not isinstance(sample, dict):
            continue
        sample_input = sample.get("input")
        sample_output = sample.get("output")
        if sample_input is None and sample_output is None:
            continue
        normalized_input = _normalize_sample_block(sample_input).rstrip("\n")
        normalized_output = _normalize_sample_block(sample_output).rstrip("\n")
        text = (
            f"样例 {idx}\n"
            f"Input:\n{normalized_input}\n\n"
            f"Output:\n{normalized_output}"
        )
        lines.append(text)
    return lines


async def _build_notes_message(stmt: dict) -> str:
    raw_notes = stmt.get("notes")
    normalized_notes = _normalize_sample_block(raw_notes)
    if not normalized_notes:
        return ""
    try:
        translated_notes, _model_tag = await translate_sample_notes(normalized_notes)
    except Exception as e:
        logger.warning("Notes translation failed, skipping notes node: %s", e)
        return ""
    final_notes = (translated_notes or normalized_notes).strip()
    if not final_notes:
        return ""
    return f"样例解释：\n{final_notes}"


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
        snake_path = os.path.join(os.path.dirname(_PICKER_PATH), "snake_trio.jpg")
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

# ── Daily post ──────────────────────────────────────────────────────────

async def do_daily_post(group_id: int, prefix: str | None = None) -> None:
    """Pick a new problem, summarize, post to group.

    This is the core logic shared between /newproblem and the daily cron.
    """
    async with _newproblem_lock(group_id):
        _newproblem_active[group_id] = {
            "group_id": group_id,
            "user_id": 0,
            "message_id": "",
            "command": "daily_post",
            "admitted_at": time.monotonic(),
        }
        try:
            await _do_daily_post_locked(group_id, prefix)
        finally:
            _newproblem_active.pop(group_id, None)


async def _do_daily_post_locked(
    group_id: int, prefix: str | None = None, *, notify_group: bool = False,
) -> bool:
    cfg = get_config()
    python = sys.executable
    state_dir = os.path.join(cfg.data_dir, "groups", str(group_id))
    os.makedirs(state_dir, exist_ok=True)

    # Step 1: Reveal yesterday's problem
    reveal_text = ""
    try:
        proc = await asyncio.create_subprocess_exec(
            python, *_picker_args("reveal", group_id),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode == 0:
            reveal_text = stdout.decode().strip()
    except Exception as e:
        logger.error(f"Reveal error (group {group_id}): {e}")

    # Step 2: Pick today's problem (w/ retry)
    picked_state: dict = {}
    pick_error_msg = ""
    for attempt in range(1, 4):
        try:
            proc = await asyncio.create_subprocess_exec(
                python, *_picker_args("pick-json", group_id, "--with-statement"),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode != 0:
                err_text = stderr.decode()[:200]
                logger.error(
                    f"Pick failed (group {group_id}, attempt {attempt}/3): {err_text}"
                )
                pick_error_msg = _classify_pick_error(err_text)
            else:
                picked_state = json.loads(stdout.decode())
                if not isinstance(picked_state, dict):
                    logger.error(
                        f"Pick failed (group {group_id}, attempt {attempt}/3): "
                        f"invalid picker payload"
                    )
                    pick_error_msg = "题目选取结果异常"
                    picked_state = {}  # reset to ensure truthiness check catches it
                else:
                    break  # success
        except Exception as e:
            logger.error(
                f"Pick error (group {group_id}, attempt {attempt}/3): {e}"
            )
            pick_error_msg = f"Codeforces 连接失败"
        if attempt < 3:
            delay = 2 * attempt
            logger.info(f"Retrying pick in {delay}s...")
            await asyncio.sleep(delay)

    if not picked_state:
        if notify_group:
            await send_group_msg(group_id, build_plain_message(
                f"刷题失败了…{pick_error_msg}，连试 3 次都没成功，等一会儿再试试吧😢"
            ))
        return False

    # Step 3: Generate Chinese summary
    desc = ""
    model_tag = ""
    pid = str(picked_state.get("today", "") or "")
    sample_messages: list[str] = []
    notes_message = ""
    try:
        if pid:
            schedule_prefetch_editorial(pid)

        stmt: dict = {}
        stmt_text = ""
        input_text = ""
        limits_text = ""
        if pid:
            stmt = _load_statement_json(cfg, group_id, pid)
        if stmt:
            stmt_text = stmt.get("description", "") or ""
            input_text = stmt.get("input", "") or ""
            tl = stmt.get("time_limit", "?")
            ml = stmt.get("memory_limit", "?")
            limits_text = f"Time: {tl}, Memory: {ml}"
            sample_messages = _build_sample_messages(stmt)
            notes_message = await _build_notes_message(stmt)

        summary, model_tag = await summarize_problem(stmt_text, input_text, limits_text)
        if not summary:
            logger.warning(f"[group_{group_id}] Summary 1st attempt failed, retrying...")
            summary, model_tag = await summarize_problem(stmt_text, input_text, limits_text)
        if summary:
            desc = summary.strip()
            save_problem_summary(group_id, pid, desc)
        else:
            logger.warning(f"[group_{group_id}] Summary failed after retry")
    except Exception as e:
        logger.warning(f"[group_{group_id}] Summary error: {e}")

    # Step 4: Compose and deliver via merged-forward card
    greeting = prefix if prefix else "中午好呀☀️ 先前题目已解出，来看看今天的每日一题吧！"
    post_msg = f"{greeting}\n\n{desc}" if desc else greeting
    if model_tag:
        post_msg += model_tag

    if reveal_text and "还没有发过题哦" not in reveal_text:
        post_msg = post_msg + "\n\n" + reveal_text

    post_msg = snake_replace(post_msg)

    fwd_resp, node_payload = await _send_problem_forward_card(
        group_id=group_id,
        post_msg=post_msg,
        sample_messages=sample_messages,
        notes_message=notes_message,
        snake_enabled=True,
    )
    if not fwd_resp:
        logger.error(f"[group_{group_id}] Problem forward-card send failed, falling back to direct")
        ok = await send_group_msg(group_id, build_plain_message(post_msg))
        if ok:
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
            except Exception as e:
                logger.warning(f"[group_{group_id}] Failed to save fallback daily_msg.json: {e}")
            append_group_ctx(group_id, {"role": "assistant", "content": post_msg})
            logger.info(f"[group_{group_id}] Daily post sent (fallback) ✓")
            return True
        else:
            logger.error(f"[group_{group_id}] Daily post send failed")
        return False
    append_group_ctx(group_id, {"role": "assistant", "content": post_msg})
    logger.info(
        f"[group_{group_id}] Daily post forwarded ✓ "
        f"({1 + len(sample_messages) + (1 if node_payload.get('note_msg_id') else 0) + (1 if node_payload.get('snake_msg_id') else 0)} msgs)"
    )
    await _commit_problem_state(group_id, picked_state)
    if pid:
        save_problem_card_ref(group_id, fwd_resp, pid, "daily_post" if prefix is None else "newproblem")

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
    except Exception as e:
        logger.warning(f"[group_{group_id}] Failed to save daily_msg.json: {e}")
    return True


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
        await enqueue_force_new_problem(
            group_id, user_id, sender, message_id, command="newproblem --force",
        )
        return

    if text == _CMD_NEWPROBLEM:
        await enqueue_force_new_problem(
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
