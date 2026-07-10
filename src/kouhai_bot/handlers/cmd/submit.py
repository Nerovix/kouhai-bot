"""/submit — judge a user's solution against today's problem.

This module also owns the runtime coordinator for stateful commands:
/submit, /clarify, /review, and /clear.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, TypeVar

from .. import registry
from ..registry import CommandDef
from ..shared import (
    build_scoreboard_entries,
    call_chat_completion_result,
    clear_user_problem_submissions,
    fetch_group_member_nickname_map,
    format_points,
    get_problem_posted_at,
    get_problem_summary,
    get_today_problem,
    judge_submission_result,
    load_known_problem_ratings,
    load_scoreboard,
    load_problem_statement,
    load_user_submissions,
    rating_to_points,
    remember_problem_rating,
    parse_json_with_llm_repair,
    save_scoreboard,
    save_user_submission,
    second_judge_submission_result,
)
from ...config import get_config
from ...context import get_display_name, load_group_ctx
from ...user_groups import (
    effective_submit_delay_sec_for_scoreboard,
    get_user_group,
    is_dynamic_submit_delay_enabled,
    is_default_group,
    settle_dynamic_submit_wait_for_problem,
)
from ...curfew import is_curfew_active, format_curfew_message
from ...editorial_followup import schedule_post_solve_editorial_followup
from ...private_judge import (
    GROUP_SCOPE,
    PRIVATE_SCOPE,
    clear_private_problem_history,
    copy_records,
    get_private_current_problem,
    group_problem_history,
    has_group_problem_private_notified,
    is_group_problem_solved,
    is_private_solved,
    load_private_problem_history,
    mark_group_problem_private_notified,
    mark_private_solved,
    replace_private_problem_history,
    save_private_submission,
    send_problem_card_private,
    set_private_current_problem,
)
from ...tutorials import format_editorial_for_review, get_official_editorial, has_cached_editorial_zh
from ...napcat.client import (
    build_at,
    build_private_reaction_message,
    build_plain_message,
    build_text,
    delete_msg,
    react_emoji,
    send_group_forward_msg,
    send_group_msg,
    send_private_forward_msg,
    send_private_msg,
)

logger = logging.getLogger("kouhai-bot.cmd.submit")

_COMPUTE_CONCURRENCY = 8
_runtime_loop: asyncio.AbstractEventLoop | None = None
_compute_sem: asyncio.Semaphore | None = None
_coordinators: dict[tuple[str, int], "GroupCoordinator"] = {}
_USER_CONTEXT_WRITERS = {"submit", "clarify", "review"}
_REVIEW_FORWARD_THRESHOLD = 200
_REVIEW_CHUNK_SIZE = 3000
_LLM_FAILURE_TEXT = " 模型服务出故障了，联系一下管理员帮帮忙吧～"
T = TypeVar("T")


def _chunk_text(text: str, chunk_size: int) -> list[str]:
    if chunk_size <= 0:
        return [text]
    return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)] or [""]


def _log_preview(value: object, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


CLARIFY_PROMPT = """你是一个算法竞赛群的选手，群友对当前题目有疑问，需要你帮他澄清题目细节。

## 你的任务

根据用户的问题、题目的中文简述、和英文原题面，回答用户的疑问。**只澄清题目本身的细节**（输入输出格式、数据范围、题意理解、样例解释等），严禁透露任何解法提示。
同时不要透露原题是哪一道，包括题号、题目名、比赛编号或任何可反查到原题身份的信息。

## 输出格式

你必须输出一个 JSON 对象：

```json
{"reply": "你的回复文字", "reaction": ""}
```

- `reply`：你发给群友的回复（QQ 纯文字，简短友好 1~3 句，可加 emoji，不要以 @ 开头）
- `reaction`：正常回复时为空字符串；**仅当用户问题是纯捣乱/完全无关的 spam 时**设为 `"123"`（此时 `reply` 也设为空字符串）

## 核心规则

1. **只回答题目本身是什么**，不回答「怎么做」。题目说 input 是空格分隔就是空格分隔；题目说按字典序排序就是字典序。这些是题目事实。
2. **严禁透露解法**：不能说「这题是 DP」「考虑二分」「关键性质是单调性」「转化成图论」「可以贪心」等任何解法相关的内容。即使用户直接问「这题是不是 DP」，你也要友好回避。
3. **检测钓鱼**：如果用户明显在套解法——问「有什么性质」「怎么转化」「关键在哪」「给点提示」——简短友好地告诉他你只能回答题目细节问题，不能给解法提示。
4. **风格**：群友口吻，简洁友好，1~3 句话，不要 AI 套话。可以适当用 emoji。
5. **和题目无关的问题**（让 bot 做别的事、闲聊、天气等）：设置 `reaction="123"`，`reply=""`。

## 示例

用户：「n 的范围是 1e5 吗？」
→ 查题面确认后：{"reply": "是的，n ≤ 1e5 哦～时空限制是 2s / 256MB", "reaction": ""}

用户：「样例里的输出为什么是 3？」
→ 根据题面解释：{"reply": "因为 xxx 情况下只有 3 种合法的方案，你理解的流程是对的就是中间 xxx 那步注意一下～", "reaction": ""}

用户：「这题是不是用线段树？」
→ {"reply": "这个我不好说哦……我只能帮你确认题目细节，做法上的事得靠你自己啦😄", "reaction": ""}

用户：「给点提示呗」
→ {"reply": "提示就算作弊啦！我只能回答题目本身的问题——数据范围、输入格式、样例这种～", "reaction": ""}

用户：「讲个笑话」
→ {"reply": "", "reaction": "123"}

回复是 QQ 纯文字，不用 Markdown/LaTeX。回复不要以 @ 开头。"""


REVIEW_PROMPT = """你是一个算法竞赛群的选手。群友已经做出了群里最近一道通过的题，现在来找你讨论这道题。

## 你的角色

你是群里的热心选手，已经做过这道题了（或者看了题面和群友的提交记录也能理解这道题）。群友来跟你聊这道题——可能问做法细节、为什么自己的某次提交被判错、或者其他和这道题相关的问题。

## 回答原则

1. **现在已经没有「剧透」限制了**——群友已经解出了这道题。你可以自由讨论做法、解法、技巧、复杂度分析等。
2. **看提交记录**：如果群友的交互记录里包含 AI judge 的判定，你可以解释为什么某次提交被判错——指出具体哪里不对、和正确做法的差距在哪。
3. **处理情绪**：群友可能因为被 judge 多次打回而有点沮丧或者不爽。用群友口吻安抚、鼓励、给具体的改进建议。不要敷衍地说「继续加油」——要给具体的技术反馈。
4. **风格**：群友口吻，简洁友好，不要 AI 套话。可以适当 emoji。不要说「首先」「其次」「综上所述」这类套话。
5. **聚焦题目本身**：用户问的是这道题，你就围绕这道题回答。如果用户岔开话题闲聊，友好地拉回来。
6. **不是解答器**：你可以讨论做法的正确性、优化方向、复杂度，但不要直接给完整代码或直接给标准答案——引导群友自己思考和优化。如果用户问的是「这题怎么做」，你可以先问他做到哪一步了、有什么想法，然后针对性地聊。

## 官方题解（仅你可见）

用户消息里可能附带 Codeforces 官方 Editorial。**群友不知道**群里存在这份题解，也没有看过原文。
- 你可以用它来核对用户说的做法是否正确、解释为什么某次提交被判错、回答复盘问题。
- **不要**大段复述或粘贴题解原文，也不要主动剧透「标准答案是 XXX」；对外仍用引导式讨论。
- 用户可能在聊自己的做法或情绪，请认真读懂他的发言再回应，不要敷衍套用题解。

