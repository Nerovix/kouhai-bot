"""Private judge state, problem resolution, and private-card helpers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import cloudscraper

from .config import get_config
from .handlers.shared import (
    get_problem_summary,
    get_today_problem,
    high_difficulty_notice,
    load_scoreboard,
    save_problem_summary,
    save_problem_card_ref,
    summarize_problem,
    translate_sample_notes,
)
from .napcat.client import (
    build_plain_message,
    send_group_forward_msg,
    send_group_msg,
    send_private_forward_msg,
    send_private_msg,
)
from .problems.picker import _normalize_sample_block

logger = logging.getLogger("kouhai-bot.private_judge")

TZ = timezone(timedelta(hours=8))
PRIVATE_SCOPE = "private"
GROUP_SCOPE = "group"
PRIVATE_FORWARD_THRESHOLD = 3000
_PRIVATE_STATE_LOAD_WARNED: set[str] = set()

_CF_API = "https://codeforces.com/api/problemset.problems"
_PROBLEM_RE = re.compile(r"^(?:CF)?(\d+)([A-Za-z][A-Za-z0-9]*)$", re.I)
_PROBLEM_PATH_RE = re.compile(
    r"(?:^|/)(?:problemset/)?problem/(\d+)/([A-Za-z0-9]+)(?:[/?#]|$)",
    re.I,
)
_CONTEST_PATH_RE = re.compile(
    r"(?:^|/)contest/(\d+)/problem/([A-Za-z0-9]+)(?:[/?#]|$)",
    re.I,
)


class NonFormulaImageProblem(RuntimeError):
    """Raised when a statement is unavailable because it depends on diagrams/images."""


def _data_dir() -> Path:
    return Path(get_config().data_dir)


def _user_dir(user_id: int) -> Path:
    path = _data_dir() / "private_judge" / "users"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _state_file(user_id: int) -> Path:
    return _user_dir(user_id) / f"{int(user_id)}.json"


def _default_state() -> dict[str, Any]:
    return {
        "current_problem": None,
        "user_submissions": [],
        "solved_problems": {},
        "last_solved_problem": "",
        "notified_group_problem_private": {},
        "problem_cards": {},
    }


def _warn_private_state_load_failure(path: Path, reason: str, exc: Exception | None = None) -> None:
    key = str(path)
    if key in _PRIVATE_STATE_LOAD_WARNED:
        return
    _PRIVATE_STATE_LOAD_WARNED.add(key)
    if exc is None:
        logger.warning("invalid private judge state at %s; using defaults: %s", path, reason)
    else:
        logger.warning(
            "failed to load private judge state at %s; using defaults: %s",
            path,
            reason,
            exc_info=True,
        )


def load_private_state(user_id: int) -> dict[str, Any]:
    path = _state_file(user_id)
    if not path.exists():
        return _default_state()
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            _warn_private_state_load_failure(path, f"expected object, got {type(data).__name__}")
            return _default_state()
    except Exception as e:
        _warn_private_state_load_failure(path, str(e), e)
        return _default_state()

    state = _default_state()
    state.update(data)
    if not isinstance(state.get("user_submissions"), list):
        state["user_submissions"] = []
    if not isinstance(state.get("solved_problems"), dict):
        state["solved_problems"] = {}
    if not isinstance(state.get("notified_group_problem_private"), dict):
        state["notified_group_problem_private"] = {}
    if not isinstance(state.get("problem_cards"), dict):
        state["problem_cards"] = {}
    return state


def save_private_state(user_id: int, state: dict[str, Any]) -> None:
    path = _state_file(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            tmp_name = f.name
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
        raise


def get_private_current_problem(user_id: int) -> dict | None:
    problem = load_private_state(user_id).get("current_problem")
    return problem if isinstance(problem, dict) else None


def set_private_current_problem(user_id: int, problem: dict) -> None:
    state = load_private_state(user_id)
    state["current_problem"] = dict(problem)
    save_private_state(user_id, state)


def get_private_current_pid(user_id: int) -> str:
    problem = get_private_current_problem(user_id)
    return str(problem.get("today", "") or "") if problem else ""


def load_private_submissions(user_id: int) -> list[dict]:
    items = load_private_state(user_id).get("user_submissions", [])
    return [item for item in items if isinstance(item, dict)]


def load_private_problem_history(user_id: int, pid: str) -> list[dict]:
    target = str(pid or "")
    items = [
        item for item in load_private_submissions(user_id)
        if str(item.get("problem", "") or "") == target
    ]
    return sorted(items, key=lambda item: str(item.get("timestamp", "")))


def save_private_submission(user_id: int, submission: dict) -> None:
    state = load_private_state(user_id)
    items = state.setdefault("user_submissions", [])
    request_id = str(submission.get("request_id", "") or "")
    if request_id:
        for idx in range(len(items) - 1, -1, -1):
            existing = items[idx]
            if isinstance(existing, dict) and str(existing.get("request_id", "") or "") == request_id:
                items[idx] = submission
                break
        else:
            items.append(submission)
    else:
        items.append(submission)
    save_private_state(user_id, state)


def replace_private_problem_history(user_id: int, pid: str, records: list[dict]) -> None:
    state = load_private_state(user_id)
    target = str(pid or "")
    kept = [
        item for item in state.get("user_submissions", [])
        if not isinstance(item, dict) or str(item.get("problem", "") or "") != target
    ]
    state["user_submissions"] = kept + [dict(item) for item in records]
    save_private_state(user_id, state)


def replace_private_problem_clarifies(user_id: int, pid: str, records: list[dict]) -> None:
    state = load_private_state(user_id)
    target = str(pid or "")
    kept = []
    for item in state.get("user_submissions", []):
        if not isinstance(item, dict):
            kept.append(item)
            continue
        if str(item.get("problem", "") or "") == target and item.get("type") == "clarify":
            continue
        kept.append(item)
    state["user_submissions"] = kept + [dict(item) for item in records]
    save_private_state(user_id, state)


def clear_private_problem_history(user_id: int, pid: str) -> int:
    state = load_private_state(user_id)
    target = str(pid or "")
    existing = state.get("user_submissions", [])
    kept = [
        item for item in existing
        if not isinstance(item, dict) or str(item.get("problem", "") or "") != target
    ]
    removed = len(existing) - len(kept)
    state["user_submissions"] = kept
    save_private_state(user_id, state)
    return removed


def mark_private_solved(user_id: int, pid: str, *, source: str = "private") -> None:
    target = str(pid or "")
    if not target:
        return
    state = load_private_state(user_id)
    solved = state.setdefault("solved_problems", {})
    if not isinstance(solved.get(target), dict):
        solved[target] = {}
    solved[target].update({
        "timestamp": int(time.time()),
        "source": source,
    })
    state["last_solved_problem"] = target
    save_private_state(user_id, state)


def is_private_solved(user_id: int, pid: str) -> bool:
    solved = load_private_state(user_id).get("solved_problems", {})
    return isinstance(solved, dict) and str(pid or "") in solved


def is_group_problem_solved(group_id: int, pid: str) -> bool:
    target = str(pid or "")
    if not target:
        return False
    sb = load_scoreboard(group_id)
    return any(str(item.get("problem", "") or "") == target for item in sb.get("solves", []))


def get_private_review_pid(user_id: int, group_id: int) -> str:
    current_pid = get_private_current_pid(user_id)
    if current_pid and (is_private_solved(user_id, current_pid) or is_group_problem_solved(group_id, current_pid)):
        return current_pid
    state = load_private_state(user_id)
    last = str(state.get("last_solved_problem", "") or "")
    if last:
        return last
    solved = state.get("solved_problems", {})
    if isinstance(solved, dict) and solved:
        return sorted(solved.keys())[-1]
    return ""


def mark_group_problem_private_notified(user_id: int, pid: str) -> bool:
    """Mark first group-to-private redirect notice for pid.

    Returns True when this is the first mark for this user/problem.
    """
    target = str(pid or "")
    if not target:
        return False
    state = load_private_state(user_id)
    notified = state.setdefault("notified_group_problem_private", {})
    first = not bool(notified.get(target))
    notified[target] = int(time.time())
    save_private_state(user_id, state)
    return first


def has_group_problem_private_notified(user_id: int, pid: str) -> bool:
    target = str(pid or "")
    if not target:
        return False
    notified = load_private_state(user_id).get("notified_group_problem_private", {})
    return isinstance(notified, dict) and bool(notified.get(target))


def parse_problem_ref(text: str) -> tuple[int, str] | None:
    value = (text or "").strip()
    if not value:
        return None
    for pattern in (_CONTEST_PATH_RE, _PROBLEM_PATH_RE):
        match = pattern.search(value)
        if match:
            return int(match.group(1)), match.group(2).upper()
    match = _PROBLEM_RE.fullmatch(value)
    if match:
        return int(match.group(1)), match.group(2).upper()
    return None


def problem_id_from_ref(contest_id: int, index: str) -> str:
    return f"{int(contest_id)}{str(index).upper()}"


def _statement_path(pid: str) -> Path:
    return _data_dir() / "statements" / f"{pid}.json"


def load_statement_json(pid: str) -> dict:
    path = _statement_path(pid)
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _problem_id(problem: dict) -> str:
    contest_id = problem.get("contestId")
    index = problem.get("index")
    if contest_id in (None, "") or not index:
        return ""
    return f"{contest_id}{str(index).upper()}"


def _cached_problem_by_pid(pid: str) -> dict | None:
    for cache_path in sorted(_data_dir().glob("cf_all_*_*.json")):
        try:
            with cache_path.open(encoding="utf-8") as f:
                items = json.load(f)
        except Exception:
            continue
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and _problem_id(item) == pid:
                return item
    return None


def _fetch_problemset() -> list[dict]:
    scraper = cloudscraper.create_scraper()
    resp = scraper.get(_CF_API, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "OK":
        raise RuntimeError(f"CF API error: {data}")
    result = data.get("result", {})
    problems = result.get("problems", [])
    return problems if isinstance(problems, list) else []


def _fetch_problem_by_pid(pid: str) -> dict | None:
    for item in _fetch_problemset():
        if isinstance(item, dict) and _problem_id(item) == pid:
            return item
    return None


def _ensure_statement(problem: dict) -> dict:
    from .problems import picker

    cfg = get_config()
    picker.STATE_DIR = cfg.data_dir
    picker.CACHE_DIR = os.path.join(cfg.data_dir, "statements")
    picker.GROUPS_DIR = os.path.join(cfg.data_dir, "groups")
    os.makedirs(picker.CACHE_DIR, exist_ok=True)
    os.makedirs(picker.GROUPS_DIR, exist_ok=True)
    stmt = picker.fetch_statement(problem)
    if isinstance(stmt, dict):
        return stmt
    if _has_non_formula_images(problem):
        raise NonFormulaImageProblem(_problem_id(problem) or "unknown")
    return {}


def _has_non_formula_images(problem: dict) -> bool:
    from .problems import picker

    contest_id = problem.get("contestId")
    index = problem.get("index")
    if contest_id in (None, "") or not index:
        return False
    try:
        result = picker.cf_statement.process_problem(contest_id, index, vl_backend="none")
    except Exception as e:
        logger.warning(
            "failed to inspect non-formula images for %s%s: %s",
            contest_id,
            index,
            e,
        )
        return False
    return bool(result.get("has_non_formula_images"))


def resolve_problem_by_pid(pid: str) -> dict:
    target = str(pid or "").strip().upper()
    if not target:
        raise ValueError("empty problem id")
    parsed = parse_problem_ref(target)
    if not parsed:
        raise ValueError("bad problem id")
    contest_id, index = parsed
    normalized_pid = problem_id_from_ref(contest_id, index)
    problem = _cached_problem_by_pid(normalized_pid)
    if problem is None:
        try:
            problem = _fetch_problem_by_pid(normalized_pid)
        except Exception as e:
            logger.warning("failed to fetch CF metadata for %s: %s", normalized_pid, e)
            problem = None
    if problem is None:
        problem = {
            "contestId": contest_id,
            "index": index,
            "name": "",
            "rating": "?",
            "tags": [],
        }

    stmt = _ensure_statement(problem)
    if not stmt:
        raise RuntimeError("statement unavailable")

    state = {
        "today": normalized_pid,
        "contestId": contest_id,
        "index": index,
        "name": problem.get("name") or stmt.get("name", ""),
        "rating": problem.get("rating", "?"),
        "tags": problem.get("tags", []),
        "date": datetime.now(TZ).strftime("%Y-%m-%d"),
    }
    return state


def resolve_random_problem(group_id: int) -> dict:
    from .handlers.cmd.newproblem import _effective_rating_range

    min_rating, max_rating = _effective_rating_range(group_id)
    candidates: list[dict] = []
    for item in _fetch_problemset():
        if not isinstance(item, dict):
            continue
        rating = item.get("rating")
        tags = item.get("tags", [])
        if rating is None or not (min_rating <= int(rating) <= max_rating):
            continue
        if "*special" in tags:
            continue
        candidates.append(item)
    if not candidates:
        raise RuntimeError("no random candidates")

    random.shuffle(candidates)
    last_error: Exception | None = None
    for problem in candidates[:20]:
        pid = _problem_id(problem)
        try:
            stmt = _ensure_statement(problem)
            if stmt:
                return {
                    "today": pid,
                    "contestId": problem["contestId"],
                    "index": str(problem["index"]).upper(),
                    "name": problem.get("name", ""),
                    "rating": problem.get("rating", "?"),
                    "tags": problem.get("tags", []),
                    "date": datetime.now(TZ).strftime("%Y-%m-%d"),
                }
        except Exception as e:
            last_error = e
            logger.warning("random private problem %s unavailable: %s", pid, e)
    raise RuntimeError(f"no statement-ready random candidate: {last_error}")


def _build_sample_messages(stmt: dict) -> list[str]:
    samples = stmt.get("samples")
    if not isinstance(samples, list):
        return []
    messages: list[str] = []
    for idx, sample in enumerate(samples, 1):
        if not isinstance(sample, dict):
            continue
        sample_input = _normalize_sample_block(sample.get("input", "")).rstrip("\n")
        sample_output = _normalize_sample_block(sample.get("output", "")).rstrip("\n")
        if not sample_input and not sample_output:
            continue
        messages.append(
            f"样例 {idx}\n"
            f"Input:\n{sample_input}\n\n"
            f"Output:\n{sample_output}"
        )
    return messages


async def _build_notes_message(stmt: dict) -> str:
    raw_notes = _normalize_sample_block(stmt.get("notes", ""))
    if not raw_notes:
        return ""
    try:
        translated, _tag = await translate_sample_notes(raw_notes)
    except Exception:
        translated = ""
    text = (translated or raw_notes).strip()
    return f"样例解释：\n{text}" if text else ""


async def _problem_summary(group_id: int, pid: str, stmt: dict) -> str:
    summary = get_problem_summary(group_id, pid)
    if summary:
        return summary
    stmt_text = stmt.get("description", "") or ""
    input_text = stmt.get("input", "") or ""
    tl = stmt.get("time_limit", "?")
    ml = stmt.get("memory_limit", "?")
    result, model_tag = await summarize_problem(stmt_text, input_text, f"Time: {tl}, Memory: {ml}")
    summary = (result or "").strip()
    if summary:
        if model_tag:
            summary += model_tag
        save_problem_summary(group_id, pid, summary)
    return summary


async def build_problem_card_payload(group_id: int, problem: dict, *, greeting: str = "private judge 题目") -> dict:
    pid = str(problem.get("today", "") or "")
    stmt = _ensure_statement(problem)
    summary = await _problem_summary(group_id, pid, stmt) if stmt else ""
    post_msg = greeting
    if summary:
        post_msg += "\n\n" + summary
    return {
        "pid": pid,
        "post_msg": post_msg,
        "sample_messages": _build_sample_messages(stmt),
        "notes_message": await _build_notes_message(stmt),
        "snake_enabled": False,
    }


def _daily_msg_path(group_id: int) -> Path:
    return _data_dir() / "groups" / str(group_id) / "daily_msg.json"


def load_current_group_card_payload(group_id: int, pid: str) -> dict | None:
    path = _daily_msg_path(group_id)
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict) or str(data.get("pid", "") or "") != str(pid or ""):
        return None
    return data


async def _send_forward_nodes_private(user_id: int, node_ids: list[str]) -> int | None:
    if not node_ids:
        return None
    await asyncio.sleep(0.5)
    return await send_private_forward_msg(
        user_id,
        [{"type": "node", "data": {"id": str(node_id)}} for node_id in node_ids],
    )


async def _send_forward_nodes_group(group_id: int, node_ids: list[str]) -> int | None:
    if not node_ids:
        return None
    await asyncio.sleep(0.5)
    return await send_group_forward_msg(
        group_id,
        [{"type": "node", "data": {"id": str(node_id)}} for node_id in node_ids],
    )


async def _send_high_difficulty_notice_private(user_id: int, problem: dict) -> None:
    notice = high_difficulty_notice(problem)
    if not notice:
        return
    try:
        await send_private_msg(user_id, build_plain_message(notice))
    except Exception as e:
        logger.warning("failed to send private high-difficulty notice: %s", e)


def _save_private_problem_card_ref(group_id: int, message_id: int | None, pid: str) -> None:
    if not message_id or not pid:
        return
    try:
        save_problem_card_ref(group_id, message_id, pid, "private_problem_card")
    except Exception as e:
        logger.warning("failed to save private problem card ref for %s: %s", pid, e)


async def send_problem_card_private(user_id: int, group_id: int, problem: dict, *, prefer_group_card: bool = True) -> bool:
    cfg = get_config()
    pid = str(problem.get("today", "") or "")
    payload = load_current_group_card_payload(group_id, pid) if prefer_group_card else None
    if payload is None:
        try:
            payload = await build_problem_card_payload(group_id, problem)
        except Exception as e:
            logger.warning("failed to build private problem card for %s: %s", pid, e)
            await send_private_msg(user_id, build_plain_message(
                "题目已设置，但题面暂时拉不到，稍后再试试 /problem。"
            ))
            return False

    node_ids: list[str] = []
    msg_id = payload.get("msg_id")
    if msg_id:
        node_ids.append(str(msg_id))
        for sample_id in payload.get("sample_msg_ids", []) if isinstance(payload.get("sample_msg_ids"), list) else []:
            if sample_id:
                node_ids.append(str(sample_id))
        for key in ("note_msg_id", "snake_msg_id"):
            value = payload.get(key)
            if value:
                node_ids.append(str(value))
        fwd_resp = await _send_forward_nodes_private(user_id, node_ids)
        if fwd_resp:
            _save_private_problem_card_ref(group_id, fwd_resp, pid)
            await _send_high_difficulty_notice_private(user_id, problem)
            return True

    post_msg = payload.get("post_msg")
    sample_messages = payload.get("sample_messages")
    notes_message = payload.get("notes_message")
    if not isinstance(post_msg, str):
        post_msg = "当前题目"
    if not isinstance(sample_messages, list):
        sample_messages = []
    if not isinstance(notes_message, str):
        notes_message = ""

    main_node_id = await send_private_msg(cfg.bot_qq, build_plain_message(post_msg))
    node_ids = [str(main_node_id)] if main_node_id else []
    if main_node_id:
        for text in [*[str(item) for item in sample_messages], notes_message]:
            if not text:
                continue
            resp = await send_private_msg(cfg.bot_qq, build_plain_message(text))
            if resp:
                node_ids.append(str(resp))
    if main_node_id:
        fwd_resp = await _send_forward_nodes_private(user_id, node_ids)
        if fwd_resp:
            _save_private_problem_card_ref(group_id, fwd_resp, pid)
            await _send_high_difficulty_notice_private(user_id, problem)
            return True

    direct_id = await send_private_msg(user_id, build_plain_message(post_msg))
    _save_private_problem_card_ref(group_id, direct_id, pid)
    for sample in sample_messages:
        sample_id = await send_private_msg(user_id, build_plain_message(str(sample)))
        _save_private_problem_card_ref(group_id, sample_id, pid)
    if notes_message:
        notes_id = await send_private_msg(user_id, build_plain_message(notes_message))
        _save_private_problem_card_ref(group_id, notes_id, pid)
    await _send_high_difficulty_notice_private(user_id, problem)
    return True


def _one_line(text: Any) -> str:
    return " ".join(str(text or "").split())


def _chunk_text(text: str, size: int = PRIVATE_FORWARD_THRESHOLD) -> list[str]:
    if size <= 0:
        return [text]
    return [text[i:i + size] for i in range(0, len(text), size)] or [text]


def format_history_records(records: list[dict], *, user_display_name: str) -> str:
    name = _one_line(user_display_name) or "这位群友"
    lines = [f"{name}在当前的历史记录如下："]
    for item in records:
        content = _one_line(item.get("content", ""))
        reply = _one_line(item.get("reply", ""))
        if content and reply:
            lines.append(f"👤：{content}")
            lines.append(f"🤖：{reply}")
        elif content:
            lines.append(f"👤：{content}")
        elif reply:
            lines.append(f"🤖：{reply}")
    return "\n".join(lines)


async def send_history_card(
    *,
    destination: str,
    user_id: int,
    group_id: int,
    records: list[dict],
    user_display_name: str,
) -> bool:
    text = format_history_records(records, user_display_name=user_display_name)
    chunks = _chunk_text(text)
    cfg = get_config()
    node_ids: list[str] = []
    for chunk in chunks:
        resp = await send_private_msg(cfg.bot_qq, build_plain_message(chunk))
        if not resp:
            node_ids = []
            break
        node_ids.append(str(resp))
    if node_ids:
        if destination == GROUP_SCOPE:
            if await _send_forward_nodes_group(group_id, node_ids):
                return True
        else:
            if await _send_forward_nodes_private(user_id, node_ids):
                return True
    for chunk in chunks:
        if destination == GROUP_SCOPE:
            await send_group_msg(group_id, build_plain_message(chunk))
        else:
            await send_private_msg(user_id, build_plain_message(chunk))
    return True


def group_problem_history(group_id: int, user_id: int, pid: str) -> list[dict]:
    sb = load_scoreboard(group_id)
    items = sb.get("user_submissions", {}).get(str(user_id), [])
    if not isinstance(items, list):
        return []
    target = str(pid or "")
    return sorted(
        [item for item in items if isinstance(item, dict) and str(item.get("problem", "") or "") == target],
        key=lambda item: str(item.get("timestamp", "")),
    )


def replace_group_problem_history(group_id: int, user_id: int, pid: str, records: list[dict]) -> None:
    from .handlers.shared import save_scoreboard

    sb = load_scoreboard(group_id)
    submissions = sb.setdefault("user_submissions", {})
    uid = str(user_id)
    existing = submissions.get(uid, [])
    if not isinstance(existing, list):
        existing = []
    target = str(pid or "")
    kept = [
        item for item in existing
        if not isinstance(item, dict) or str(item.get("problem", "") or "") != target
    ]
    submissions[uid] = kept + [dict(item) for item in records]
    save_scoreboard(group_id, sb)


def replace_group_problem_clarifies(group_id: int, user_id: int, pid: str, records: list[dict]) -> None:
    from .handlers.shared import save_scoreboard

    sb = load_scoreboard(group_id)
    submissions = sb.setdefault("user_submissions", {})
    uid = str(user_id)
    existing = submissions.get(uid, [])
    if not isinstance(existing, list):
        existing = []
    target = str(pid or "")
    kept = []
    for item in existing:
        if not isinstance(item, dict):
            kept.append(item)
            continue
        if str(item.get("problem", "") or "") == target and item.get("type") == "clarify":
            continue
        kept.append(item)
    submissions[uid] = kept + [dict(item) for item in records]
    save_scoreboard(group_id, sb)


def current_group_pid(group_id: int) -> str:
    current = get_today_problem(group_id)
    return str(current.get("today", "") or "") if current else ""


def private_record_has_correct(records: list[dict], pid: str) -> bool:
    target = str(pid or "")
    return any(
        str(item.get("problem", "") or "") == target
        and item.get("type") == "submit"
        and item.get("result") == "correct"
        for item in records
    )


def copy_records(records: list[dict]) -> list[dict]:
    return [json.loads(json.dumps(item, ensure_ascii=False)) for item in records]
