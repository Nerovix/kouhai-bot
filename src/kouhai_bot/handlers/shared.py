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
import urllib.request
from pathlib import Path

from ..config import get_config
from ..llm import ChatCompletionResult, chat_completion, strip_leaked_thinking

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
    provider_name: str = "",
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
        provider_name=provider_name,
    )


async def call_chat_completion(
    messages: list[dict],
    model: str = "",
    task: str = "",
    temperature: float = 0.7,
    timeout: int = 120,
    response_format: dict | None = None,
    thinking: dict | None = None,
    provider_name: str = "",
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
        provider_name=provider_name,
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
    text = strip_leaked_thinking(text)
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


_JSON_REPAIR_MAX_ATTEMPTS = 3
_JSON_REPAIR_MAX_CHARS = 12000


def _strip_leaked_thinking_from_json(value):
    if isinstance(value, str):
        return strip_leaked_thinking(value)
    if isinstance(value, dict):
        return {k: _strip_leaked_thinking_from_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_leaked_thinking_from_json(v) for v in value]
    return value


def _json_repair_prompt(text: str, expected_schema: str, parse_note: str) -> str:
    payload = {
        "expected_schema": expected_schema or "JSON object",
        "parse_note": parse_note or "local parser could not read a JSON object",
        "bad_output": (text or "")[:_JSON_REPAIR_MAX_CHARS],
    }
    return (
        "你是一个 JSON 编写高手。下面是另一个模型本应输出 JSON 对象、但格式不合法的回复。\n"
        "请只修复格式，不要改写含义，不要补充新事实，不要解释。\n"
        "必须只输出一个合法 JSON 对象；不要 Markdown、不要代码块、不要前后缀文字。\n"
        "如果原回复只是纯文本 summary，请按 expected_schema 包成对应字段；"
        "如果原回复已经包含字段但引号、逗号、布尔值或转义有错，只做最小修复。\n\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


async def parse_json_with_llm_repair(
    text: str | None,
    *,
    expected_schema: str = "JSON object",
    task: str = "summary",
    timeout: int | None = None,
    max_attempts: int = _JSON_REPAIR_MAX_ATTEMPTS,
) -> tuple[dict, str]:
    """Parse an LLM JSON object, using general_model to repair malformed output.

    Returns (parsed_object, repair_model_tag). repair_model_tag is non-empty only
    when a repair model response was used.
    """
    current = strip_leaked_thinking(text or "")
    parsed = robust_json_parse(current)
    if parsed:
        return _strip_leaked_thinking_from_json(parsed), ""
    if not current:
        return {}, ""

    cfg = get_config()
    repair_timeout = timeout or getattr(cfg, "summary_timeout_sec", 120)
    parse_note = "no valid JSON object found"
    last_tag = ""
    attempts = max(0, int(max_attempts or 0))
    for attempt in range(attempts):
        logger.info(
            "json repair requested attempt=%s schema=%s",
            attempt + 1,
            expected_schema,
        )
        result = await call_chat_completion_result(
            [
                {
                    "role": "system",
                    "content": (
                        "你只负责把模型输出修成合法 JSON 对象。"
                        "禁止解释，禁止改变语义，禁止输出 JSON 以外的文本。"
                    ),
                },
                {
                    "role": "user",
                    "content": _json_repair_prompt(current, expected_schema, parse_note),
                },
            ],
            task=task,
            temperature=0.0,
            timeout=repair_timeout,
            response_format={"type": "json_object"},
        )
        last_tag = result.model_tag
        if not result.text:
            parse_note = f"repair model returned no text ({result.failure_kind or 'unknown failure'})"
            continue
        current = strip_leaked_thinking(result.text)
        parsed = robust_json_parse(current)
        if parsed:
            return _strip_leaked_thinking_from_json(parsed), last_tag
        parse_note = "repair output was still not valid JSON"

    logger.warning(
        "json repair failed after %s attempts for schema=%s; last_output=%s",
        attempts,
        expected_schema,
        current[:200],
    )
    return {}, ""


# ── Problem statement loading ───────────────────────────────────────────

_MAX_MULTIMODAL_IMAGES = 8


def multimodal_model_configured() -> bool:
    return bool(get_config().llm_multimodal_providers)


def load_problem_statement_json(pid: str) -> dict:
    """Load raw problem statement cache JSON."""
    cfg = get_config()
    stmt_path = os.path.join(cfg.data_dir, "statements", f"{pid}.json")
    if not os.path.exists(stmt_path):
        return {}

    try:
        with open(stmt_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def format_problem_statement_for_llm(stmt: dict) -> str:
    """Format statement cache JSON as text for LLM context."""
    if not isinstance(stmt, dict):
        return ""

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


def load_problem_statement(pid: str) -> str:
    """Load full problem statement from cache, formatted for LLM."""
    return format_problem_statement_for_llm(load_problem_statement_json(pid))


def statement_images(stmt: dict) -> list[dict]:
    images = stmt.get("images", []) if isinstance(stmt, dict) else []
    return [item for item in images if isinstance(item, dict) and item.get("src")]


def _download_image_data_url(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = resp.read()
        content_type = str(resp.headers.get("Content-Type") or "").split(";", 1)[0]
    mime = content_type if content_type.startswith("image/") else "image/png"
    import base64

    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def build_multimodal_user_content(text: str, images: list[dict]) -> list[dict]:
    """Build OpenAI-compatible text+image content parts."""
    content: list[dict] = [{"type": "text", "text": text}]
    for idx, image in enumerate(images[:_MAX_MULTIMODAL_IMAGES], 1):
        src = str(image.get("src", "") or "").strip()
        if not src:
            continue
        kind = str(image.get("kind", "") or "image")
        marker = str(image.get("marker", "") or f"IMAGE_{idx}")
        placeholder = str(image.get("placeholder", "") or f"[[{marker}: {kind}]]")
        context = str(image.get("context", "") or "").strip()
        label = f"题面图片 {marker}（{kind}），对应原文占位符：{placeholder}"
        if context:
            label += f"，附近文本：{context[:500]}"
        content.append({"type": "text", "text": "\n\n" + label})
        try:
            image_url = _download_image_data_url(src)
        except Exception as e:
            logger.warning("failed to download statement image %s: %s", src, e)
            image_url = src
        content.append({"type": "image_url", "image_url": {"url": image_url}})
    return content


def _usable_statement_images(images: list[dict] | None) -> list[dict]:
    return [item for item in images or [] if isinstance(item, dict) and item.get("src")]


def _multimodal_content_or_text(text: str, images: list[dict]) -> str | list[dict]:
    return build_multimodal_user_content(text, images) if images else text


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
        return strip_leaked_thinking(item.get("summary_zh", "") or "")
    if isinstance(item, str):
        return strip_leaked_thinking(item)
    return ""


def save_problem_summary(group_id: int, pid: str, summary_zh: str) -> None:
    if not pid or not summary_zh:
        return
    summary_zh = strip_leaked_thinking(summary_zh)
    if not summary_zh:
        return
    data = load_problem_summaries(group_id)
    data[pid] = {
        "summary_zh": summary_zh,
    }
    with open(_problem_summary_file(group_id), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def sanitize_cached_problem_card_payload(data: dict) -> tuple[dict, bool]:
    """Scrub cached LLM text before rebuilding user-visible problem cards.

    If any cached text changes, stale message-node ids must not be reused because
    forwarding them would resend the original unsanitized message body.
    """
    if not isinstance(data, dict):
        return {}, False
    cleaned = dict(data)
    changed = False
    for key in ("post_msg", "notes_message"):
        value = cleaned.get(key)
        if not isinstance(value, str):
            continue
        stripped = strip_leaked_thinking(value)
        if stripped != value:
            cleaned[key] = stripped
            changed = True
    if changed:
        for key in ("msg_id", "sample_msg_ids", "note_msg_id", "snake_msg_id", "fwd_message_id"):
            cleaned.pop(key, None)
    return cleaned, changed


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


def _dialogue_kind(item_type: str, result: str) -> str:
    if item_type in {"submit", "clarify", "review"}:
        return item_type
    if result in {"clarify", "review"}:
        return result
    return item_type or "interaction"


def _history_dialogue(history: list[dict] | None) -> list[dict]:
    dialogue: list[dict] = []
    for turn, item in enumerate(history or [], 1):
        item_type = str(item.get("type", "") or "")
        result = str(item.get("result", "") or "")
        kind = _dialogue_kind(item_type, result)
        content = str(item.get("content", "") or "").strip()
        if content:
            user_turn = {
                "turn": len(dialogue) + 1,
                "role": "user",
                "kind": kind,
                "content": content,
                "note": "user_claim",
            }
            if result:
                user_turn["verdict"] = result
            dialogue.append(user_turn)

        reply = str(item.get("reply", "") or "").strip()
        if reply:
            dialogue.append({
                "turn": len(dialogue) + 1,
                "role": "assistant",
                "kind": f"{kind}_feedback",
                "content": reply,
                "note": "bot_feedback_not_user_claim",
            })

        reason = str(item.get("reason", "") or "").strip()
        if reason and not reply:
            dialogue.append({
                "turn": len(dialogue) + 1,
                "role": "assistant",
                "kind": f"{kind}_verdict_reason",
                "content": reason,
                "note": "bot_reason_not_user_claim",
            })
    return dialogue


def build_judge_context(
    problem_text: str,
    submission: str,
    history: list[dict] | None = None,
) -> dict:
    return {
        "task": "first_pass_judge_complete_solution",
        "problem_statement": problem_text,
        "current_submission": submission,
        "dialogue": _history_dialogue(history),
        "dialogue_rules": [
            "role=user entries are user claims and may complete the current submission.",
            "role=assistant entries are bot feedback or verdict reasons; use them only as context, not as user-stated ideas.",
            "The current_submission is the claim being judged now.",
        ],
    }


def build_judge_messages(
    problem_text: str,
    submission: str,
    history: list[dict] | None = None,
) -> list[dict]:
    user_msg = json.dumps(
        build_judge_context(problem_text, submission, history),
        ensure_ascii=False,
    )
    return [
        {"role": "system", "content": get_judge_prompt()},
        {"role": "user", "content": user_msg},
    ]


def build_second_judge_messages(
    problem_text: str,
    submission: str,
    history: list[dict] | None,
    first_judge_result: dict,
    editorial_text: str,
    editorial_source: str = "",
) -> list[dict]:
    editorial_body = (editorial_text or "").strip()
    if len(editorial_body) > 18000:
        editorial_body = editorial_body[:18000] + "\n...(官方题解已截断)"
    user_msg = json.dumps({
        "task": "second_review_correctness_only",
        "problem_statement": problem_text,
        "current_submission": submission,
        "dialogue": _history_dialogue(history),
        "dialogue_rules": [
            "role=user entries are user claims and may complete the current submission.",
            "role=assistant entries are bot feedback or verdict reasons; they are not user-stated ideas.",
            "The current_submission is the claim whose correctness is being reviewed.",
        ],
        "first_pass": {
            "verdict": first_judge_result.get("correct"),
            "reason_to_audit": first_judge_result.get("reason", ""),
            "reply": first_judge_result.get("reply", ""),
            "reaction": first_judge_result.get("reaction", ""),
        },
        "official_reference": {
            "source": editorial_source,
            "editorial": editorial_body,
        },
        "decision_focus": [
            "Do not judge completeness in second review.",
            "Do not require matching the official editorial.",
            "Reject if the user's actual algorithm is wrong or too slow.",
            "Reject if first_pass.reason repairs the user's claim into a different algorithm.",
        ],
    }, ensure_ascii=False)
    return [
        {"role": "system", "content": (
            "你是算法竞赛做法判定的二审 bot。你会收到题面、用户做法、用户在本题的历史上下文、"
            "一审 bot 的完整 JSON 判定，以及爬取到的 Codeforces 官方题解。"
            "一审 bot 做出判定时看不到官方题解，只能基于题面、用户做法和历史上下文独立推理；"
            "因此一审判为正确并不代表它已被题解验证过。"
            "你的任务是复核一审判为正确的做法在正确性上是否站得住，而不是重新审查完整性。\n\n"
            "判定原则：\n"
            "1) 只输出 JSON 对象，字段为 correct、reason、reply、reaction。\n"
            "2) 二审不是题解匹配器。如果用户做法在逻辑上正确，即使和官方题解完全不同、没有使用题解算法，也必须判 correct=true。\n"
            "3) 二审不因普通实现细节、证明细节、代码细节、命名不严谨或展开不充分而判错；这些完整性问题已经由一审处理。"
            "只在用户实际说出的核心策略、关键比较量、复杂度主张或边界处理会导致 WA/TLE 时，才判 correct=false。\n"
            "4) 如果用户做法存在关键漏洞、复杂度不满足、必要边界处理错误，或与题解揭示的关键性质直接冲突，判 correct=false，"
            "reason 写清楚技术原因，reply 写给用户看的简短纠错提示。\n"
            "5) 一审可能被用户错误说法带偏。你必须独立判断，不要默认赞同一审。\n"
            "6) 必须区分“用户实际写出的做法”和“你替用户补全后的正确做法”。"
            "如果需要把用户的关键词重新解释成题解里的另一个关键概念，或补上用户没有说明的关键贪心依据、DP 状态、"
            "比较函数、复杂度优化、边界处理，才能让做法正确，应判 correct=false。\n"
            "7) 对优化/贪心题要特别检查优先级或比较函数是否写对。"
            "不要把“维护当前最大区间/最大代价并从中间截断”自动修补成“维护新增一个操作的边际收益”；"
            "不要把朴素逐次模拟自动修补成二分阈值、批量计数或数学求和。"
            "若答案规模可能很大，而用户只描述逐个添加/逐个弹堆，没有说明批量化或对阈值二分，复杂度不满足时应判错。\n"
            "8) 在判 correct=true 前，主动尝试构造小反例或极端规模反例。"
            "若能找到与用户描述直接冲突的反例，或用户描述缺少排除该反例的关键条件，应判 correct=false。\n\n"
            "输出示例："
            "{\"correct\": true, \"reason\": \"...\", \"reply\": \"\", \"reaction\": \"\"}"
        )},
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


async def second_judge_submission_result(
    problem_text: str,
    submission: str,
    history: list[dict] | None,
    first_judge_result: dict,
    editorial_text: str,
    editorial_source: str = "",
    provider_name: str = "",
    model: str = "",
) -> ChatCompletionResult:
    """Re-check a first-pass correct verdict with official editorial context."""
    cfg = get_config()
    return await call_chat_completion_result(
        build_second_judge_messages(
            problem_text,
            submission,
            history,
            first_judge_result,
            editorial_text,
            editorial_source,
        ),
        model=model,
        task="judge",
        temperature=0.2,
        timeout=cfg.judge_timeout_sec,
        response_format={"type": "json_object"},
        thinking={"type": "enabled"},
        provider_name=provider_name,
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
    parsed, _repair_tag = await parse_json_with_llm_repair(
        result.text,
        expected_schema='{ "correct": boolean, "reason": string, "reply": string, "reaction": string }',
        task="summary",
        timeout=get_config().summary_timeout_sec,
    )
    return parsed


# ── Judge ───────────────────────────────────────────────────────────────

_SUMMARY_TARGET_CHARS = 520
_SUMMARY_HARD_MAX_CHARS = 700
_SUMMARY_LEAK_RE = re.compile(
    r"(题解|解题思路|计数思路|算法思路|复杂度|"
    r"可以用[^。；\n]*(?:枚举|贪心|动态规划|DP|dp|二分|排序|线段树|树状数组|最短路)|"
    r"(?:枚举|贪心|动态规划|DP|dp|二分|排序|线段树|树状数组)[^。；\n]*即可)"
)
_SUMMARY_FORMAT_RE = re.compile(r"(^|\n)\s*(输入|输出|样例)\s*[:：]", re.I)
_SUMMARY_MARKDOWN_RE = re.compile(r"(^|\n)\s*(#{1,6}\s+|[-*]\s+|```|\d+\.\s+)")
_SUMMARY_UNICODE_NOTATION_GUIDE = (
    "Unicode 角标白名单：只使用下面明确列出的字符，不要自己创造看起来像角标的字母。"
    "下标数字：₀₁₂₃₄₅₆₇₈₉；"
    "下标拉丁字母：ₐ ₑ ₕ ᵢ ⱼ ₖ ₗ ₘ ₙ ₒ ₚ ᵣ ₛ ₜ ᵤ ᵥ ₓ；"
    "下标希腊字母：ᵦ ᵧ ᵨ ᵩ ᵪ；"
    "下标其他字母：ₔ；"
    "下标符号：₊ ₋ ₌ ₍ ₎；"
    "上标数字：⁰¹²³⁴⁵⁶⁷⁸⁹；"
    "上标小写拉丁字母：ᵃ ᵇ ᶜ ᵈ ᵉ ᶠ ᵍ ʰ ⁱ ʲ ᵏ ˡ ᵐ ⁿ ᵒ ᵖ ʳ ˢ ᵗ ᵘ ᵛ ʷ ˣ ʸ ᶻ；"
    "上标大写拉丁字母：ᴬ ᴮ ᴰ ᴱ ᴳ ᴴ ᴵ ᴶ ᴷ ᴸ ᴹ ᴺ ᴼ ᴾ ᴿ ᵀ ᵁ ⱽ ᵂ；"
    "上标希腊字母：ᵅ ᵝ ᵞ ᵟ ᵋ ᶿ ᵠ ᵡ；"
    "上标其他字母：ᵊ ᵌ ᵸ ᵎ ᵔ ᵕ ᵙ ᵜ ᵚ ᶛ；"
    "上标符号：⁺ ⁻ ⁼ ⁽ ⁾。"
    "常见写法：aᵢ、aⱼ、aₖ、aₙ、aᵢ₊₁、dpᵢⱼ、x²、2ᵏ、10¹⁸、10⁹+7。"
    "没有列出的下标/上标字母（例如下标 b/c/d/f/g/q/w/y/z，或上标 q）不要硬造；"
    "端点下标不确定或字符白名单不够用时，优先改成自然语言，如“给定的若干元素/所有给定位置”。"
)
def _polish_summary_notation(summary: str) -> str:
    return summary.strip()


def _summary_system_prompt() -> str:
    return (
        "你是算法竞赛选手，在 QQ 群用中文介绍一道题的题意。你的目标不是完整翻译题面，"
        "而是在不丢失限制和目标的前提下，写出短、自然、让人愿意读题的题意简述。"
        "必须只输出 JSON 对象，格式为 {\"summary\":\"...\"}。"
    )


def _summary_source_payload(stmt_text: str, input_text: str, limits_text: str) -> dict[str, str]:
    return {
        "statement": stmt_text or "",
        "input": input_text or "",
        "limits": limits_text or "",
    }


def _build_summary_prompt(stmt_text: str, input_text: str, limits_text: str) -> str:
    payload = _summary_source_payload(stmt_text, input_text, limits_text)
    return (
        "把下面 Codeforces 题面素材压缩转述为中文 QQ 群题意 summary。\n\n"
        "硬性要求：\n"
        "1. 只写题意，不写题解、算法、思路、复杂度、样例推导或任何“怎么做”。\n"
        "2. 读者只看 summary 应该知道：给了什么、允许做什么、要求算/判定/构造什么、输出含义、全部数据范围、时限和内存限制。\n"
        "3. 不要单列“输入：”“输出：”“样例：”。群友是交流做法，不是写代码；普通读入/输出格式默认省略。"
        "只保留会影响理解的输出对象/答案含义，以及多测、所有 n 之和这类限制。交互题、构造输出、多答案、YES/NO 判定、特殊输出格式会影响题意时才自然说明。\n"
        "4. 所有数据范围必须完整出现；可合并压缩但不能丢。题面给了上下界时尽量写完整区间，如 1≤n≤2×10⁵、0≤aᵢ≤10⁹、1≤t≤10⁴、多测且所有 n 之和≤2×10⁵；范围里也使用角标，不要写 a_i。\n"
        "5. 时限和内存必须写在末尾，例如“时限2s，内存256MB”。\n"
        "6. 背景故事、角色名、修辞、样例细节能删就删；定义、边界、判定规则、输出目标、数据范围、时空限制不能删。不要改变规则适用对象：若原文说每次操作/每个玩家/任意元素满足某条件，不能误写成只对先手、只对某一次或只对某一类对象成立。\n"
        "7. LaTeX 是最后手段：普通数组下标和幂次优先用 Unicode 角标，如 aᵢ、a₁、a₂、aₙ、10¹⁸、10⁹+7；也可写“第 i 个/每个位置的 a”。"
        f"{_SUMMARY_UNICODE_NOTATION_GUIDE}"
        "严格递增序列写 a₁<a₂<…<aₙ 或“严格递增坐标”。不要猜测列表端点或把原文没有的下标终点写进 summary；端点不适合用 Unicode 角标时改用自然语言。"
        "简单关系用 ≤、≥、×、mod。只有复杂求和、递推、分式、多重下标、精确定义用中文会失真时，才保留最少必要 LaTeX。\n"
        f"8. 目标长度不超过 {_SUMMARY_TARGET_CHARS} 个中文字符；复杂题可略长，但完整限制优先于短。\n"
        "9. 不要 Markdown、标题、列表、代码块、粗体斜体。\n\n"
        "好风格示例：\n"
        "给定长度为 n 的数组 a 和 q 个询问。每次询问给出区间 [l,r]，要求把区间划分成尽量少的连续段，"
        "使每段内所有数的乘积等于它们的 LCM；输出最少段数。1≤n,q≤10⁵，1≤aᵢ≤10⁵，时限1s，内存256MB。\n"
        "给定多组数据，每组有数组 a。对每个循环移位求某个二元答案 (cnt,cost)，其中 cost 对 10⁹+7 取模；1≤t≤2×10⁴，所有 n 之和≤10⁶，1≤aᵢ≤10⁹，时限3s，内存256MB。\n"
        "给定递增坐标 1≤a₁<a₂<…<aₙ，可在整数点新增传送器，要求从 0 到 aₙ 的总代价≤m，求最少新增数。1≤n≤2×10⁵，aₙ≤m≤10¹⁸，时限7s，内存512MB。\n\n"
        "坏风格示例（禁止）：\n"
        "输入：第一行 n 和 q... 输出：每个询问输出答案... 解题思路是预处理每个位置能延伸到哪里，然后倍增。\n\n"
        "题面素材 JSON：\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


async def _parse_summary_json(text: str | None, *, timeout: int) -> tuple[str | None, str]:
    parsed, repair_tag = await parse_json_with_llm_repair(
        text,
        expected_schema='{ "summary": string }',
        task="summary",
        timeout=timeout,
    )
    summary = parsed.get("summary") if isinstance(parsed, dict) else None
    if not isinstance(summary, str):
        return None, ""
    summary = _polish_summary_notation(summary)
    return summary or None, repair_tag


def _limit_values(limits_text: str) -> tuple[str, str]:
    time_value = ""
    memory_value = ""
    time_match = re.search(r"Time:\s*([^,]+)", limits_text or "", re.I)
    memory_match = re.search(r"Memory:\s*(.+)$", limits_text or "", re.I)
    if time_match:
        time_value = time_match.group(1).strip().lower()
    if memory_match:
        memory_value = memory_match.group(1).strip().lower()
    return time_value, memory_value


def _limit_value_present(summary: str, value: str, *, kind: str) -> bool:
    if not value or "?" in value:
        return True
    text = summary.lower().replace(" ", "")
    raw = value.replace(" ", "")
    if raw and raw in text:
        return True
    number_match = re.search(r"\d+(?:\.\d+)?", value)
    if not number_match:
        return True
    number = number_match.group(0)
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    if kind == "time":
        return bool(re.search(rf"{re.escape(number)}(?:s|秒|second|seconds)", text))
    return bool(re.search(rf"{re.escape(number)}(?:mb|mib|兆|megabyte|megabytes)", text))


def _summary_quality_issues(summary: str, limits_text: str = "") -> list[str]:
    issues: list[str] = []
    text = (summary or "").strip()
    if not text:
        return ["summary is empty"]
    if len(text) > _SUMMARY_HARD_MAX_CHARS:
        issues.append(f"summary is too long ({len(text)} chars)")
    elif len(text) > _SUMMARY_TARGET_CHARS:
        issues.append(f"summary should be more compressed ({len(text)} chars)")
    if _SUMMARY_LEAK_RE.search(text):
        issues.append("summary appears to include solution/editorial content")
    if _SUMMARY_FORMAT_RE.search(text):
        issues.append("summary uses explicit input/output/sample sections")
    if _SUMMARY_MARKDOWN_RE.search(text):
        issues.append("summary uses markdown/list formatting")

    time_value, memory_value = _limit_values(limits_text)
    if not _limit_value_present(text, time_value, kind="time"):
        issues.append("summary omits time limit")
    if not _limit_value_present(text, memory_value, kind="memory"):
        issues.append("summary omits memory limit")
    return issues


def _build_summary_repair_prompt(
    stmt_text: str,
    input_text: str,
    limits_text: str,
    summary: str,
    issues: list[str],
) -> str:
    payload = {
        "source": _summary_source_payload(stmt_text, input_text, limits_text),
        "bad_summary": summary,
        "issues": issues,
    }
    return (
        "请修正 bad_summary，并仍然只输出 JSON：{\"summary\":\"...\"}。\n"
        "修正规则：只压缩、改写、删除违规内容；不要加入 source 中没有的新事实。\n"
        "必须保留题目目标、关键定义、输出含义、全部数据范围（上下界都保留）、时限和内存；删除题解/思路/复杂度/样例推导；"
        "修正任何把“每次/任意/双方/所有对象”的规则误写成只对先手、只对一次或只对部分对象成立的表述；"
        "不要单列输入输出；普通输入格式删掉，只保留答案含义、多测和总和限制；正文和数据范围里的普通 a_i、a_{i+1}、a_1<...<a_n 都改成 aᵢ、aᵢ₊₁、a₁<…<aₙ、相邻位置等角标或自然写法；"
        f"{_SUMMARY_UNICODE_NOTATION_GUIDE}"
        "把端点无法从 source 直接确认的角标范围改成自然语言，除非 source 明确给出该终点；"
        "把 10^18、1e18、10^9+7、1e9+7 等普通幂次改成 10¹⁸、10⁹+7；"
        "LaTeX 只在复杂公式不可自然中文化时保留。\n\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


async def summarize_problem(
    stmt_text: str,
    input_text: str,
    limits_text: str,
    images: list[dict] | None = None,
) -> tuple[str | None, str]:
    """Generate Chinese summary of a problem via the configured chat provider.

    Returns (summary_text, model_tag). model_tag is empty if the LLM call failed.
    """
    cfg = get_config()
    image_items = _usable_statement_images(images)
    if image_items and not multimodal_model_configured():
        logger.warning("summary requested for image statement without llm.multimodal_model")
        return None, ""
    user_prompt = _build_summary_prompt(stmt_text, input_text, limits_text)
    user_content = _multimodal_content_or_text(user_prompt, image_items)
    messages = [
        {"role": "system", "content": _summary_system_prompt()},
        {"role": "user", "content": user_content},
    ]
    result = await call_chat_completion_result(
        messages,
        task="multimodal_summary" if image_items else "summary",
        temperature=0.4,
        timeout=cfg.summary_timeout_sec,
        response_format={"type": "json_object"},
    )
    summary, repair_tag = await _parse_summary_json(result.text, timeout=cfg.summary_timeout_sec)
    tag = repair_tag or result.model_tag
    if not summary:
        logger.warning("summary generation returned invalid JSON/object")
        return None, ""

    issues = _summary_quality_issues(summary, limits_text)
    if not issues:
        return summary, tag

    logger.info("summary quality repair requested: %s", "; ".join(issues))
    repair_result = await call_chat_completion_result(
        [
            {"role": "system", "content": _summary_system_prompt()},
            {
                "role": "user",
                "content": _build_summary_repair_prompt(
                    stmt_text,
                    input_text,
                    limits_text,
                    summary,
                    issues,
                ),
            },
        ],
        task="summary",
        temperature=0.2,
        timeout=cfg.summary_timeout_sec,
        response_format={"type": "json_object"},
    )
    repaired, repaired_json_tag = await _parse_summary_json(
        repair_result.text,
        timeout=cfg.summary_timeout_sec,
    )
    if not repaired:
        logger.warning("summary repair returned invalid JSON/object")
        return None, ""
    repair_issues = _summary_quality_issues(repaired, limits_text)
    if repair_issues:
        logger.warning("summary repair still failed quality gate: %s", "; ".join(repair_issues))
        return None, ""
    return repaired, repaired_json_tag or repair_result.model_tag


async def translate_sample_notes(
    notes_text: str,
    images: list[dict] | None = None,
) -> tuple[str | None, str]:
    """Translate sample notes/explanations into concise Chinese plain text."""
    content = (notes_text or "").strip()
    if not content:
        return None, ""
    cfg = get_config()
    image_items = _usable_statement_images(images)
    if image_items and not multimodal_model_configured():
        logger.warning("sample notes translation requested with images without llm.multimodal_model")
        image_items = []
    prompt = (
        "下面是题目中与样例相关的解释（Notes）。\n"
        "请把它忠实翻译为自然、准确的中文；只做翻译，不要总结、点评、补充解释、改写成题解，"
        "也不要加入原文没有的结论。\n"
        "输出约束（严格执行）：\n"
        "1) 只输出纯文本正文，不要标题或前言。\n"
        "2) 保留原文段落结构；逐句翻译，不要把多句样例解释压成一句概述。不要使用 Markdown 标题、代码块、反引号、粗体、列表语法。\n"
        "3) 样例中的枚举、集合、操作序列、答案数值必须完整保留；不要写省略号，不要用“等”“若干”代替原文列出的对象，不要中途截断。\n"
        "4) LaTeX 是最后手段：普通数组下标和幂次优先用 Unicode 角标，如 aᵢ、a₁、a₂、aₙ、10¹⁸、10⁹+7；"
        "不要输出普通下划线下标，如 a_i、dp_i_j、inc_i、pref_i；改写成 aᵢ、dpᵢⱼ、incᵢ、prefᵢ，或用自然语言。"
        f"{_SUMMARY_UNICODE_NOTATION_GUIDE}"
        "5) 简单关系和运算直接使用可读字符：→、<、>、≤、≥、≠、×、÷、⊕、mod 等；"
        "\\{a,b\\} 写成集合 {a,b}，O(n^2) 写成 O(n²) 或 O(n^2)。\n"
        "6) 禁止不必要的 LaTeX 命令，不要出现 \\( \\)、$$、代码围栏，"
        "不要把简单的 \\lt、\\gt、\\leq、\\geq、\\times、\\oplus 原样留下；"
        "只有复杂求和、递推、分式、多重下标、精确定义用中文或 Unicode 会失真时，才保留最少必要 LaTeX。\n\n"
        f"{content}"
    )
    result = await call_chat_completion_result(
        [
            {
                "role": "system",
                "content": (
                    "你是算法竞赛选手。你要把题目样例解释忠实翻译成中文，"
                    "要求简洁、可读，且不得编造信息。只做翻译，不做总结；长枚举和操作序列也必须完整翻译，不得截断。"
                    "你必须输出纯文本；LaTeX 只在不用会失真时保留，普通下标和幂次优先使用 Unicode 角标。"
                ),
            },
            {"role": "user", "content": _multimodal_content_or_text(prompt, image_items)},
        ],
        task="multimodal_summary" if image_items else "summary",
        timeout=cfg.summary_timeout_sec,
    )
    return result.text, result.model_tag


async def translate_editorial_to_zh(
    editorial_text: str,
    pid: str = "",
    problem_text: str = "",
    images: list[dict] | None = None,
) -> tuple[str | None, str, bool | None]:
    """Validate and translate a Codeforces editorial to Chinese for group delivery.

    Returns (translated_text, model_tag, matched). matched is False only for explicit mismatches.
    """
    body = (editorial_text or "").strip()
    if not body:
        return None, "", None
    if len(body) > 24000:
        body = body[:24000] + "\n...(题解原文已截断)"
    problem_body = (problem_text or "").strip()
    if len(problem_body) > 12000:
        problem_body = problem_body[:12000] + "\n...(题面已截断)"

    prompt_payload = {
        "pid": pid or "未知",
        "problem": problem_body,
        "official_editorial": body,
    }
    cfg = get_config()
    image_items = _usable_statement_images(images)
    if image_items and not multimodal_model_configured():
        logger.warning("editorial translation requested with images without llm.multimodal_model")
        image_items = []
    user_prompt = json.dumps(prompt_payload, ensure_ascii=False)
    result = await call_chat_completion_result(
        [
            {
                "role": "system",
                "content": (
                    "你是算法竞赛选手，要先判断爬取到的 Codeforces 官方 Editorial 是否对应给定题目，"
                    "再把匹配的题解忠实译成中文，供 QQ 群友阅读。"
                    "你必须输出 JSON 对象，格式严格为："
                    "{\"matched\":\"yes\",\"result\":\"中文题解译文\"} 或 "
                    "{\"matched\":\"no\"}。"
                    "matched 只能是 yes 或 no。"
                    "判断匹配时比较题面目标、输入输出、关键变量/约束、核心算法对象和题解讨论的问题；"
                    "如果题解明显在讲另一道题，或题解内容与题面目标/变量/结论对不上，输出 matched=no，"
                    "不要翻译、不要解释。"
                    "如果只是题解使用了不同记号、只覆盖核心思路、或题面与题解能合理对应，输出 matched=yes。"
                    "matched=yes 时，result 是可在 QQ 群直接阅读的中文题解译文。"
                    "只做忠实翻译，不要总结、压缩成提纲、点评、扩写、补充原文没有的做法或结论。"
                    "如果 official_editorial 是整场比赛的多题合集，只翻译 pid 对应的当前题章节，不要翻译其他题；"
                    "但当前题章节内除代码外的每个非空段落都要覆盖，按原文顺序逐段翻译，不要合并删段。"
                    "每个算法步骤、定义、转移、复杂度说明都至少要有对应译文；不要把多个步骤压成一句概述，也不要用省略号。"
                    "只翻译思路、做法、复杂度等文字说明；原文中的代码、伪代码、"
                    "以反引号或代码块形式出现的程序片段一律不要输出，可用一句「实现见官方代码」带过。"
                    "不要增删关键算法步骤，不要添加原文没有的内容。"
                    "result 输出连贯纯文本，尽量保留当前题章节的自然段；不要 Markdown 标题、列表语法、代码围栏，"
                    "不要写「以下是翻译」「总结」等套话。"
                    "LaTeX/公式处理（重要）：默认不要用 LaTeX，不要输出 \\( \\)、$$ 或 $$$。"
                    "普通数组下标和幂次优先用 Unicode 角标，如 aᵢ、aⱼ、a₁、a₂、aₙ、aᵢ₊₁、x²、2ᵏ、10¹⁸、10⁹+7。"
                    "不要输出普通下划线下标，如 a_i、dp_i_j、inc_i、pref_i；改写成 aᵢ、dpᵢⱼ、incᵢ、prefᵢ，或用自然语言。"
                    f"{_SUMMARY_UNICODE_NOTATION_GUIDE}"
                    "简单关系和运算直接使用可读字符：≤、≥、<、>、≠、×、÷、⊕、mod；"
                    "\\{a,b\\} 写成集合 {a,b}，O(n^2) 写成 O(n²) 或 O(n^2)。"
                    "只有复杂求和、递推、分式、多重下标、精确定义用中文或 Unicode 会失真时，才保留最少必要 LaTeX。"
                ),
            },
            {"role": "user", "content": _multimodal_content_or_text(user_prompt, image_items)},
        ],
        task="multimodal_summary" if image_items else "summary",
        timeout=600,
        response_format={"type": "json_object"},
        thinking={"type": "enabled"},
    )
    if not result.text:
        return None, "", None
    parsed, repair_tag = await parse_json_with_llm_repair(
        result.text,
        expected_schema='{ "matched": "yes" | "no", "result": string }',
        task="summary",
        timeout=cfg.summary_timeout_sec,
    )
    matched = str(parsed.get("matched", "")).strip().lower()
    if matched == "no":
        return None, "", False
    if matched != "yes":
        return None, "", None
    translated = str(parsed.get("result", "") or "").strip()
    if not translated:
        return None, "", None
    return translated, repair_tag or result.model_tag, True


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