回复是 QQ 纯文字，不用 Markdown/LaTeX。回复不要以 @ 开头。"""


@dataclass
class PendingRequest:
    kind: str
    group_id: int
    user_id: int
    sender: dict
    message_id: str
    command: str
    nickname: str
    scope: str = GROUP_SCOPE
    payload: str = ""
    submit_pid: str = ""
    submit_problem: dict | None = None
    submit_already_solved_at_enqueue: bool = False
    review_pid: str = ""
    review_mentioned_user_ids: list[int] = field(default_factory=list)
    target_pid: str = ""
    seq: int = 0
    admitted_at: float = field(default_factory=time.monotonic)
    admitted_wall: datetime = field(default_factory=lambda: datetime.now(timezone(timedelta(hours=8))))
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    compute_result: Any = None
    discarded: bool = False
    submit_terminal_started: bool = False
    submit_judge_done: bool = False
    submit_correct: bool = False
    submit_score_candidate: bool = False
    submit_score_resolved: bool = False
    submit_waiting_reply_sent: bool = False

    def status_dict(self) -> dict:
        return {
            "scope": self.scope,
            "group_id": self.group_id,
            "user_id": self.user_id,
            "message_id": self.message_id,
            "command": self.command,
            "admitted_at": self.admitted_at,
        }

    @property
    def is_private(self) -> bool:
        return self.scope == PRIVATE_SCOPE


def _refresh_runtime() -> None:
    global _runtime_loop, _compute_sem, _coordinators
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if loop is not _runtime_loop:
        _runtime_loop = loop
        _compute_sem = asyncio.Semaphore(_COMPUTE_CONCURRENCY)
        _coordinators = {}


def _compute_limit() -> asyncio.Semaphore:
    _refresh_runtime()
    assert _compute_sem is not None
    return _compute_sem


def _coordinator_key(group_id: int, *, scope: str = GROUP_SCOPE, user_id: int = 0) -> tuple[str, int]:
    if scope == PRIVATE_SCOPE:
        return (PRIVATE_SCOPE, int(user_id))
    return (GROUP_SCOPE, int(group_id))


def _get_coordinator(
    group_id: int,
    *,
    scope: str = GROUP_SCOPE,
    user_id: int = 0,
) -> "GroupCoordinator":
    _refresh_runtime()
    key = _coordinator_key(group_id, scope=scope, user_id=user_id)
    coord = _coordinators.get(key)
    if coord is None:
        coord = GroupCoordinator(group_id, scope=scope, owner_id=key[1])
        _coordinators[key] = coord
    return coord


async def _call_llm_limited(*args, **kwargs):
    async with _compute_limit():
        return await call_chat_completion_result(*args, **kwargs)


async def _judge_llm_limited(
    problem_text: str,
    submission: str,
    history: list[dict] | None = None,
):
    async with _compute_limit():
        return await judge_submission_result(problem_text, submission, history)


async def _second_judge_llm_limited(
    problem_text: str,
    submission: str,
    history: list[dict] | None,
    first_judge_result: dict,
    editorial_text: str,
    editorial_source: str = "",
    provider_name: str = "",
    model: str = "",
):
    async with _compute_limit():
        return await second_judge_submission_result(
            problem_text,
            submission,
            history,
            first_judge_result,
            editorial_text,
            editorial_source,
            provider_name=provider_name,
            model=model,
        )


async def _send_req_plain(req: PendingRequest, text: str, *, mention: bool = True) -> int | None:
    if req.is_private:
        return await send_private_msg(req.user_id, build_plain_message(text.strip()))
    if mention:
        return await send_group_msg(req.group_id, [
            build_at(req.user_id),
            build_text(f" {text.strip()}"),
        ])
    return await send_group_msg(req.group_id, build_plain_message(text))


async def _send_req_segments(req: PendingRequest, segments: list[dict]) -> int | None:
    if req.is_private:
        cleaned = [seg for seg in segments if seg.get("type") != "at"]
        if not cleaned:
            cleaned = build_plain_message("")
        return await send_private_msg(req.user_id, cleaned)
    return await send_group_msg(req.group_id, segments)


async def _react_req(req: PendingRequest, emoji_id: str) -> None:
    if req.is_private:
        await send_private_msg(req.user_id, build_private_reaction_message(emoji_id))
        return
    await react_emoji(req.message_id, emoji_id)


def _load_latest_group_summary(group_id: int) -> str:
    try:
        ctx = load_group_ctx(group_id)
        for msg in reversed(ctx):
            if msg.get("role") == "assistant":
                return msg.get("content", "")
    except Exception:
        pass
    return ""


def _build_review_history(history: list[dict]) -> str:
    def record_type(item: dict) -> str:
        explicit = item.get("type", "")
        if explicit in {"submit", "clarify", "review"}:
            return explicit
        result = item.get("result", "")
        if result in {"clarify", "review"}:
            return result
        if result in {"correct", "incorrect"}:
            return "submit"
        return "unknown"

    parts = []
    type_counts = {"clarify": 0, "submit": 0, "review": 0, "unknown": 0}
    submit_result_counts = {"correct": 0, "incorrect": 0, "other": 0}
    submit_no = 0
    for i, item in enumerate(history, 1):
        content = item.get("content", "") or ""
        typ = record_type(item)
        result = item.get("result", "") or ""
        reason = item.get("reason", "") or ""
        reply = item.get("reply", "") or ""
        type_counts[typ] = type_counts.get(typ, 0) + 1
        if typ == "clarify":
            parts.append(
                f"--- 交互 #{i} | type=clarify ---\n"
                f"用户问题: {content[:500]}\n"
                f"Bot回复: {reply[:300]}\n"
            )
        elif typ == "review":
            parts.append(
                f"--- 交互 #{i} | type=review ---\n"
                f"用户问题: {content[:500]}\n"
                f"Bot: {reply[:300]}\n"
            )
        elif typ == "submit":
            submit_no += 1
            if result in {"correct", "incorrect"}:
                submit_result_counts[result] += 1
            else:
                submit_result_counts["other"] += 1
            parts.append(
                f"--- 交互 #{i} | type=submit | submit #{submit_no} | result={result} ---\n"
                f"用户提交: {content[:500]}\n"
                f"Judge判定: {reason[:300]}\n"
                f"Bot回复: {reply[:300]}\n"
            )
        else:
            parts.append(
                f"--- 交互 #{i} | type=unknown | result={result} ---\n"
                f"用户内容: {content[:500]}\n"
                f"Bot回复: {reply[:300]}\n"
            )

    submit_stats = (
        f"correct={submit_result_counts['correct']}，"
        f"incorrect={submit_result_counts['incorrect']}"
    )
    if submit_result_counts["other"]:
        submit_stats += f"，other={submit_result_counts['other']}"
    stats = (
        f"统计：clarify={type_counts.get('clarify', 0)}，"
        f"submit={type_counts.get('submit', 0)}（{submit_stats}），"
        f"review={type_counts.get('review', 0)}。"
    )
    return "\n".join([stats, *parts])


def _load_user_problem_history(
    group_id: int,
    user_id: int,
    pid: str,
    *,
    scope: str = GROUP_SCOPE,
) -> list[dict]:
    if scope == PRIVATE_SCOPE:
        history = load_private_problem_history(user_id, pid)
    else:
        history = load_user_submissions(group_id, user_id)
    items = [item for item in history if item.get("problem") == pid]
    return sorted(items, key=lambda item: str(item.get("timestamp", "")))


def _unique_user_ids(user_ids: list[int] | None) -> list[int]:
    seen: set[int] = set()
    result: list[int] = []
    for user_id in user_ids or []:
        try:
            normalized = int(user_id)
        except (TypeError, ValueError):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _request_id(req: PendingRequest) -> str:
    if req.seq:
        return f"local-{req.group_id}-{req.user_id}-{req.command}-{req.message_id}-{req.seq}"
    return ""


def _context_record(
    req: PendingRequest,
    *,
    result: str = "pending",
    reason: str = "",
    reply: str = "",
    problem: str = "",
) -> dict:
    record = {
        "timestamp": req.admitted_wall.isoformat(),
        "type": req.kind,
        "content": req.payload,
        "result": result,
        "reason": reason,
        "reply": reply,
        "problem": problem or req.target_pid,
    }
    request_id = _request_id(req)
    if request_id:
        record["request_id"] = request_id
    return record


def _is_problem_solved_in_scoreboard(group_id: int, pid: str) -> bool:
    if not pid:
        return False
    sb = load_scoreboard(group_id)
    return _scoreboard_has_problem_solve(sb, pid)


def _scoreboard_has_problem_solve(sb: dict, pid: str) -> bool:
    if not pid:
        return False
    for solve in sb.get("solves", []):
        if str(solve.get("problem", "") or "") == str(pid):
            return True
    return False


def _format_submit_wait_seconds(remaining: int) -> str:
    if remaining >= 60:
        minutes = (remaining + 59) // 60
        return f"请等待 {minutes} 分钟后再提交"
    return f"请等待 {remaining} 秒后再提交"


def _format_group_submit_message_for_remaining(user_id: int, remaining: int) -> str:
    user_group = get_user_group(user_id)
    template = (
        user_group.submit_delay_message
        or f"{user_group.display_name}用户{{wait}}"
    )
    return template.replace("{wait}", _format_submit_wait_seconds(remaining))


def _cumulative_solves(sb: dict, user_id: int) -> int:
    return sum(1 for solve in sb.get("solves", []) if str(solve.get("user_id")) == str(user_id))


def _top5_entries(group_id: int, sb: dict, user_id: int) -> list[dict]:
    return build_scoreboard_entries(
        group_id,
        sb,
        user_group_name=get_user_group(user_id).name,
    )[:5]


def _update_scoreboard_for_pid(
    group_id: int,
    user_id: int,
    nickname: str,
    pid: str,
    problem: dict | None,
) -> tuple[bool, int, list[dict], dict]:
    """Record a solve for the admitted problem, not necessarily current state.
    Returns (is_first_blood, solved_count, top5_entries, scoreboard_dict)."""
    sb = load_scoreboard(group_id)
    sb.setdefault("solves", [])
    uid = str(user_id)

    for solve in sb["solves"]:
        if str(solve.get("user_id")) == uid and str(solve.get("problem", "") or "") == pid:
            solved = _cumulative_solves(sb, user_id)
            return False, solved, _top5_entries(group_id, sb, user_id), sb

    problem_solves = [solve for solve in sb["solves"] if str(solve.get("problem", "") or "") == pid]
    is_first_blood = len(problem_solves) == 0
    rating = problem.get("rating") if problem else None
    remember_problem_rating(group_id, pid, rating)

    entry = {
        "user_id": user_id,
        "nickname": nickname,
        "date": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"),
        "problem": pid,
        "order": len(sb["solves"]) + 1,
    }
    sb["solves"].append(entry)
    current = get_today_problem(group_id)
    current_pid = str(current.get("today", "") or "") if current else ""
    if current_pid != pid:
        settle_dynamic_submit_wait_for_problem(sb, pid)
    save_scoreboard(group_id, sb)
    solved = _cumulative_solves(sb, user_id)
    return is_first_blood, solved, _top5_entries(group_id, sb, user_id), sb


async def _reveal_problem_source(group_id: int) -> str:
    picker_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "kouhai_bot", "problems", "picker.py",
    )
    picker_path = os.path.abspath(os.path.normpath(picker_path))
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, picker_path, "reveal", "--group", str(group_id),
            "--data-dir", str(get_config().data_dir),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode == 0:
            reveal = stdout.decode().strip()
            if reveal and "还没有发过题哦" not in reveal:
                return reveal.replace("上一道题来自", "本题来自")
    except Exception:
        pass
    return ""


class GroupCoordinator:
    def __init__(self, group_id: int, *, scope: str = GROUP_SCOPE, owner_id: int | None = None):
        self.group_id = group_id
        self.scope = scope
        self.owner_id = int(owner_id if owner_id is not None else group_id)
        self.lock = asyncio.Lock()
        self.active: dict[int, PendingRequest] = {}
        self.clear_watermarks: dict[tuple[str, int, int, str], int] = {}
        self.submit_candidates: dict[str, list[PendingRequest]] = {}
        self.scoring_pids: set[str] = set()
        self.next_seq = 1

    def _enqueue_locked(self, req: PendingRequest) -> None:
        req.seq = self.next_seq
        self.next_seq += 1
        self.active[req.seq] = req
        resolve_pids = self._discard_superseded_submits_locked(req)
        if req.kind in _USER_CONTEXT_WRITERS and req.target_pid:
            record = _context_record(req, problem=req.target_pid)
            if req.is_private:
                save_private_submission(req.user_id, record)
            else:
                save_user_submission(req.group_id, req.user_id, record)
        if req.submit_score_candidate and req.submit_pid:
            self.submit_candidates.setdefault(req.submit_pid, []).append(req)
        req.task = asyncio.create_task(
            self._run_request(req),
            name=f"stateful_{req.command}_{req.group_id}_{req.seq}",
        )
        for pid in resolve_pids:
            asyncio.create_task(
                self._resolve_submit_scores(pid),
                name=f"resolve_submit_scores_{req.group_id}_{pid}",
            )

    async def enqueue(self, req: PendingRequest) -> None:
        async with self.lock:
            self._enqueue_locked(req)
        await req.done_event.wait()

    def status(self) -> dict | None:
        if not self.active:
            return None
        return min(self.active.values(), key=lambda item: item.admitted_at).status_dict()

    def _log_finished(
        self,
        req: PendingRequest,
        status: str,
        *,
        problem: str = "",
        extra: dict | None = None,
    ) -> None:
        return

    def _finish_request_locked(self, req: PendingRequest) -> str:
        self.active.pop(req.seq, None)
        pid = (req.compute_result or {}).get("pid", "") or req.submit_pid
        if pid:
            candidates = self.submit_candidates.get(pid)
            if candidates:
                candidates[:] = [c for c in candidates if c.seq != req.seq]
                if not candidates:
                    self.submit_candidates.pop(pid, None)
        req.done_event.set()
        return pid

    def _discard_superseded_submits_locked(self, req: PendingRequest) -> set[str]:
        if req.kind not in {"submit", "clear"} or not req.target_pid:
            return set()

        resolved_pids: set[str] = set()
        for old in list(self.active.values()):
            if old is req:
                continue
            if old.kind != "submit":
                continue
            if old.user_id != req.user_id or old.target_pid != req.target_pid:
                continue
            if old.seq >= req.seq or old.done_event.is_set():
                continue
            if old.discarded or old.submit_terminal_started:
                continue

            old.discarded = True
            old.compute_result = {"kind": "discarded", "pid": old.submit_pid}
            if req.kind == "submit":
                record = _context_record(old, result="superseded", problem=old.submit_pid)
                if old.is_private:
                    save_private_submission(old.user_id, record)
                else:
                    save_user_submission(old.group_id, old.user_id, record)
            self._log_finished(old, "stale", problem=old.submit_pid)
            resolved_pids.add(self._finish_request_locked(old))
            if old.task and not old.task.done():
                old.task.cancel()
        return {pid for pid in resolved_pids if pid}

    async def _finish_request(self, req: PendingRequest) -> None:
        async with self.lock:
            self._finish_request_locked(req)

    async def _load_user_problem_history_for_request(
        self,
        req: PendingRequest,
        pid: str,
        user_id: int | None = None,
    ) -> list[dict]:
        target_user_id = req.user_id if user_id is None else user_id
        async with self.lock:
            request_id = _request_id(req)
            history = [
                item for item in _load_user_problem_history(
                    req.group_id,
                    target_user_id,
                    pid,
                    scope=req.scope if target_user_id == req.user_id else GROUP_SCOPE,
                )
                if not request_id or str(item.get("request_id", "") or "") != request_id
            ]
        return sorted(history, key=lambda item: str(item.get("timestamp", "")))

    async def _save_context_record(self, req: PendingRequest, record: dict) -> bool:
        async with self.lock:
            key = (req.scope, req.group_id, req.user_id, str(record.get("problem", "") or ""))
            if self.clear_watermarks.get(key, 0) >= req.seq:
                return False
            if req.is_private:
                save_private_submission(req.user_id, record)
            else:
                save_user_submission(req.group_id, req.user_id, record)
            return True

    async def _run_request(self, req: PendingRequest) -> None:
        try:
            if req.discarded:
                return
            if req.kind == "submit":
                req.compute_result = await self._compute_submit(req)
            elif req.kind == "clarify":
                req.compute_result = await self._compute_clarify(req)
            elif req.kind == "review":
                req.compute_result = await self._compute_review(req)
            else:
                req.compute_result = {"kind": "noop"}
        except asyncio.CancelledError:
            req.compute_result = {"kind": "cancelled"}
        except Exception as e:
            logger.error(
                "[group_%s] compute error on %s seq=%s: %s",
                req.group_id, req.command, req.seq, e, exc_info=True,
            )
            req.compute_result = {"kind": "error", "error": str(e)}
        try:
            if req.discarded:
                return
            if req.kind == "submit":
                await self._finalize_submit(req)
            elif req.kind == "clarify":
                await self._finalize_clarify(req)
            elif req.kind == "review":
                await self._finalize_review(req)
            elif req.kind == "clear":
                await self._finalize_clear(req)
            else:
                await self._finish_request(req)
        except Exception as e:
            logger.error(
                "[group_%s] coordinator error on %s seq=%s: %s",
                self.group_id, req.command, req.seq, e, exc_info=True,
            )
            self._log_finished(req, "error", extra={"error": str(e)[:500]})
            await _send_req_plain(req, f"处理 /{req.command} 时出了点问题，稍后再试试？")
            await self._finish_request(req)

    async def _compute_submit(self, req: PendingRequest) -> dict:
        pid = req.submit_pid
        if not pid:
            return {"kind": "no_problem"}
        if req.submit_already_solved_at_enqueue:
            return {"kind": "already_solved", "pid": pid}

        problem_text = load_problem_statement(pid)
        if not problem_text:
            return {"kind": "no_statement", "pid": pid}

        cfg = get_config()
        if cfg.submit_ac_backdoor and cfg.submit_ac_backdoor in req.payload:
            return {
                "kind": "judge",
                "pid": pid,
                "result": {
                    "correct": True,
                    "reason": "SUBMIT_AC_BACKDOOR matched",
                    "reply": "",
                    "reaction": "",
                },
            }

        history = await self._load_user_problem_history_for_request(req, pid)
        await _react_req(req, random.choice(["128064", "289"]))
        logger.info(
            "[group_%s] first judge start pid=%s seq=%s user=%s history_items=%s",
            req.group_id,
            pid,
            req.seq,
            req.user_id,
            len(history),
        )
        result = await _judge_llm_limited(problem_text, req.payload, history)
        if not result.text:
            logger.warning(
                "[group_%s] first judge failed pid=%s seq=%s provider=%s model=%s failure=%s",
                req.group_id,
                pid,
                req.seq,
                result.provider_name,
                result.model,
                result.failure_kind,
            )
            return {"kind": result.failure_kind or "error", "pid": pid}
        parsed, _repair_tag = await parse_json_with_llm_repair(
            result.text,
            expected_schema='{ "correct": boolean, "reason": string, "reply": string, "reaction": string }',
            task="summary",
            timeout=get_config().summary_timeout_sec,
        )
        if not parsed:
            logger.warning(
                "[group_%s] first judge malformed JSON after repair pid=%s seq=%s provider=%s model=%s",
                req.group_id,
                pid,
                req.seq,
                result.provider_name,
                result.model,
            )
            return {"kind": "service_unavailable", "pid": pid}
        logger.info(
            "[group_%s] first judge result pid=%s seq=%s provider=%s model=%s correct=%s reaction=%s reason=%s",
            req.group_id,
            pid,
            req.seq,
            result.provider_name,
            result.model,
            parsed.get("correct"),
            parsed.get("reaction", ""),
            _log_preview(parsed.get("reason", "")),
        )
        if parsed.get("correct", False) and parsed.get("reaction", "") != "123":
            editorial = get_official_editorial(pid) if has_cached_editorial_zh(pid) else None
            if editorial:
                source = "\n".join(
                    part for part in [editorial.tutorial_title, editorial.tutorial_url]
                    if part
                )
                logger.info(
                    "[group_%s] second judge start pid=%s seq=%s user=%s source=%s",
                    req.group_id,
                    pid,
                    req.seq,
                    req.user_id,
                    _log_preview(source),
                )
                second = await _second_judge_llm_limited(
                    problem_text,
                    req.payload,
                    history,
                    parsed,
                    editorial.text,
                    source,
                )
                if second.text:
                    second_parsed, _second_repair_tag = await parse_json_with_llm_repair(
                        second.text,
                        expected_schema='{ "correct": boolean, "reason": string, "reply": string, "reaction": string }',
                        task="summary",
                        timeout=get_config().summary_timeout_sec,
                    )
                    if "correct" in second_parsed:
                        logger.info(
                            "[group_%s] second judge result pid=%s seq=%s provider=%s model=%s correct=%s reaction=%s reason=%s",
                            req.group_id,
                            pid,
                            req.seq,
                            second.provider_name,
                            second.model,
                            second_parsed.get("correct"),
                            second_parsed.get("reaction", ""),
                            _log_preview(second_parsed.get("reason", "")),
                        )
                        return {
                            "kind": "judge",
                            "pid": pid,
                            "result": second_parsed,
                            "model_tag": second.model_tag,
                            "first_judge_result": parsed,
                            "second_judge": True,
                        }
                logger.warning(
                    "[group_%s] second judge unavailable or malformed for %s seq=%s provider=%s model=%s failure=%s has_text=%s; blocking first-pass correct verdict",
                    req.group_id,
                    pid,
                    req.seq,
                    second.provider_name,
                    second.model,
                    second.failure_kind,
                    bool(second.text),
                )
                return {"kind": second.failure_kind or "service_unavailable", "pid": pid}
        return {"kind": "judge", "pid": pid, "result": parsed, "model_tag": result.model_tag}


    async def _compute_clarify(self, req: PendingRequest) -> dict:
        pid = req.target_pid
        if not pid:
            return {"kind": "no_problem"}
        problem_text = load_problem_statement(pid)
        if not problem_text:
            return {"kind": "no_statement", "pid": pid}

        cfg = get_config()
        summary = (
            get_problem_summary(req.group_id, pid)
            if req.is_private
            else _load_latest_group_summary(req.group_id)
        )
        await _react_req(req, random.choice(["128064", "289"]))
        result = await _call_llm_limited(
            [
                {"role": "system", "content": CLARIFY_PROMPT},
                {"role": "user", "content": (
                    f"题目中文简述：\n{summary[:2000] if summary else '(暂无简述)'}\n\n"
                    f"题目英文原文：\n{problem_text[:5000]}\n\n"
                    f"群友的问题：\n{req.payload}"
                )},
            ],
            task="clarify",
            timeout=cfg.clarify_timeout_sec,
            response_format={"type": "json_object"},
            thinking={"type": "enabled"},
        )
        if not result.text:
            return {"kind": result.failure_kind or "error", "pid": pid}

        parsed, _repair_tag = await parse_json_with_llm_repair(
            result.text,
            expected_schema='{ "reply": string, "reaction": string }',
            task="summary",
            timeout=get_config().summary_timeout_sec,
        )
        if not parsed:
            return {"kind": "unavailable", "pid": pid}

        return {"kind": "clarify", "pid": pid, "parsed": parsed, "model_tag": result.model_tag}

    async def _compute_review(self, req: PendingRequest) -> dict:
        pid = req.review_pid
        if not pid:
            return {"kind": "no_review_problem"}

        problem_text = load_problem_statement(pid)
        if not problem_text:
            return {"kind": "no_statement", "pid": pid}

        history = await self._load_user_problem_history_for_request(req, pid)
        history_str = _build_review_history(history)

        user_parts = [
            f"题目原文：\n{problem_text[:8000]}",
            f"发起人在此题的提交/判定记录：\n{history_str if history_str else '(无)'}",
        ]
        if req.review_mentioned_user_ids:
            mentioned_parts = []
            for mentioned_uid in req.review_mentioned_user_ids:
                mentioned_history = await self._load_user_problem_history_for_request(
                    req,
                    pid,
                    mentioned_uid,
                )
                mentioned_history_str = (
                    _build_review_history(mentioned_history)
                    if mentioned_history
                    else "(无)"
                )
                mentioned_parts.append(
                    f"用户 {mentioned_uid}：\n"
                    f"{mentioned_history_str}"
                )
            user_parts.append(
                "被 @ 群友在此题的上下文：\n" + "\n\n".join(mentioned_parts)
            )
        editorial = get_official_editorial(pid)
        if editorial:
            user_parts.append(format_editorial_for_review(editorial))
        user_parts.append(f"用户的问题：\n{req.payload}")

        cfg = get_config()
        await _react_req(req, random.choice(["128064", "289"]))
        result = await _call_llm_limited(
            [
                {"role": "system", "content": REVIEW_PROMPT},
                {"role": "user", "content": "\n\n".join(user_parts)},
            ],
            task="review",
            timeout=cfg.review_timeout_sec,
            thinking={"type": "enabled"},
        )
        if not result.text:
            return {"kind": result.failure_kind or "error", "pid": pid}

        reply = result.text.strip()
        if not reply:
            return {"kind": "empty", "pid": pid}

        return {"kind": "review", "pid": pid, "reply": reply, "model_tag": result.model_tag}

    async def _send_already_solved(self, req: PendingRequest) -> None:
        if req.is_private:
            await _send_req_plain(req, "这题已经通过啦，可以直接 /review 复盘。")
        else:
            await send_group_msg(req.group_id, [
                build_at(req.user_id),
                build_text(" 已经有人解出本题了～想刷下一道可以发 /newproblem 哦"),
            ])

    async def _send_llm_failure(self, req: PendingRequest) -> None:
        await _react_req(req, "268")
        await _send_req_plain(req, _LLM_FAILURE_TEXT)

    async def _send_correct_only(
        self,
        req: PendingRequest,
        model_tag: str = "",
        *,
        waiting_for_earlier: bool = False,
    ) -> None:
        if req.submit_waiting_reply_sent:
            return
        if waiting_for_earlier:
            text = " 做法被判定为正确了～前面还有更早发出的提交正在判题，排行榜结果需要等它们结束后再确认。"
        else:
            text = " 做法被判定为正确了～"
        if model_tag:
            text = text.rstrip() + model_tag
        await _send_req_plain(req, text)
        req.submit_waiting_reply_sent = True

    def _problem_source_from_snapshot(self, req: PendingRequest, pid: str) -> str:
        problem = req.submit_problem or {}
        name = str(problem.get("name", "") or "")
        rating = str(problem.get("rating", "") or "")
        parts = [f"CF{pid}"]
        if name:
            parts.append(name)
        if rating and rating != "?":
            parts.append(rating)
        return f"本题来自 {' '.join(parts)}✨" if parts else ""

    async def _send_private_success(self, req: PendingRequest, pid: str, model_tag: str = "") -> None:
        mark_private_solved(req.user_id, pid, source="private")
        current_pid = ""
        current = get_today_problem(req.group_id)
        if current:
            current_pid = str(current.get("today", "") or "")
        if current_pid and current_pid == pid:
            text = (
                f"做对了 {pid}！🎉 private judge 不直接加分。"
                "如果群里还没人通过这题，可以到服务群发 /sync，把这次通过同步到群榜。"
            )
        else:
            text = f"做对了 {pid}！🎉 这次通过只记录在 private judge，不计入群榜。"
        if model_tag:
            text = text.rstrip() + model_tag
        self._log_finished(req, "correct", problem=pid, extra={"scope": PRIVATE_SCOPE})
        await _send_req_plain(req, text)

    async def _send_scoreboard_success(self, req: PendingRequest, pid: str, model_tag: str = "") -> None:
        async with self.lock:
            is_fb, solved, top5, sb = _update_scoreboard_for_pid(
                req.group_id,
                req.user_id,
                req.nickname,
                pid,
                req.submit_problem,
            )
            user_group = get_user_group(req.user_id)
            ranked = build_scoreboard_entries(req.group_id, sb, user_group_name=user_group.name)
            current_entry = next(
                (entry for entry in ranked if str(entry["user_id"]) == str(req.user_id)),
                None,
            )
            rating = load_known_problem_ratings(req.group_id, {pid}).get(pid)

        self._log_finished(req, "correct", problem=pid)
        try:
            from ...annotations.exporter import export_problem_annotation_bundle
            export_problem_annotation_bundle(req.group_id, pid, source="auto_on_first_solve")
        except Exception as e:
            logger.warning(
                "[group_%s] failed to export annotation bundle for %s: %s",
                req.group_id, pid, e, exc_info=True,
            )

        rank = int(current_entry["rank"]) if current_entry else 1
        total_score = format_points(float(current_entry["score"])) if current_entry else "0"
        score_gain = format_points(rating_to_points(rating)) if rating is not None else "0"

        if is_fb:
            cheer = f"恭喜拿下本题一血！🎉 本题 +{score_gain} 分（共 {solved} 题，总分 {total_score}），当前第 {rank}"
        else:
            cheer = f"做对了！🎉 本题 +{score_gain} 分（共 {solved} 题，总分 {total_score}），当前第 {rank}"
        lines = [cheer]
        if top5:
            nickname_map = await fetch_group_member_nickname_map(req.group_id)
            top_title = "🏆 Top 5：" if is_default_group(user_group) else f"🏆 {user_group.display_name} Top 5："
            lines.extend(["", top_title])
            for entry in top5:
                uid = str(entry["user_id"])
                name = nickname_map.get(uid) or entry["nickname"] or uid
                lines.append(
                    f"{entry['rank']}. {name} ({entry['solved']} 题，{format_points(entry['score'])} 分)"
                )
        reveal = self._problem_source_from_snapshot(req, pid) or await _reveal_problem_source(req.group_id)
        if reveal:
            lines.extend(["", reveal])
        resp_text = "\n".join(lines)
        if model_tag:
            resp_text = resp_text.rstrip() + model_tag
        await send_group_msg(req.group_id, [
            build_at(req.user_id),
            build_text(f" {resp_text}"),
        ])
        schedule_post_solve_editorial_followup(req.group_id, pid)

    async def _resolve_submit_scores(self, pid: str) -> None:
        while True:
            score_req: PendingRequest | None = None
            post_solve: list[PendingRequest] = []
            async with self.lock:
                if pid in self.scoring_pids:
                    await asyncio.sleep(0.1)
                    continue
                candidates = sorted(self.submit_candidates.get(pid, []), key=lambda item: item.seq)
                solved = _is_problem_solved_in_scoreboard(self.group_id, pid)
                for candidate in candidates:
                    if candidate.submit_score_resolved:
                        continue
                    if not candidate.submit_judge_done:
                        break
                    if not candidate.submit_correct:
                        candidate.submit_score_resolved = True
                        continue
                    if solved:
                        candidate.submit_score_resolved = True
                        post_solve.append(candidate)
                        continue
                    candidate.submit_score_resolved = True
                    self.scoring_pids.add(pid)
                    score_req = candidate
                    break

            for candidate in post_solve:
                model_tag = (candidate.compute_result or {}).get("model_tag", "")
                if not candidate.submit_waiting_reply_sent:
                    await self._send_correct_only(candidate, model_tag)
                self._log_finished(
                    candidate,
                    "post_solve_correct",
                    problem=pid,
                    extra={"after_first_solve": True},
                )
                await self._finish_request(candidate)

            if score_req is None:
                return

            try:
                model_tag = (score_req.compute_result or {}).get("model_tag", "")
                await self._send_scoreboard_success(score_req, pid, model_tag)
                await self._finish_request(score_req)
            finally:
                async with self.lock:
                    self.scoring_pids.discard(pid)

    async def _finalize_submit(self, req: PendingRequest) -> None:
        if req.discarded:
            return
        req.submit_terminal_started = True
        result = req.compute_result or {}
        kind = result.get("kind")
        if kind == "no_problem":
            self._log_finished(req, "no_problem")
            if req.is_private:
                await _send_req_plain(req, "当前还没有 private judge 题目～先发 /setproblem 设置一道题吧。")
            else:
                await send_group_msg(req.group_id, build_plain_message(
                    f"@{req.nickname} 当前还没有题目可以提交～发 /newproblem 刷新一道？"
                ))
            await self._finish_request(req)
            return
        if kind == "bad_pid":
            self._log_finished(req, "bad_pid")
            await _send_req_plain(req, "加载题目信息失败，稍后再试试？")
            await self._finish_request(req)
            return
        if kind == "already_solved":
            self._log_finished(req, "already_solved", problem=result.get("pid", ""))
            await self._send_already_solved(req)
            await self._finish_request(req)
            return
        if kind == "no_statement":
            self._log_finished(req, "no_statement", problem=result.get("pid", ""))
            await _send_req_plain(req, "加载题目信息失败，稍后再试试？")
            req.submit_judge_done = True
            req.submit_correct = False
            await self._save_context_record(
                req,
                _context_record(req, result="no_statement", problem=result.get("pid", "")),
            )
            await self._resolve_submit_scores(pid=result.get("pid", ""))
            await self._finish_request(req)
            return
        if kind in {"timeout", "service_unavailable", "cancelled", "error"}:
            status = "timeout" if kind == "timeout" else "service_unavailable"
            self._log_finished(req, status, problem=result.get("pid", ""))
            await self._send_llm_failure(req)
            req.submit_judge_done = True
            req.submit_correct = False
            await self._save_context_record(
                req,
                _context_record(req, result=status, problem=result.get("pid", "")),
            )
            await self._resolve_submit_scores(pid=result.get("pid", ""))
            await self._finish_request(req)
            return

        pid = result.get("pid", "")
        parsed = result.get("result", {})
        correct = parsed.get("correct", False)
        reaction = parsed.get("reaction", "")
        reply = parsed.get("reply", "")
        reason = parsed.get("reason", "")
        model_tag = result.get("model_tag", "")

        if reaction == "123":
            self._log_finished(req, "offtopic", problem=pid)
            await _react_req(req, "123")
            req.submit_judge_done = True
            req.submit_correct = False
            await self._resolve_submit_scores(pid)
            await self._finish_request(req)
            return

        await self._save_context_record(
            req,
            _context_record(
                req,
                result="correct" if correct else "incorrect",
                reason=reason,
                reply=reply,
                problem=pid,
            ),
        )
        req.submit_judge_done = True
        req.submit_correct = bool(correct)

        if correct:
            if req.is_private:
                await self._send_private_success(req, pid, model_tag)
                await self._finish_request(req)
                return
            if req.submit_score_candidate:
                async with self.lock:
                    earlier_waiting = any(
                        candidate.seq < req.seq
                        and not candidate.submit_judge_done
                        for candidate in self.submit_candidates.get(pid, [])
                    )
                if earlier_waiting:
                    await self._send_correct_only(req, model_tag, waiting_for_earlier=True)
                await self._resolve_submit_scores(pid)
            else:
                await self._send_correct_only(req, model_tag)
                self._log_finished(
                    req,
                    "post_solve_correct",
                    problem=pid,
                    extra={"after_first_solve": True},
                )
                await self._finish_request(req)
            return

        if reply:
            reply = re.sub(r'@\S+', '', reply.replace("\U0001f605", "\u2764\ufe0f")).strip()
            if model_tag:
                reply = reply.rstrip() + model_tag
            self._log_finished(req, "incorrect", problem=pid)
            await _send_req_plain(req, reply)
            await self._resolve_submit_scores(pid)
            await self._finish_request(req)
            return

        reason = reason or "做法不太对呢"
        if model_tag:
            reason = reason.rstrip() + model_tag
        self._log_finished(req, "incorrect", problem=pid)
        await _send_req_plain(req, f"{reason}。再想想？🤔")
        await self._resolve_submit_scores(pid)
        await self._finish_request(req)

    async def _finalize_clarify(self, req: PendingRequest) -> None:
        result = req.compute_result or {}
        kind = result.get("kind")
        if kind == "no_problem":
            self._log_finished(req, "no_problem")
            if req.is_private:
                await _send_req_plain(req, "当前还没有 private judge 题目～先发 /setproblem 设置一道题吧。")
            else:
                await send_group_msg(req.group_id, build_plain_message(
                    f"@{req.nickname} 还没有今日题目哦～"
                ))
            await self._finish_request(req)
            return
        if kind == "bad_pid":
            self._log_finished(req, "bad_pid")
            await _send_req_plain(req, "读取题目信息失败～")
            await self._finish_request(req)
            return
        if kind == "no_statement":
            self._log_finished(req, "no_statement", problem=result.get("pid", ""))
            await _send_req_plain(req, "抱歉，题面缓存不可用～")
            await self._save_context_record(
                req,
                _context_record(req, result="no_statement", problem=result.get("pid", "")),
            )
            await self._finish_request(req)
            return
        if kind in {"timeout", "service_unavailable", "cancelled", "error", "unavailable"}:
            status = "timeout" if kind == "timeout" else "service_unavailable"
            self._log_finished(req, status, problem=result.get("pid", ""))
            await self._send_llm_failure(req)
            await self._save_context_record(
                req,
                _context_record(req, result=status, problem=result.get("pid", "")),
            )
            await self._finish_request(req)
            return

        pid = result.get("pid", "")
        parsed = result.get("parsed", {})
        if parsed.get("reaction") == "123":
            self._log_finished(req, "offtopic", problem=pid)
            await _react_req(req, "123")
            await self._finish_request(req)
            return

        reply = parsed.get("reply", "") or ""
        if not reply:
            self._log_finished(req, "service_unavailable", problem=pid)
            await self._send_llm_failure(req)
            await self._save_context_record(
                req,
                _context_record(req, result="service_unavailable", problem=pid),
            )
            await self._finish_request(req)
            return

        if len(reply) > 500:
            reply = reply[:500] + "…"
        reply = reply.replace("😅", "❤️")
        model_tag = result.get("model_tag", "")
        if model_tag:
            reply = reply.rstrip() + model_tag
        await _send_req_plain(req, reply)

        await self._save_context_record(
            req,
            _context_record(req, result="clarify", reply=reply, problem=pid),
        )
        self._log_finished(req, "ok", problem=pid)
        await self._finish_request(req)

    async def _finalize_review(self, req: PendingRequest) -> None:
        result = req.compute_result or {}
        kind = result.get("kind")
        if kind == "no_review_problem":
            self._log_finished(req, "no_review_problem")
            await _send_req_plain(req, "还没有已通过的题目可以 review 哦～先做出一道再来聊吧！")
            await self._finish_request(req)
            return
        if kind == "no_statement":
            self._log_finished(req, "no_statement", problem=result.get("pid", ""))
            await _send_req_plain(req, "抱歉，题面缓存不可用～")
            await self._save_context_record(
                req,
                _context_record(req, result="no_statement", problem=result.get("pid", "")),
            )
            await self._finish_request(req)
            return
        if kind in {"timeout", "service_unavailable", "empty", "cancelled", "error"}:
            status = "timeout" if kind == "timeout" else "service_unavailable"
            self._log_finished(req, status, problem=result.get("pid", ""))
            await self._send_llm_failure(req)
            await self._save_context_record(
                req,
                _context_record(req, result=status, problem=result.get("pid", "")),
            )
            await self._finish_request(req)
            return

        pid = result.get("pid", "")
        if not pid:
            self._log_finished(req, "error")
            await _send_req_plain(req, "读取题目信息失败～")
            await self._finish_request(req)
            return

        reply = result.get("reply", "").replace("😅", "❤️")
        model_tag = result.get("model_tag", "")
        if model_tag:
            reply = reply.rstrip() + model_tag

        if len(reply) > _REVIEW_FORWARD_THRESHOLD:
            cfg = get_config()
            chunks = _chunk_text(reply, _REVIEW_CHUNK_SIZE)
            logger.info(
                "[group_%s] /review seq=%s user=%s pid=%s using forward-card path: reply_len=%s chunks=%s",
                req.group_id,
                req.seq,
                req.user_id,
                pid,
                len(reply),
                len(chunks),
            )
            node_ids: list[str] = []
            for idx, chunk in enumerate(chunks, 1):
                self_resp = await send_private_msg(cfg.bot_qq, build_plain_message(chunk))
                if not self_resp:
                    logger.warning(
                        "[group_%s] /review seq=%s self-send chunk %s/%s failed; falling back to direct group messages",
                        req.group_id,
                        req.seq,
                        idx,
                        len(chunks),
                    )
                    node_ids = []
                    break
                logger.info(
                    "[group_%s] /review seq=%s self-send chunk %s/%s ok: node_id=%s chunk_len=%s",
                    req.group_id,
                    req.seq,
                    idx,
                    len(chunks),
                    self_resp,
                    len(chunk),
                )
                node_ids.append(str(self_resp))
            if node_ids:
                await asyncio.sleep(0.5)
                if req.is_private:
                    fwd_resp = await send_private_forward_msg(
                        req.user_id,
                        [{"type": "node", "data": {"id": node_id}} for node_id in node_ids],
                    )
                else:
                    fwd_resp = await send_group_forward_msg(
                        req.group_id,
                        [{"type": "node", "data": {"id": node_id}} for node_id in node_ids],
                    )
                if fwd_resp:
                    logger.info(
                        "[group_%s] /review seq=%s forward-card send ok: fwd_msg_id=%s node_count=%s",
                        req.group_id,
                        req.seq,
                        fwd_resp,
                        len(node_ids),
                    )
                    if not req.is_private:
                        await send_group_msg(req.group_id, [
                            build_at(req.user_id),
                            build_text(" 回复较长，已折叠到卡片里啦 👆"),
                        ])
                else:
                    logger.warning(
                        "[group_%s] /review seq=%s forward-card send failed after self-send; falling back to direct group messages",
                        req.group_id,
                        req.seq,
                    )
                    for idx, chunk in enumerate(chunks, 1):
                        logger.info(
                            "[group_%s] /review seq=%s direct-send fallback chunk %s/%s len=%s",
                            req.group_id,
                            req.seq,
                            idx,
                            len(chunks),
                            len(chunk),
                        )
                        if req.is_private:
                            await send_private_msg(req.user_id, build_plain_message(chunk))
                        else:
                            await send_group_msg(req.group_id, build_plain_message(chunk))
            else:
                for idx, chunk in enumerate(chunks, 1):
                    logger.info(
                        "[group_%s] /review seq=%s direct-send fallback chunk %s/%s len=%s",
                        req.group_id,
                        req.seq,
                        idx,
                        len(chunks),
                        len(chunk),
                    )
                    if req.is_private:
                        await send_private_msg(req.user_id, build_plain_message(chunk))
                    else:
                        await send_group_msg(req.group_id, build_plain_message(chunk))
        else:
            logger.info(
                "[group_%s] /review seq=%s user=%s pid=%s using direct-send path: reply_len=%s",
                req.group_id,
                req.seq,
                req.user_id,
                pid,
                len(reply),
            )
            await _send_req_plain(req, reply)

        await self._save_context_record(
            req,
            _context_record(req, result="review", reply=reply, problem=pid),
        )
        self._log_finished(req, "ok", problem=pid)
        await self._finish_request(req)

    async def _finalize_clear(self, req: PendingRequest) -> None:
        pid = req.target_pid
        if not pid:
            await _react_req(req, "10060")
            await self._finish_request(req)
            return

        async with self.lock:
            if req.is_private:
                clear_private_problem_history(req.user_id, pid)
            else:
                clear_user_problem_submissions(req.group_id, req.user_id, pid)
            key = (req.scope, req.group_id, req.user_id, pid)
            self.clear_watermarks[key] = max(self.clear_watermarks.get(key, 0), req.seq)
        await _react_req(req, "128076")
        self._log_finished(req, "ok", problem=pid)
        await self._finish_request(req)


def get_group_lock_status(group_id: int) -> dict | None:
    _refresh_runtime()
    coord = _coordinators.get(_coordinator_key(group_id))
    return coord.status() if coord else None


def get_private_lock_status(user_id: int, group_id: int | None = None) -> dict | None:
    _refresh_runtime()
    coord = _coordinators.get(_coordinator_key(group_id or 0, scope=PRIVATE_SCOPE, user_id=user_id))
    return coord.status() if coord else None


def has_active_problem_request(
    *,
    scope: str,
    group_id: int,
    user_id: int,
    pid: str,
) -> bool:
    _refresh_runtime()
    coord = _coordinators.get(_coordinator_key(group_id, scope=scope, user_id=user_id))
    if coord is None:
        return False
    target = str(pid or "")
    for req in coord.active.values():
        if req.user_id != user_id:
            continue
        if req.kind not in _USER_CONTEXT_WRITERS:
            continue
        if req.target_pid == target or req.submit_pid == target or req.review_pid == target:
            return True
    return False


async def run_group_state_update(group_id: int, fn: Callable[[], T | Awaitable[T]]) -> T:
    """Run a short group JSON state update under the group's coordinator lock."""
    coord = _get_coordinator(group_id)
    async with coord.lock:
        result = fn()
        if inspect.isawaitable(result):
            return await result
        return result


