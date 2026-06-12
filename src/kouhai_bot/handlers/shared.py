"""Shared handler logic — configured LLM API, judge, problem loading, utilities.

Extracted from old bridge.py. Used by command handlers and scheduler.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import re
import time
from pathlib import Path

from ..config import get_config
from ..llm import ChatCompletionResult, chat_completion

logger = logging.getLogger("kouhai-bot.shared")

HIGH_DIFFICULTY_RATING_THRESHOLD = 2800
HIGH_DIFFICULTY_NOTICE = (
    "这道题的难度较高，bot 的推理能力可能受限。"
    "如果你觉得 bot 说得不对，请查看题解～"
)

# ── LLM API ─────────────────────────────────────────────────────────────


async def call_chat_completion_result(
    messages: list[dict],
    model: str = "",
    task: str = "",
    temperature: float = 0.7,
    timeout: int = 120,
    response_format: dict | None = None,
    thinking: dict | None = None,
) -> ChatCompletionResult:
    """Call the configured chat-completions provider and keep terminal failure metadata."""
    return await chat_completion(
        messages,
        model=model,
        task=task,
        temperature=temperature,
        timeout=timeout,
        response_format=response_format,
        thinking=thinking,
    )


async def call_chat_completion(
    messages: list[dict],
    model: str = "",
    task: str = "",
    temperature: float = 0.7,
    timeout: int = 120,
    response_format: dict | None = None,
    thinking: dict | None = None,
) -> str | None:
    """Call the configured chat-completions provider. Returns response text or None."""
    result = await call_chat_completion_result(
        messages,
        model=model,
        task=task,
        temperature=temperature,
        timeout=timeout,
        response_format=response_format,
        thinking=thinking,
    )
    return result.text


# ── Snake emoji replacement ─────────────────────────────────────────────

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\u2600-\u27BF"
    "\u2B50"
    "]",
    re.UNICODE,
)

def snake_replace(text: str) -> str:
    """10% chance to replace each emoji with 🐍."""
    def _replacer(m):
        return "🐍" if random.random() < 0.1 else m.group(0)
    return _EMOJI_RE.sub(_replacer, text)


# ── JSON parsing ────────────────────────────────────────────────────────

def robust_json_parse(text: str) -> dict:
    """Parse JSON, handling markdown fences, trailing commas, and more."""
    if not text:
        return {}
    # Strip markdown fences
    text = re.sub(r'^```(?:json)?\s*\n?', '', text.strip())
    text = re.sub(r'\n?```\s*$', '', text.strip())
    # Find first { and last }
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end == -1 or start >= end:
        logger.warning(f"robust_json_parse: no JSON object found in: {text[:200]}")
        return {}
    json_str = text[start:end + 1]
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        # Try ast.literal_eval as fallback
        try:
            import ast
            return ast.literal_eval(json_str)
        except Exception:
            pass
        # Last resort: regex extract key fields
        result = {}
        for field in ['correct', 'reason', 'reply']:
            m = re.search(rf'"{field}"\s*:\s*(true|false|"[^"]*")', json_str)
            if m:
                val = m.group(1)
                if val == 'true':
                    result[field] = True
                elif val == 'false':
                    result[field] = False
                else:
                    result[field] = val.strip('"')
        if result:
            logger.warning(f"robust_json_parse: regex fallback → {result}")
        return result


# ── Problem statement loading ───────────────────────────────────────────

def load_problem_statement(pid: str) -> str:
    """Load full problem statement from cache, formatted for LLM."""
    cfg = get_config()
    stmt_path = os.path.join(cfg.data_dir, "statements", f"{pid}.json")
    if not os.path.exists(stmt_path):
        return ""

    with open(stmt_path) as f:
        stmt = json.load(f)

    parts = []
    if stmt.get("name"):
        parts.append(f"Problem: {stmt['name']}")
    if stmt.get("time_limit"):
        parts.append(f"Time limit: {stmt['time_limit']}")
    if stmt.get("memory_limit"):
        parts.append(f"Memory limit: {stmt['memory_limit']}")

    desc = stmt.get("description", "")
    if desc:
        parts.append(f"\nDescription:\n{desc}")

    inp = stmt.get("input", "")
    if inp:
        parts.append(f"\nInput:\n{inp}")

    output = stmt.get("output", "")
    if output:
        parts.append(f"\nOutput:\n{output}")

    samples = stmt.get("samples", [])
    if samples:
        for s in samples:
            parts.append(f"\nInput:\n{s['input']}\nOutput:\n{s['output']}")

    notes = stmt.get("notes", "")
    if notes:
        parts.append(f"\nNote:\n{notes}")

    return "\n".join(parts)


# ── Today's problem ─────────────────────────────────────────────────────

def _today_state_file(group_id: int) -> str:
    cfg = get_config()
    d = os.path.join(cfg.data_dir, "groups", str(group_id))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "state.json")


def _problem_summary_file(group_id: int) -> str:
    cfg = get_config()
    d = os.path.join(cfg.data_dir, "groups", str(group_id))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "problem_summaries.json")


def _problem_card_refs_file(group_id: int) -> str:
    cfg = get_config()
    d = os.path.join(cfg.data_dir, "groups", str(group_id))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "problem_card_refs.json")


def get_today_problem(group_id: int) -> dict | None:
    """Get today's problem info from state file."""
    path = _today_state_file(group_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _daily_msg_file(group_id: int) -> str:
    cfg = get_config()
    d = os.path.join(cfg.data_dir, "groups", str(group_id))
    return os.path.join(d, "daily_msg.json")


def get_problem_posted_at(group_id: int) -> int | None:
    """Unix timestamp when the current problem was posted to the group."""
    state = get_today_problem(group_id)
    if not state:
        return None
    raw = state.get("posted_at")
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass

    pid = str(state.get("today", "") or "")
    if not pid:
        return None
    daily_path = _daily_msg_file(group_id)
    if not os.path.exists(daily_path):
        return None
    try:
        with open(daily_path, encoding="utf-8") as f:
            daily = json.load(f)
        if str(daily.get("pid", "") or "") != pid:
            return None
        return int(os.path.getmtime(daily_path))
    except Exception:
        return None


def mark_problem_posted(group_id: int, posted_at: int | None = None) -> None:
    """Record when today's problem card was delivered to the group."""
    path = _today_state_file(group_id)
    if not os.path.exists(path):
        return
    try:
        with open(path) as f:
            state = json.load(f)
        if not isinstance(state, dict):
            return
        state["posted_at"] = int(time.time() if posted_at is None else posted_at)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        return


def load_problem_summaries(group_id: int) -> dict:
    path = _problem_summary_file(group_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_problem_summary(group_id: int, pid: str) -> str:
    data = load_problem_summaries(group_id)
    item = data.get(pid)
    if isinstance(item, dict):
        return item.get("summary_zh", "") or ""
    if isinstance(item, str):
        return item
    return ""


def save_problem_summary(group_id: int, pid: str, summary_zh: str) -> None:
    if not pid or not summary_zh:
        return
    data = load_problem_summaries(group_id)
    data[pid] = {
        "summary_zh": summary_zh,
    }
    with open(_problem_summary_file(group_id), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_problem_card_refs(group_id: int) -> dict[str, dict]:
    path = _problem_card_refs_file(group_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_problem_card_ref_pid(group_id: int, message_id: str) -> str:
    if not message_id:
        return ""
    data = load_problem_card_refs(group_id)
    item = data.get(str(message_id))
    if not isinstance(item, dict):
        return ""
    return str(item.get("problem", "") or "")


def save_problem_card_ref(
    group_id: int,
    message_id: str | int,
    pid: str,
    source: str,
    max_entries: int = 1000,
) -> None:
    if not message_id or not pid:
        return
    data = load_problem_card_refs(group_id)
    now = int(time.time())
    data[str(message_id)] = {
        "problem": pid,
        "source": source,
        "created_at": now,
    }
    if len(data) > max_entries:
        ranked = sorted(
            data.items(),
            key=lambda item: int(item[1].get("created_at", 0)) if isinstance(item[1], dict) else 0,
        )
        data = dict(ranked[-max_entries:])
    with open(_problem_card_refs_file(group_id), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_already_solved(group_id: int) -> bool:
    """Check if anyone has solved today's problem."""
    problem = get_today_problem(group_id)
    if not problem:
        return False
    pid = problem.get("today", "")
    sb = load_scoreboard(group_id)
    for solve in sb.get("solves", []):
        if solve.get("problem") == pid:
            return True
    return False


def get_latest_solved_problem_id(group_id: int) -> str | None:
    """Get the most recently solved problem ID for a group, if any."""
    sb = load_scoreboard(group_id)
    solves = sb.get("solves", [])
    for solve in reversed(solves):
        pid = solve.get("problem", "")
        if pid:
            return pid
    return None


# ── Scoreboard ──────────────────────────────────────────────────────────

def _scoreboard_file(group_id: int) -> str:
    cfg = get_config()
    d = os.path.join(cfg.data_dir, "groups", str(group_id))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "scoreboard.json")


def load_scoreboard(group_id: int) -> dict:
    path = _scoreboard_file(group_id)
    if not os.path.exists(path):
        return {"solves": [], "user_submissions": {}}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {"solves": [], "user_submissions": {}}


def save_scoreboard(group_id: int, sb: dict) -> None:
    with open(_scoreboard_file(group_id), "w") as f:
        json.dump(sb, f, ensure_ascii=False, indent=2)


def _problem_ratings_file(group_id: int) -> str:
    cfg = get_config()
    d = os.path.join(cfg.data_dir, "groups", str(group_id))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "problem_ratings.json")


def _coerce_int(value) -> int | None:
    if value in (None, "", "?"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def high_difficulty_notice(problem: dict | None) -> str:
    if not isinstance(problem, dict):
        return ""
    rating = _coerce_int(problem.get("rating"))
    if rating is not None and rating > HIGH_DIFFICULTY_RATING_THRESHOLD:
        return HIGH_DIFFICULTY_NOTICE
    return ""


def load_problem_ratings(group_id: int) -> dict[str, int]:
    path = _problem_ratings_file(group_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            raw = json.load(f)
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    data: dict[str, int] = {}
    for pid, rating in raw.items():
        rating_value = _coerce_int(rating)
        if rating_value is not None:
            data[str(pid)] = rating_value
    return data


def save_problem_ratings(group_id: int, ratings: dict[str, int]) -> None:
    with open(_problem_ratings_file(group_id), "w") as f:
        json.dump(ratings, f, ensure_ascii=False, indent=2, sort_keys=True)


def remember_problem_rating(group_id: int, pid: str, rating) -> None:
    if not pid:
        return
    rating_value = _coerce_int(rating)
    if rating_value is None:
        return
    ratings = load_problem_ratings(group_id)
    if ratings.get(pid) == rating_value:
        return
    ratings[pid] = rating_value
    save_problem_ratings(group_id, ratings)


def _problem_id_from_problem(problem: dict) -> str:
    contest_id = problem.get("contestId")
    index = problem.get("index")
    if contest_id in (None, "") or not index:
        return ""
    return f"{contest_id}{index}"


def load_known_problem_ratings(group_id: int, problem_ids: set[str] | None = None) -> dict[str, int]:
    ratings = load_problem_ratings(group_id)
    target_ids = {pid for pid in (problem_ids or set()) if pid}
    pending = target_ids - set(ratings)

    state = get_today_problem(group_id)
    if state:
        state_pid = str(state.get("today", "") or _problem_id_from_problem(state))
        state_rating = _coerce_int(state.get("rating"))
        if state_pid and state_rating is not None and (not target_ids or state_pid in target_ids):
            ratings[state_pid] = state_rating
            pending.discard(state_pid)

    if pending:
        cfg = get_config()
        for cache_path in sorted(Path(cfg.data_dir).glob("cf_all_*_*.json")):
            try:
                with cache_path.open() as f:
                    cached = json.load(f)
            except Exception:
                continue
            if not isinstance(cached, list):
                continue
            for item in cached:
                if not isinstance(item, dict):
                    continue
                pid = _problem_id_from_problem(item)
                if not pid or pid not in pending:
                    continue
                rating_value = _coerce_int(item.get("rating"))
                if rating_value is None:
                    continue
                ratings[pid] = rating_value
                pending.discard(pid)
                if not pending:
                    break
            if not pending:
                break

    if ratings != load_problem_ratings(group_id):
        save_problem_ratings(group_id, ratings)
    if not target_ids:
        return ratings
    return {pid: ratings[pid] for pid in target_ids if pid in ratings}


def get_cumulative_solves(sb: dict, user_id: int) -> int:
    uid = str(user_id)
    return sum(1 for s in sb.get("solves", []) if str(s.get("user_id")) == uid)


def rating_to_points(rating: int) -> float:
    return math.pow(2.0, (rating - 2000) / 300)


def format_points(points: float) -> str:
    return f"{points:.2f}".rstrip("0").rstrip(".")


def build_scoreboard_entries(
    group_id: int,
    sb: dict | None = None,
    *,
    user_group_name: str | None = None,
) -> list[dict]:
    from ..user_groups import get_user_group

    scoreboard = sb if sb is not None else load_scoreboard(group_id)
    solves = scoreboard.get("solves", [])
    problem_ids = {
        str(item.get("problem", "") or "")
        for item in solves
        if item.get("problem")
    }
    ratings = load_known_problem_ratings(group_id, problem_ids)

    totals: dict[str, dict] = {}
    for solve in solves:
        uid = str(solve.get("user_id"))
        if user_group_name is not None:
            try:
                if get_user_group(int(uid)).name != user_group_name:
                    continue
            except (TypeError, ValueError):
                continue
        entry = totals.setdefault(uid, {
            "user_id": solve.get("user_id", uid),
            "nickname": solve.get("nickname", "") or uid,
            "solved": 0,
            "score": 0.0,
        })
        entry["solved"] += 1
        pid = str(solve.get("problem", "") or "")
        rating = ratings.get(pid)
        if rating is not None:
            entry["score"] += rating_to_points(rating)

    ranked = sorted(
        totals.values(),
        key=lambda item: (-round(item["score"], 6), str(item["user_id"])),
    )
    prev_score: float | None = None
    display_rank = 0
    for idx, entry in enumerate(ranked, 1):
        score = round(entry["score"], 6)
        if score != prev_score:
            display_rank = idx
            prev_score = score
        entry["rank"] = display_rank
    return ranked


async def fetch_group_member_nickname_map(group_id: int) -> dict[str, str]:
    """Fetch the latest group nickname map for one group."""
    from ..napcat import client as napcat_client

    try:
        resp = await napcat_client._http_post("get_group_member_list", {"group_id": group_id})
        members = resp.get("data", []) if resp.get("status") == "ok" else []
    except Exception:
        members = []

    nickname_map: dict[str, str] = {}
    for member in members:
        if not isinstance(member, dict):
            continue
        uid = str(member.get("user_id", "") or "")
        if not uid:
            continue
        nickname_map[uid] = member.get("card") or member.get("nickname") or uid
    return nickname_map


def update_scoreboard(group_id: int, user_id: int, nickname: str) -> tuple[bool, int, list[dict]]:
    """Record a solve. Returns (is_first_blood, cumulative_solves, top5)."""
    from datetime import datetime, timezone, timedelta
    from ..user_groups import get_user_group

    TZ = timezone(timedelta(hours=8))
    sb = load_scoreboard(group_id)
    uid = str(user_id)
    user_group_name = get_user_group(user_id).name
    problem = get_today_problem(group_id)
    pid = problem.get("today", "") if problem else ""
    today_str = datetime.now(TZ).strftime("%Y-%m-%d")
    remember_problem_rating(group_id, pid, problem.get("rating") if problem else None)

    # Already solved this problem?
    for s in sb["solves"]:
        if str(s.get("user_id")) == str(user_id) and s.get("problem") == pid:
            solved = get_cumulative_solves(sb, user_id)
            return False, solved, _top5_entries(group_id, sb, user_group_name=user_group_name)

    # New solve — check first blood
    problem_solves = [s for s in sb["solves"] if s.get("problem") == pid]
    is_first_blood = len(problem_solves) == 0

    order = len(sb["solves"]) + 1
    entry = {
        "user_id": user_id,
        "nickname": nickname,
        "date": today_str,
        "problem": pid,
        "order": order,
    }
    sb.setdefault("solves", []).append(entry)

    save_scoreboard(group_id, sb)
    solved = get_cumulative_solves(sb, user_id)
    return is_first_blood, solved, _top5_entries(group_id, sb, user_group_name=user_group_name)


def _top5_entries(group_id: int, sb: dict, *, user_group_name: str | None = None) -> list[dict]:
    """Get top 5 users by total score only, with tied ranks preserved."""
    return build_scoreboard_entries(group_id, sb, user_group_name=user_group_name)[:5]


def load_user_submissions(group_id: int, user_id: int) -> list:
    sb = load_scoreboard(group_id)
    submissions = sb.get("user_submissions", {})
    return submissions.get(str(user_id), [])


def save_user_submission(group_id: int, user_id: int, submission: dict) -> None:
    sb = load_scoreboard(group_id)
    submissions = sb.get("user_submissions", {})
    uid = str(user_id)
    if uid not in submissions:
        submissions[uid] = []
    request_id = str(submission.get("request_id", "") or "")
    if request_id:
        for idx in range(len(submissions[uid]) - 1, -1, -1):
            existing = submissions[uid][idx]
            if str(existing.get("request_id", "") or "") == request_id:
                submissions[uid][idx] = submission
                break
        else:
            submissions[uid].append(submission)
    else:
        submissions[uid].append(submission)
    sb["user_submissions"] = submissions
    save_scoreboard(group_id, sb)


def clear_user_problem_submissions(group_id: int, user_id: int, pid: str) -> int:
    sb = load_scoreboard(group_id)
    submissions = sb.get("user_submissions", {})
    uid = str(user_id)
    existing = submissions.get(uid, [])
    kept = [item for item in existing if item.get("problem") != pid]
    removed = len(existing) - len(kept)
    if kept:
        submissions[uid] = kept
    else:
        submissions.pop(uid, None)
    sb["user_submissions"] = submissions
    save_scoreboard(group_id, sb)
    return removed


# ── Judge ───────────────────────────────────────────────────────────────

def get_judge_prompt() -> str:
    """Load judge system prompt."""
    prompt_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "judge_prompt.txt"
    )
    if os.path.exists(prompt_path):
        with open(prompt_path, encoding="utf-8") as f:
            return f.read()
    return "You are a competitive programming judge."


def build_judge_messages(
    problem_text: str,
    submission: str,
    history: list[dict] | None = None,
) -> list[dict]:
    user_msg = json.dumps({
        "problem": problem_text,
        "submission": submission,
        "history": history,
    }, ensure_ascii=False)
    return [
        {"role": "system", "content": get_judge_prompt()},
        {"role": "user", "content": user_msg},
    ]


async def judge_submission_result(
    problem_text: str,
    submission: str,
    history: list[dict] | None = None,
) -> ChatCompletionResult:
    """Judge a submission and preserve terminal LLM failure metadata."""
    cfg = get_config()
    return await call_chat_completion_result(
        build_judge_messages(problem_text, submission, history),
        task="judge",
        temperature=0.3,
        timeout=cfg.judge_timeout_sec,
        response_format={"type": "json_object"},
        thinking={"type": "enabled"},
    )


async def judge_submission(
    problem_text: str,
    submission: str,
    history: list[dict] | None = None,
) -> dict | None:
    """Judge a submission. Returns {correct, reason, reaction, reply} or None."""
    result = await judge_submission_result(problem_text, submission, history)
    if not result.text:
        return None
    return robust_json_parse(result.text)


# ── Judge ───────────────────────────────────────────────────────────────

async def summarize_problem(stmt_text: str, input_text: str, limits_text: str) -> tuple[str | None, str]:
    """Generate Chinese summary of a problem via the configured chat provider.
    
    Returns (summary_text, model_tag). model_tag is empty if the LLM call failed.
    """
    cfg = get_config()
    prompt = (
        f"下面是需要压缩转述的题面素材（可能含英文，以及从公式图识别得到的 LaTeX；勿臆造素材里未出现的事实）：\n\n"
        f"【题干主体】\n{stmt_text}\n\n"
        f"【输入说明】（若几乎为空可略写）\n{input_text}\n\n"
        f"【时空限制】\n{limits_text}\n\n"
        "请输出一段连贯的中文题意简述，使群友只读这段也能明白「要算什么 / 判定什么 / 输出什么」并能开始思考做法。\n\n"
        "完整性（最高优先级）：\n"
        "1) 只基于上述素材转述与压缩；禁止编造素材未写明的性质、定理、样例推论或「显然」步骤。\n"
        "2) 禁止为缩短篇幅而删掉题意关键信息。下列若在素材中出现，则简述中必须保留（可用等价中文复述；"
        "若删掉会改变题目则绝对禁止删）：目标或优化对象；答案/计数/图论对象的精确定义；核心等式、不等式、递推式；"
        "YES/NO 的判定规则；构造题要输出什么的精确定义；交互题里询问/回答的含义；会改变结论的特例或边界。\n"
        "3) 素材中的公式（含 LaTeX）凡参与题意定义、而非仅装饰的，必须在简述中体现：优先用清晰中文把关系说完整；"
        "若仅用中文会产生歧义或无法保留必要的符号结构，允许保留最少必要的 LaTeX 片段（如分式、求和、多重下标），"
        "禁止大段堆砌 LaTeX、禁止代码围栏或 Markdown 数学块。\n\n"
        "表达与长度：\n"
        "1) 背景故事、角色名、与算法无关的修辞可删；定义、约束、关键公式不可删。\n"
        "2) 默认流畅中文 + 简单符号（如 ≤、≥、×）；非交互且无特殊读入要求时，I/O 格式可点到为止。\n"
        "3) 数据范围跟在变量后括号里（如 n(1e5)、a_i(0～1e18)）；时空限制写在段末。\n"
        "4) 交互题在简述末尾加「交互题」。\n"
        "5) 在保证信息等价、无歧义的前提下尽量短；信息完整优先于「字数减半」类目标。\n"
    )
    result = await call_chat_completion_result([
        {"role": "system", "content": (
            "你是算法竞赛选手，在 QQ 群用中文介绍每日一题。"
            "输出连贯、可读、信息完整的中文简述；默认少用 LaTeX 以降低阅读成本，但题意所依赖的关键公式与定义必须交代清楚，"
            "必要时可保留少量 LaTeX。不要使用 Markdown（标题井号、列表语法、代码围栏、粗体斜体）。"
        )},
        {"role": "user", "content": prompt},
    ], task="summary", timeout=cfg.summary_timeout_sec)
    return result.text, result.model_tag


async def translate_sample_notes(notes_text: str) -> tuple[str | None, str]:
    """Translate sample notes/explanations into concise Chinese plain text."""
    content = (notes_text or "").strip()
    if not content:
        return None, ""
    cfg = get_config()
    prompt = (
        "下面是题目中与样例相关的解释（Notes）。\n"
        "请把它翻译为自然、准确的中文，保持原意，不要扩写，不要加入原文没有的结论。\n"
        "输出约束（严格执行）：\n"
        "1) 只输出纯文本正文，不要标题或前言。\n"
        "2) 禁止 Markdown：不要代码块、反引号、粗体、列表语法。\n"
        "3) 禁止 LaTeX 命令：不要出现反斜杠命令（例如 \\xrightarrow、\\lt、\\gt、\\leq）。\n"
        "4) 需要表达符号时，直接使用可读字符：→、<、>、≤、≥、≠、×、÷、⊕ 等。\n"
        "5) 若原文是公式/符号，优先改写为中文句子或上述可读符号；不要保留 LaTeX 形式。\n\n"
        f"{content}"
    )
    result = await call_chat_completion_result(
        [
            {
                "role": "system",
                "content": (
                    "你是算法竞赛选手。你要把题目样例解释忠实翻译成中文，"
                    "要求简洁、可读，且不得编造信息。"
                    "你必须输出纯文本，禁止 Markdown 和 LaTeX 命令。"
                ),
            },
            {"role": "user", "content": prompt},
        ],
        task="summary",
        timeout=cfg.summary_timeout_sec,
    )
    return result.text, result.model_tag


async def translate_editorial_to_zh(editorial_text: str, pid: str = "") -> tuple[str | None, str]:
    """Translate a Codeforces editorial to Chinese for group delivery.

    Returns (translated_text, model_tag). model_tag is empty if the LLM call failed.
    """
    body = (editorial_text or "").strip()
    if not body:
        return None, ""
    if len(body) > 24000:
        body = body[:24000] + "\n...(题解原文已截断)"

    prompt = (
        f"题目编号：{pid or '未知'}\n\n"
        f"待翻译的官方 Editorial（英文为主，可能含 LaTeX/公式与代码）：\n\n"
        f"{body}\n\n"
        "请输出可在 QQ 群直接阅读的中文题解译文（仅思路与算法说明，不要贴代码）。"
        "公式与符号尽量用中文和简单字符表达，避免 LaTeX。"
    )
    result = await call_chat_completion_result(
        [
            {
                "role": "system",
                "content": (
                    "你是算法竞赛选手，要把 Codeforces 官方 Editorial 忠实译成中文题解，"
                    "供 QQ 群友阅读。"
                    "只翻译思路、做法、复杂度等文字说明；原文中的代码、伪代码、"
                    "以反引号或代码块形式出现的程序片段一律不要输出，可用一句「实现见官方代码」带过。"
                    "不要增删关键算法步骤，不要添加原文没有的内容。"
                    "LaTeX/公式处理（重要）：默认不要用 LaTeX，不要输出 \\( \\)、$$、$$$ 或代码围栏。"
                    "把公式改写成流畅中文或简单符号，例如："
                    "\\{a,b\\} 写成集合 {a,b}；"
                    "O(n^2) 写成 O(n²) 或 O(n^2)；\\le \\lt 写成 ≤ <。"
                    "仅当不用 LaTeX 会产生歧义、且无法用中文说清楚时，才保留最少量的 LaTeX 片段。"
                    "输出连贯纯文本，不要 Markdown 标题、列表语法。"
                    "不要写「以下是翻译」等套话，直接输出译文。"
                ),
            },
            {"role": "user", "content": prompt},
        ],
        task="summary",
        timeout=600,
    )
    return result.text, result.model_tag


# ── Contest checking ────────────────────────────────────────────────────

async def check_contests_for_group(group_id: int) -> None:
    """Check CF API for upcoming contests and notify group."""
    import asyncio
    import aiohttp
    from datetime import datetime, timezone, timedelta
    from ..napcat.client import send_group_msg

    TZ = timezone(timedelta(hours=8))
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://codeforces.com/api/contest.list",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
    except Exception as e:
        logger.warning(f"CF contest API error: {e}")
        return

    if data.get("status") != "OK":
        return

    now = datetime.now(TZ)
    cutoff = now + timedelta(hours=24)
    upcoming = []
    for c in data["result"]:
        phase = c.get("phase", "")
        if phase not in ("BEFORE", "CODING"):
            continue
        start = datetime.fromtimestamp(c["startTimeSeconds"], tz=TZ)
        if start <= cutoff:
            upcoming.append((start, c, phase))

    if not upcoming:
        return

    upcoming.sort(key=lambda x: x[0])
    lines = ["🏆 Codeforces 比赛提醒！"]
    for start, c, phase in upcoming:
        name = c["name"]
        dur_h = c["durationSeconds"] // 3600
        dur_m = (c["durationSeconds"] % 3600) // 60
        dur_str = f"{dur_h}h" if dur_m == 0 else f"{dur_h}h{dur_m}m"
        start_str = start.strftime("%H:%M")
        if phase == "CODING":
            rel = "正在进行"
        else:
            delta = start - now
            if delta.total_seconds() < 3600:
                rel = f"{int(delta.total_seconds()//60)}分钟后"
            else:
                rel = f"{int(delta.total_seconds()//3600)}小时后"
        lines.append(f"{start_str} {rel} | {name}（{dur_str}）")

    await asyncio.sleep(2)
    await send_group_msg(group_id, [
        {"type": "at", "data": {"qq": "all"}},
        {"type": "text", "data": {"text": "\n" + "\n".join(lines)}},
    ])