async def enqueue_submit_request(
    group_id: int,
    user_id: int,
    sender: dict,
    message_id: str,
    submission: str,
    *,
    scope: str = GROUP_SCOPE,
) -> str:
    coord = _get_coordinator(group_id, scope=scope, user_id=user_id)
    req: PendingRequest | None = None
    async with coord.lock:
        sb = load_scoreboard(group_id)
        if scope == GROUP_SCOPE:
            wait_window = effective_submit_delay_sec_for_scoreboard(user_id, sb)
            posted_at = get_problem_posted_at(group_id)
            if wait_window > 0 and posted_at is not None:
                remaining = wait_window - (time.time() - posted_at)
                if remaining > 0:
                    return _format_group_submit_message_for_remaining(
                        user_id,
                        int(math.ceil(remaining)),
                    )
            problem = get_today_problem(group_id)
        else:
            problem = get_private_current_problem(user_id)
        submit_pid = problem.get("today", "") if problem else ""
        if scope == PRIVATE_SCOPE:
            already_solved = bool(
                submit_pid
                and (
                    is_private_solved(user_id, submit_pid)
                    or is_group_problem_solved(group_id, submit_pid)
                )
            )
        else:
            already_solved = bool(submit_pid and _scoreboard_has_problem_solve(sb, submit_pid))
        req = PendingRequest(
            kind="submit",
            group_id=group_id,
            user_id=user_id,
            sender=sender,
            message_id=message_id,
            command="submit",
            nickname=get_display_name(sender),
            scope=scope,
            payload=submission,
            submit_pid=submit_pid,
            submit_problem=problem,
            target_pid=submit_pid,
            submit_already_solved_at_enqueue=already_solved,
            submit_score_candidate=bool(scope == GROUP_SCOPE and submit_pid and not already_solved),
        )
        coord._enqueue_locked(req)
    await req.done_event.wait()
    return ""


async def enqueue_clarify_request(
    group_id: int,
    user_id: int,
    sender: dict,
    message_id: str,
    question: str,
    *,
    scope: str = GROUP_SCOPE,
) -> None:
    coord = _get_coordinator(group_id, scope=scope, user_id=user_id)
    problem = get_private_current_problem(user_id) if scope == PRIVATE_SCOPE else get_today_problem(group_id)
    pid = problem.get("today", "") if problem else ""
    req = PendingRequest(
        kind="clarify",
        group_id=group_id,
        user_id=user_id,
        sender=sender,
        message_id=message_id,
        command="clarify",
        nickname=get_display_name(sender),
        scope=scope,
        payload=question,
        target_pid=pid,
    )
    await coord.enqueue(req)
    await req.done_event.wait()


async def enqueue_review_request(
    group_id: int,
    user_id: int,
    sender: dict,
    message_id: str,
    question: str,
    review_pid: str,
    mentioned_user_ids: list[int] | None = None,
    *,
    scope: str = GROUP_SCOPE,
) -> None:
    coord = _get_coordinator(group_id, scope=scope, user_id=user_id)
    req = PendingRequest(
        kind="review",
        group_id=group_id,
        user_id=user_id,
        sender=sender,
        message_id=message_id,
        command="review",
        nickname=get_display_name(sender),
        scope=scope,
        payload=question,
        review_pid=review_pid,
        review_mentioned_user_ids=_unique_user_ids(mentioned_user_ids),
        target_pid=review_pid,
    )
    await coord.enqueue(req)
    await req.done_event.wait()


async def enqueue_clear_request(
    group_id: int,
    user_id: int,
    sender: dict,
    message_id: str,
    *,
    scope: str = GROUP_SCOPE,
) -> None:
    coord = _get_coordinator(group_id, scope=scope, user_id=user_id)
    problem = get_private_current_problem(user_id) if scope == PRIVATE_SCOPE else get_today_problem(group_id)
    pid = problem.get("today", "") if problem else ""
    req = PendingRequest(
        kind="clear",
        group_id=group_id,
        user_id=user_id,
        sender=sender,
        message_id=message_id,
        command="clear",
        nickname=get_display_name(sender),
        scope=scope,
        target_pid=pid,
    )
    await coord.enqueue(req)
    await req.done_event.wait()


async def _redirect_blocked_group_submit_to_private(
    group_id: int,
    user_id: int,
    sender: dict,
    message_id: str,
    submission: str,
) -> bool:
    problem = get_today_problem(group_id)
    pid = str(problem.get("today", "") or "") if problem else ""
    if not problem or not pid:
        return False

    set_private_current_problem(user_id, problem)
    if not load_private_problem_history(user_id, pid):
        group_history = group_problem_history(group_id, user_id, pid)
        if group_history:
            replace_private_problem_history(user_id, pid, copy_records(group_history))
    first = not has_group_problem_private_notified(user_id, pid)
    if first:
        intro_id = await send_private_msg(user_id, build_plain_message(
            "你现在还在群提交等待期，这次提交已转到 private judge；我也帮你把当前群题设好了。"
        ))
        if intro_id is None:
            await send_group_msg(group_id, [
                build_at(user_id),
                build_text(" 我想把这次提交转到 private judge，但私聊发送失败了；这次先不撤回也不判题，检查一下是否能接收临时会话？"),
            ])
            return True
        await send_problem_card_private(user_id, group_id, problem, prefer_group_card=True)

    repeated = await send_private_msg(user_id, build_plain_message(
        f"刚才被撤回的提交内容：\n{submission}\n\n开始 judge～"
    ))
    if repeated is None:
        await send_group_msg(group_id, [
            build_at(user_id),
            build_text(" 私聊复述提交失败了；这次先不撤回也不判题，联系管理员看一下私聊权限吧。"),
        ])
        return True

    mark_group_problem_private_notified(user_id, pid)

    try:
        await delete_msg(message_id)
    except Exception:
        logger.warning("[group_%s] failed to delete early starred submit %s", group_id, message_id)

    await enqueue_submit_request(
        group_id,
        user_id,
        sender,
        message_id,
        submission,
        scope=PRIVATE_SCOPE,
    )
    return True


async def handle(group_id: int, user_id: int, sender: dict,
                 message_id: str, raw_text: str, segments: list,
                 event: dict) -> None:
    """Handle /submit command."""
    nickname = get_display_name(sender)
    scope = PRIVATE_SCOPE if event.get("message_type") == "private" else GROUP_SCOPE

    stripped = raw_text.lstrip()
    match = re.match(r'/submit\s+', stripped)
    if not match:
        if scope == PRIVATE_SCOPE:
            await send_private_msg(user_id, build_plain_message("用法：/submit 你的做法。试试看？"))
        else:
            await send_group_msg(group_id, build_plain_message(
                f"@{nickname} 用法：/submit 你的做法。试试看？"
            ))
        return

    submission = stripped[match.end():].strip()
    if not submission:
        if scope == PRIVATE_SCOPE:
            await send_private_msg(user_id, build_plain_message(
                "/submit 后面要写你的做法呀，只发 /submit 我不知道你做了啥 ❤️"
            ))
        else:
            await send_group_msg(group_id, build_plain_message(
                f"@{nickname} /submit 后面要写你的做法呀，只发 /submit 我不知道你做了啥 😅"
            ))
        return

    if is_curfew_active():
        msg = format_curfew_message()
        if scope == PRIVATE_SCOPE:
            await send_private_msg(user_id, build_plain_message(msg))
        else:
            await send_group_msg(group_id, build_plain_message(msg))
        return

    blocked_message = await enqueue_submit_request(
        group_id,
        user_id,
        sender,
        message_id,
        submission,
        scope=scope,
    )
    if blocked_message:
        user_group = get_user_group(user_id)
        if scope == GROUP_SCOPE and is_dynamic_submit_delay_enabled(user_group):
            if await _redirect_blocked_group_submit_to_private(
                group_id,
                user_id,
                sender,
                message_id,
                submission,
            ):
                return
        await send_group_msg(group_id, [
            build_at(user_id),
            build_text(f" {blocked_message}"),
        ])
        return


def register() -> None:
    registry.register(CommandDef(
        name="submit",
        aliases=["sbm"],
        description="提交做法，AI 判定对错",
        usage="你的做法",
        handler=handle,
        cooldown=10,
    ))
