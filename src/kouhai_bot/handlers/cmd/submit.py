"""/submit — judge a user's solution against the group's current problem.

This module also owns the runtime coordinator for stateful commands:
/submit, /clarify, /review, and /clear. Group-only, openid-native, DB-backed.

The concurrency design is preserved from the legacy bridge: admission-time pid
snapshots, per-group sequence numbers, parallel LLM compute (global limit 8),
same-user submit replacement, clear watermarks, seq-ordered first-blood
resolution, and save-before-deliver. State lives in SQLite (group = view);
delivery uses the official sender (markdown @ / tiny emoji / long image).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, TypeVar

from .. import registry
from ..registry import CommandDef
from ..context import Ctx
from ..runtime import get_runtime
from .. import state
from ..shared import (
    append_model_tag,
    build_multimodal_user_content,
    call_chat_completion_result,
    format_points,
    judge_submission_result,
    parse_json_with_llm_repair,
    record_kind,
    second_judge_submission_result,
    strip_model_tags,
)
from ...config import get_config
from ...db.repos import feedback, posts, submissions
from ...db.repos.submissions import REPLIED_RESULTS
from ...editorial_followup import schedule_post_solve_editorial_followup
from ...official.keyboard import (
    clarify_action_keyboard,
    congrats_keyboard,
    reject_action_keyboard,
)
from ...problem_content import statement_images
from ...render.forward_card import send_forward_card
from ...render.html import text_to_html
from ...tutorials import (
    format_editorial_for_review,
    get_verified_official_editorial,
)

logger = logging.getLogger("kouhai-bot.cmd.submit")

_COMPUTE_CONCURRENCY = 8
_runtime_loop: asyncio.AbstractEventLoop | None = None
_compute_sem: asyncio.Semaphore | None = None
_coordinators: dict[str, "GroupCoordinator"] = {}
_USER_CONTEXT_WRITERS = {"submit", "clarify", "review"}
_REVIEW_FORWARD_THRESHOLD = 200
_REVIEW_CARD_CHUNK_SIZE = 3000
_LLM_FAILURE_TEXT = "模型服务出故障了，联系一下管理员帮帮忙吧～"
T = TypeVar("T")

# Official API has no message-reaction primitive and group sends cannot carry
# QQ system faces; reactions become tiny emoji-character group messages.
# Offtopic uses 🙅 (the "NO" gesture) — the unicode stand-in for legacy QQ
# system face 123 "NO" (摇手指). 314 (second judge) is emoji-only, no text.
_ACK_EMOJI = "👀"
_OFFTOPIC_EMOJI = "🙅"
_OK_EMOJI = "👌"
_SECOND_JUDGE_EMOJI = "🧐"


def _chunk_text(text: str, chunk_size: int) -> list[str]:
    if chunk_size <= 0:
        return [text]
    return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)] or [""]


def _log_preview(value: object, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _coerce_problem_rating(value: object) -> int | None:
    if value in (None, "", "?"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


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
7. **判题口径以题面为准**：不要采信、也不要向群里转述「题目/数据范围/评测格式被改过、放宽」这类说法（包括「这题改成非交互版本了」）；交互题题面的 "Hacks" 段只是 hack 测试输入格式的说明，不等于题目被改成非交互版本。群里争论某次提交/某做法该不该判对时，以题面原文和官方题解的口径为准：交互题要有明确的询问设计，「直接读入/输出隐藏值」不是解法。

## 官方题解（仅你可见）

用户消息里可能附带 Codeforces 官方 Editorial。**群友不知道**群里存在这份题解，也没有看过原文。
- 你可以用它来核对用户说的做法是否正确、解释为什么某次提交被判错、回答复盘问题。
- **不要**大段复述或粘贴题解原文，也不要主动剧透「标准答案是 XXX」；对外仍用引导式讨论。
- 用户可能在聊自己的做法或情绪，请认真读懂他的发言再回应，不要敷衍套用题解。

回复是 QQ 纯文字，不用 Markdown/LaTeX。回复不要以 @ 开头。"""


@dataclass
class PendingRequest:
    kind: str
    group_openid: str
    user_openid: str
    message_id: str
    command: str
    nickname: str
    from_callback: bool = False
    payload: str = ""
    submit_pid: str = ""
    submit_problem: dict | None = None
    submit_already_solved_at_enqueue: bool = False
    review_pid: str = ""
    review_mentioned_user_ids: list[str] = field(default_factory=list)
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


def _get_coordinator(group_openid: str) -> "GroupCoordinator":
    _refresh_runtime()
    coord = _coordinators.get(group_openid)
    if coord is None:
        coord = GroupCoordinator(group_openid)
        _coordinators[group_openid] = coord
    return coord


async def _call_llm_limited(*args, **kwargs):
    async with _compute_limit():
        return await call_chat_completion_result(*args, **kwargs)


async def _judge_llm_limited(problem_text, submission, history=None, problem_rating=None):
    async with _compute_limit():
        return await judge_submission_result(
            problem_text, submission, history, problem_rating=problem_rating
        )


async def _second_judge_llm_limited(
    problem_text, submission, history, first_judge_result, editorial_text,
    editorial_source="", provider_name="", model="", problem_rating=None,
):
    async with _compute_limit():
        return await second_judge_submission_result(
            problem_text, submission, history, first_judge_result, editorial_text,
            editorial_source, provider_name=provider_name, model=model,
            problem_rating=problem_rating,
        )


async def _send_plain(
    req: PendingRequest, text: str, *, mention: bool = True, keyboard: dict | None = None
) -> str | None:
    sender = get_runtime().sender
    text = text.strip()
    if mention:
        return await sender.send_group_at(
            req.group_openid, req.user_openid, text, msg_id=req.message_id, keyboard=keyboard
        )
    return await sender.send_group_text(req.group_openid, text, msg_id=req.message_id)


async def _react(req: PendingRequest, emoji: str) -> None:
    await get_runtime().sender.react(
        req.group_openid, emoji, msg_id=req.message_id, user_openid=req.user_openid
    )


def _build_review_history(history: list[dict]) -> str:
    def record_type(item: dict) -> str:
        return record_kind(item) or "unknown"

    parts = []
    type_counts = {"clarify": 0, "submit": 0, "review": 0, "unknown": 0}
    submit_result_counts = {"correct": 0, "incorrect": 0, "other": 0}
    submit_no = 0
    for i, item in enumerate(history, 1):
        content = item.get("content", "") or ""
        typ = record_type(item)
        result = item.get("result", "") or ""
        reason = strip_model_tags(item.get("reason", "") or "")
        reply = strip_model_tags(item.get("reply", "") or "")
        type_counts[typ] = type_counts.get(typ, 0) + 1
        if typ == "clarify":
            parts.append(
                f"--- 交互 #{i} | type=clarify ---\n"
                f"用户问题: {content[:500]}\nBot回复: {reply[:300]}\n"
            )
        elif typ == "review":
            parts.append(
                f"--- 交互 #{i} | type=review ---\n"
                f"用户问题: {content[:500]}\nBot: {reply[:300]}\n"
            )
        elif typ == "submit":
            submit_no += 1
            if result in {"correct", "incorrect"}:
                submit_result_counts[result] += 1
            else:
                submit_result_counts["other"] += 1
            parts.append(
                f"--- 交互 #{i} | type=submit | submit #{submit_no} | result={result} ---\n"
                f"用户提交: {content[:500]}\nJudge判定: {reason[:300]}\nBot回复: {reply[:300]}\n"
            )
        else:
            parts.append(
                f"--- 交互 #{i} | type=unknown | result={result} ---\n"
                f"用户内容: {content[:500]}\nBot回复: {reply[:300]}\n"
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


def _request_id(req: PendingRequest) -> str:
    if req.seq:
        return f"local-{req.group_openid}-{req.user_openid}-{req.command}-{req.message_id}-{req.seq}"
    return ""


def _context_record(
    req: PendingRequest,
    *,
    result: str = "pending",
    reason: str = "",
    reply: str = "",
    problem: str = "",
    model_tag: str = "",
) -> dict:
    record = {
        "group_openid": req.group_openid,
        "user_openid": req.user_openid,
        "pid": problem or req.target_pid,
        "type": req.kind,
        "content": req.payload,
        "result": result,
        "reason": reason,
        "reply": reply,
        "model_tag": model_tag,
        "seq": req.seq,
        "timestamp": req.admitted_wall.isoformat(),
    }
    request_id = _request_id(req)
    if request_id:
        record["request_id"] = request_id
    return record


class GroupCoordinator:
    def __init__(self, group_openid: str):
        self.group_openid = group_openid
        self.lock = asyncio.Lock()
        self.active: dict[int, PendingRequest] = {}
        self.clear_watermarks: dict[tuple[str, str, str], int] = {}
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
            asyncio.create_task(self._save_record(record))
        if req.submit_score_candidate and req.submit_pid:
            self.submit_candidates.setdefault(req.submit_pid, []).append(req)
        req.task = asyncio.create_task(
            self._run_request(req),
            name=f"stateful_{req.command}_{req.group_openid}_{req.seq}",
        )
        for pid in resolve_pids:
            asyncio.create_task(
                self._resolve_submit_scores(pid),
                name=f"resolve_submit_scores_{self.group_openid}_{pid}",
            )

    async def _save_record(self, record: dict) -> None:
        await submissions.save_submission(get_runtime().db, record)

    async def enqueue(self, req: PendingRequest) -> None:
        async with self.lock:
            self._enqueue_locked(req)
        await req.done_event.wait()

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
            if old is req or old.kind != "submit":
                continue
            if old.user_openid != req.user_openid or old.target_pid != req.target_pid:
                continue
            if old.seq >= req.seq or old.done_event.is_set():
                continue
            if old.discarded or old.submit_terminal_started:
                continue
            old.discarded = True
            old.compute_result = {"kind": "discarded", "pid": old.submit_pid}
            if req.kind == "submit":
                record = _context_record(old, result="superseded", problem=old.submit_pid)
                asyncio.create_task(self._save_record(record))
            resolved_pids.add(self._finish_request_locked(old))
            if old.task and not old.task.done():
                old.task.cancel()
        return {pid for pid in resolved_pids if pid}

    async def _finish_request(self, req: PendingRequest) -> None:
        async with self.lock:
            self._finish_request_locked(req)

    async def _load_history(self, req: PendingRequest, pid: str, user_openid: str | None = None) -> list[dict]:
        target = req.user_openid if user_openid is None else user_openid
        async with self.lock:
            request_id = _request_id(req)
            history = await submissions.get_user_problem_submissions(
                get_runtime().db, self.group_openid, target, pid,
                exclude_request_id=request_id or None,
            )
        return sorted(history, key=lambda item: str(item.get("timestamp", "")))

    async def _save_context_record(self, req: PendingRequest, record: dict) -> bool:
        async with self.lock:
            key = (self.group_openid, req.user_openid, str(record.get("pid", "") or ""))
            if self.clear_watermarks.get(key, 0) >= req.seq:
                return False
            await submissions.save_submission(get_runtime().db, record)
            if record.get("result") in REPLIED_RESULTS:
                await feedback.set_last_interaction(
                    get_runtime().db, self.group_openid, req.user_openid, record
                )
            return True

    async def _remove_context_record(self, req: PendingRequest) -> None:
        async with self.lock:
            request_id = _request_id(req)
            if request_id:
                await submissions.remove_submission(get_runtime().db, request_id)

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
                req.group_openid, req.command, req.seq, e, exc_info=True,
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
                self.group_openid, req.command, req.seq, e, exc_info=True,
            )
            await _send_plain(req, f"处理 /{req.command} 时出了点问题，稍后再试试？")
            await self._finish_request(req)

    async def _compute_submit(self, req: PendingRequest) -> dict:
        db = get_runtime().db
        pid = req.submit_pid
        if not pid:
            return {"kind": "no_problem"}
        if req.submit_already_solved_at_enqueue:
            return {"kind": "already_solved", "pid": pid}

        problem_text = await state.problem_statement_text(db, pid)
        if not problem_text:
            return {"kind": "no_statement", "pid": pid}

        cfg = get_config()
        if cfg.submit_ac_backdoor and cfg.submit_ac_backdoor in req.payload:
            return {
                "kind": "judge", "pid": pid,
                "result": {"correct": True, "reason": "SUBMIT_AC_BACKDOOR matched", "reply": "", "reaction": ""},
            }

        history = await self._load_history(req, pid)
        await _react(req, _ACK_EMOJI)
        problem_rating = _coerce_problem_rating((req.submit_problem or {}).get("rating"))
        result = await _judge_llm_limited(problem_text, req.payload, history, problem_rating=problem_rating)
        if not result.text:
            logger.warning(
                "[group_%s] first judge failed pid=%s seq=%s failure=%s",
                req.group_openid, pid, req.seq, result.failure_kind,
            )
            return {"kind": result.failure_kind or "error", "pid": pid}
        parsed, _ = await parse_json_with_llm_repair(
            result.text,
            expected_schema='{ "correct": boolean, "reason": string, "reply": string, "reaction": string }',
            task="summary", timeout=cfg.summary_timeout_sec,
        )
        if not parsed:
            return {"kind": "service_unavailable", "pid": pid}
        logger.info(
            "[group_%s] first judge pid=%s seq=%s correct=%s reaction=%s",
            req.group_openid, pid, req.seq, parsed.get("correct"), parsed.get("reaction", ""),
        )
        if parsed.get("correct", False) and parsed.get("reaction", "") != "123":
            editorial = await get_verified_official_editorial(pid)
            if editorial:
                await _react(req, _SECOND_JUDGE_EMOJI)
                source = "\n".join(p for p in [editorial.tutorial_title, editorial.tutorial_url] if p)
                second = await _second_judge_llm_limited(
                    problem_text, req.payload, history, parsed, editorial.text, source,
                    problem_rating=problem_rating,
                )
                if second.text:
                    second_parsed, _ = await parse_json_with_llm_repair(
                        second.text,
                        expected_schema='{ "correct": boolean, "reason": string, "reply": string, "reaction": string }',
                        task="summary", timeout=cfg.summary_timeout_sec,
                    )
                    if "correct" in second_parsed:
                        return {
                            "kind": "judge", "pid": pid, "result": second_parsed,
                            "model_tag": second.model_tag, "first_judge_result": parsed, "second_judge": True,
                        }
                logger.warning(
                    "[group_%s] second judge unavailable for %s seq=%s; falling back to first-pass correct",
                    req.group_openid, pid, req.seq,
                )
                return {
                    "kind": "judge", "pid": pid, "result": parsed, "model_tag": result.model_tag,
                    "second_judge_failure": second.failure_kind or "service_unavailable",
                }
        return {"kind": "judge", "pid": pid, "result": parsed, "model_tag": result.model_tag}

    async def _compute_clarify(self, req: PendingRequest) -> dict:
        db = get_runtime().db
        pid = req.target_pid
        if not pid:
            return {"kind": "no_problem"}
        stmt = await state.problem_statement_json(db, pid)
        problem_text = await state.problem_statement_text(db, pid)
        if not problem_text:
            return {"kind": "no_statement", "pid": pid}

        cfg = get_config()
        images = statement_images(stmt)
        summary = await state.verified_summary(db, pid)
        await _react(req, _ACK_EMOJI)
        user_prompt = (
            f"题目中文简述：\n{summary[:2000] if summary else '(暂无简述)'}\n\n"
            f"题目英文原文：\n{problem_text[:5000]}\n\n"
            f"群友的问题：\n{req.payload}"
        )
        user_content = await build_multimodal_user_content(user_prompt, images) if images else user_prompt
        result = await _call_llm_limited(
            [{"role": "system", "content": CLARIFY_PROMPT}, {"role": "user", "content": user_content}],
            task="multimodal_clarify" if images else "clarify",
            timeout=cfg.clarify_timeout_sec,
            response_format={"type": "json_object"},
            thinking={"type": "enabled"},
        )
        if not result.text:
            return {"kind": result.failure_kind or "error", "pid": pid}
        parsed, _ = await parse_json_with_llm_repair(
            result.text, expected_schema='{ "reply": string, "reaction": string }',
            task="summary", timeout=cfg.summary_timeout_sec,
        )
        if not parsed:
            return {"kind": "unavailable", "pid": pid}
        return {"kind": "clarify", "pid": pid, "parsed": parsed, "model_tag": result.model_tag}

    async def _compute_review(self, req: PendingRequest) -> dict:
        db = get_runtime().db
        pid = req.review_pid
        if not pid:
            return {"kind": "no_review_problem"}
        problem_text = await state.problem_statement_text(db, pid)
        if not problem_text:
            return {"kind": "no_statement", "pid": pid}

        problem_rating = await state.problem_rating(db, pid)
        history = await self._load_history(req, pid)
        history_str = _build_review_history(history)
        user_parts = [
            f"题目原文：\n{problem_text[:8000]}",
            f"发起人在此题的提交/判定记录：\n{history_str if history_str else '(无)'}",
        ]
        if req.review_mentioned_user_ids:
            mentioned_parts = []
            for uid in req.review_mentioned_user_ids:
                mh = await self._load_history(req, pid, uid)
                mentioned_parts.append(f"用户 {uid}：\n{_build_review_history(mh) if mh else '(无)'}")
            user_parts.append("被 @ 群友在此题的上下文：\n" + "\n\n".join(mentioned_parts))
        editorial = await get_verified_official_editorial(pid)
        if editorial:
            user_parts.append(format_editorial_for_review(editorial))
        user_parts.append(f"用户的问题：\n{req.payload}")

        cfg = get_config()
        await _react(req, _ACK_EMOJI)
        result = await _call_llm_limited(
            [{"role": "system", "content": REVIEW_PROMPT}, {"role": "user", "content": "\n\n".join(user_parts)}],
            task="review", timeout=cfg.review_timeout_sec,
            thinking={"type": "enabled"}, problem_rating=problem_rating,
        )
        if not result.text:
            return {"kind": result.failure_kind or "error", "pid": pid}
        reply = result.text.strip()
        if not reply:
            return {"kind": "empty", "pid": pid}
        return {"kind": "review", "pid": pid, "reply": reply, "model_tag": result.model_tag}

    async def _send_already_solved(self, req: PendingRequest) -> None:
        await _send_plain(req, "已经有人解出本题了～想刷下一道可以发 /np 哦")

    async def _send_llm_failure(self, req: PendingRequest) -> None:
        await _send_plain(req, _LLM_FAILURE_TEXT)

    async def _send_correct_only(self, req: PendingRequest, model_tag: str = "", *, waiting_for_earlier: bool = False) -> None:
        if req.submit_waiting_reply_sent:
            return
        if waiting_for_earlier:
            text = "做法被判定为正确了～前面还有更早发出的提交正在判题，排行榜结果需要等它们结束后再确认。"
        else:
            text = "做法被判定为正确了～"
        await _send_plain(req, append_model_tag(text, model_tag))
        req.submit_waiting_reply_sent = True

    def _problem_label(self, req: PendingRequest, pid: str) -> str:
        problem = req.submit_problem or {}
        name = str(problem.get("name", "") or "").strip()
        rating = str(problem.get("rating", "") or "").strip()
        parts = [f"CF{pid}"]
        if name:
            parts.append(name)
        if rating and rating != "?":
            parts.append(rating)
        return " ".join(parts)

    async def _send_scoreboard_success(self, req: PendingRequest, pid: str, model_tag: str = "") -> None:
        db = get_runtime().db
        async with self.lock:
            solve = await posts.record_solve(db, self.group_openid, req.user_openid, pid, req.nickname)
            ranked = await posts.group_scoreboard(db, self.group_openid)
            current = next((e for e in ranked if e["user_openid"] == req.user_openid), None)
            rating = await state.problem_rating(db, pid)

        is_fb = solve["is_first_blood"]
        solved = solve["solved_count"]
        total_score = format_points(float(current["score"])) if current else "0"
        score_gain = format_points(posts.rating_to_points(rating)) if rating is not None else "0"

        if is_fb:
            cheer = f"恭喜拿下本题一血！🎉 本题 +{score_gain} 分（共 {solved} 题，总分 {total_score}）"
        else:
            cheer = f"做对了！🎉 本题 +{score_gain} 分（共 {solved} 题，总分 {total_score}）"
        reveal = f"本题来自 {self._problem_label(req, pid)}✨"
        resp_text = append_model_tag(f"{cheer}\n\n{reveal}", model_tag)
        await _send_plain(req, resp_text, keyboard=congrats_keyboard(pid))
        schedule_post_solve_editorial_followup(self.group_openid, pid)

    async def _resolve_submit_scores(self, pid: str) -> None:
        db = get_runtime().db
        while True:
            score_req: PendingRequest | None = None
            post_solve: list[PendingRequest] = []
            async with self.lock:
                busy = pid in self.scoring_pids
                if not busy:
                    candidates = sorted(self.submit_candidates.get(pid, []), key=lambda item: item.seq)
                    solved = await state.is_problem_solved(db, self.group_openid, pid)
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
            if busy:
                await asyncio.sleep(0.1)
                continue

            for candidate in post_solve:
                model_tag = (candidate.compute_result or {}).get("model_tag", "")
                if not candidate.submit_waiting_reply_sent:
                    await self._send_correct_only(candidate, model_tag)
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
            await _send_plain(req, "当前还没有题目可以提交～发 /np 刷新一道？")
            await self._finish_request(req)
            return
        if kind == "already_solved":
            await self._send_already_solved(req)
            await self._finish_request(req)
            return
        if kind == "no_statement":
            await _send_plain(req, "加载题目信息失败，稍后再试试？")
            req.submit_judge_done = True
            req.submit_correct = False
            await self._save_context_record(req, _context_record(req, result="no_statement", problem=result.get("pid", "")))
            await self._resolve_submit_scores(pid=result.get("pid", ""))
            await self._finish_request(req)
            return
        if kind in {"timeout", "service_unavailable", "cancelled", "error"}:
            status = "timeout" if kind == "timeout" else "service_unavailable"
            await self._send_llm_failure(req)
            req.submit_judge_done = True
            req.submit_correct = False
            await self._save_context_record(req, _context_record(req, result=status, problem=result.get("pid", "")))
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
            await _react(req, _OFFTOPIC_EMOJI)
            await self._remove_context_record(req)
            req.submit_judge_done = True
            req.submit_correct = False
            await self._resolve_submit_scores(pid)
            await self._finish_request(req)
            return

        await self._save_context_record(
            req,
            _context_record(req, result="correct" if correct else "incorrect", reason=reason, reply=reply, problem=pid, model_tag=model_tag),
        )
        req.submit_judge_done = True
        req.submit_correct = bool(correct)

        if correct:
            if req.submit_score_candidate:
                async with self.lock:
                    earlier_waiting = any(
                        c.seq < req.seq and not c.submit_judge_done
                        for c in self.submit_candidates.get(pid, [])
                    )
                if earlier_waiting:
                    await self._send_correct_only(req, model_tag, waiting_for_earlier=True)
                await self._resolve_submit_scores(pid)
            else:
                await self._send_correct_only(req, model_tag)
                await self._finish_request(req)
            return

        if reply:
            reply = re.sub(r'@\S+', '', reply.replace("\U0001f605", "\u2764\ufe0f")).strip()
            await _send_plain(
                req, append_model_tag(reply, model_tag), keyboard=reject_action_keyboard()
            )
            await self._resolve_submit_scores(pid)
            await self._finish_request(req)
            return

        reason = reason or "做法不太对呢"
        await _send_plain(
            req, append_model_tag(f"{reason}。再想想？🤔", model_tag),
            keyboard=reject_action_keyboard(),
        )
        await self._resolve_submit_scores(pid)
        await self._finish_request(req)

    async def _finalize_clarify(self, req: PendingRequest) -> None:
        result = req.compute_result or {}
        kind = result.get("kind")
        if kind == "no_problem":
            await _send_plain(req, "还没有今日题目哦～")
            await self._finish_request(req)
            return
        if kind == "no_statement":
            await _send_plain(req, "抱歉，题面缓存不可用～")
            await self._save_context_record(req, _context_record(req, result="no_statement", problem=result.get("pid", "")))
            await self._finish_request(req)
            return
        if kind in {"timeout", "service_unavailable", "cancelled", "error", "unavailable"}:
            status = "timeout" if kind == "timeout" else "service_unavailable"
            await self._send_llm_failure(req)
            await self._save_context_record(req, _context_record(req, result=status, problem=result.get("pid", "")))
            await self._finish_request(req)
            return

        pid = result.get("pid", "")
        parsed = result.get("parsed", {})
        if parsed.get("reaction") == "123":
            await _react(req, _OFFTOPIC_EMOJI)
            await self._remove_context_record(req)
            await self._finish_request(req)
            return

        reply = parsed.get("reply", "") or ""
        if not reply:
            await self._send_llm_failure(req)
            await self._save_context_record(req, _context_record(req, result="service_unavailable", problem=pid))
            await self._finish_request(req)
            return

        if len(reply) > 500:
            reply = reply[:500] + "…"
        reply = reply.replace("😅", "❤️")
        model_tag = result.get("model_tag", "")
        await self._save_context_record(req, _context_record(req, result="clarify", reply=reply, problem=pid, model_tag=model_tag))
        await _send_plain(req, append_model_tag(reply, model_tag), keyboard=clarify_action_keyboard())
        await self._finish_request(req)

    async def _finalize_review(self, req: PendingRequest) -> None:
        result = req.compute_result or {}
        kind = result.get("kind")
        if kind == "no_review_problem":
            await _send_plain(req, "还没有已通过的题目可以 review 哦～先做出一道再来聊吧！")
            await self._finish_request(req)
            return
        if kind == "no_statement":
            await _send_plain(req, "抱歉，题面缓存不可用～")
            await self._save_context_record(req, _context_record(req, result="no_statement", problem=result.get("pid", "")))
            await self._finish_request(req)
            return
        if kind in {"timeout", "service_unavailable", "empty", "cancelled", "error"}:
            status = "timeout" if kind == "timeout" else "service_unavailable"
            await self._send_llm_failure(req)
            await self._save_context_record(req, _context_record(req, result=status, problem=result.get("pid", "")))
            await self._finish_request(req)
            return

        pid = result.get("pid", "")
        if not pid:
            await _send_plain(req, "读取题目信息失败～")
            await self._finish_request(req)
            return

        reply = result.get("reply", "").replace("😅", "❤️")
        model_tag = result.get("model_tag", "")
        display_reply = append_model_tag(reply, model_tag)
        await self._save_context_record(req, _context_record(req, result="review", reply=reply, problem=pid, model_tag=model_tag))

        if len(display_reply) > _REVIEW_FORWARD_THRESHOLD:
            rt = get_runtime()
            bot_name = rt.sender.bot_name or "算法助手"
            chunks = _chunk_text(display_reply, _REVIEW_CARD_CHUNK_SIZE)
            nodes = [
                {"nickname": bot_name, "content_html": text_to_html(c), "content_text": c}
                for c in chunks
            ]
            mid = await send_forward_card(
                rt.sender, self.group_openid, nodes, title="复盘",
                text_fallback=display_reply, browser=rt.browser,
            )
            if mid:
                # The long image already carries the reply; a bare @ just pings the asker.
                await _send_plain(req, "")
            else:
                for chunk in chunks:
                    await get_runtime().sender.send_group_text(self.group_openid, chunk)
        else:
            await _send_plain(req, display_reply)
        await self._finish_request(req)

    async def _finalize_clear(self, req: PendingRequest) -> None:
        pid = req.target_pid
        if not pid:
            if not req.from_callback:
                await _react(req, _OK_EMOJI)
            await self._finish_request(req)
            return
        async with self.lock:
            await submissions.clear_user_problem(get_runtime().db, self.group_openid, req.user_openid, pid)
            key = (self.group_openid, req.user_openid, pid)
            self.clear_watermarks[key] = max(self.clear_watermarks.get(key, 0), req.seq)
        # A button callback already shows QQ's "操作成功" toast, so the 👌 ack
        # would be redundant; typed /clear still gets it.
        if not req.from_callback:
            await _react(req, _OK_EMOJI)
        await self._finish_request(req)


def get_group_active_requests(group_openid: str) -> list[dict]:
    """All in-flight stateful requests for a group in admission order, each as
    {nickname, command, user_openid}. Superseded/discarded requests are excluded.
    /status uses this to list who is being processed (not just the earliest)."""
    _refresh_runtime()
    coord = _coordinators.get(group_openid)
    if coord is None:
        return []
    reqs = sorted(
        (r for r in coord.active.values() if not r.discarded),
        key=lambda r: r.seq,
    )
    return [
        {"nickname": r.nickname, "command": r.command, "user_openid": r.user_openid}
        for r in reqs
    ]


def has_active_problem_request(*, group_openid: str, user_openid: str, pid: str) -> bool:
    _refresh_runtime()
    coord = _coordinators.get(group_openid)
    if coord is None:
        return False
    target = str(pid or "")
    for req in coord.active.values():
        if req.user_openid != user_openid or req.kind not in _USER_CONTEXT_WRITERS:
            continue
        if req.target_pid == target or req.submit_pid == target or req.review_pid == target:
            return True
    return False


async def run_group_state_update(group_openid: str, fn: Callable[[], T | Awaitable[T]]) -> T:
    """Run a short group state update under the group's coordinator lock."""
    coord = _get_coordinator(group_openid)
    async with coord.lock:
        result = fn()
        if inspect.isawaitable(result):
            return await result
        return result


async def enqueue_submit_request(ctx: Ctx, submission: str) -> None:
    db = ctx.db
    coord = _get_coordinator(ctx.group_openid)
    async with coord.lock:
        problem = await state.current_problem(db, ctx.group_openid)
        submit_pid = problem.get("today", "") if problem else ""
        already_solved = bool(submit_pid and await state.is_problem_solved(db, ctx.group_openid, submit_pid))
        req = PendingRequest(
            kind="submit", group_openid=ctx.group_openid, user_openid=ctx.user_openid,
            message_id=ctx.message_id, command="submit", nickname=ctx.name,
            payload=submission, submit_pid=submit_pid, submit_problem=problem,
            target_pid=submit_pid, submit_already_solved_at_enqueue=already_solved,
            submit_score_candidate=bool(submit_pid and not already_solved),
        )
        coord._enqueue_locked(req)
    await req.done_event.wait()


async def enqueue_clarify_request(ctx: Ctx, question: str) -> None:
    coord = _get_coordinator(ctx.group_openid)
    async with coord.lock:
        problem = await state.current_problem(ctx.db, ctx.group_openid)
        pid = problem.get("today", "") if problem else ""
        req = PendingRequest(
            kind="clarify", group_openid=ctx.group_openid, user_openid=ctx.user_openid,
            message_id=ctx.message_id, command="clarify", nickname=ctx.name,
            payload=question, target_pid=pid,
        )
        coord._enqueue_locked(req)
    await req.done_event.wait()


async def enqueue_review_request(
    ctx: Ctx,
    question: str,
    pid_resolver: Callable[[], Awaitable[str]],
    mentioned_user_ids: list[str] | None = None,
) -> None:
    coord = _get_coordinator(ctx.group_openid)
    seen: set[str] = set()
    mentions = []
    for uid in mentioned_user_ids or []:
        if uid and uid not in seen and uid != ctx.user_openid:
            seen.add(uid)
            mentions.append(uid)
    async with coord.lock:
        review_pid = await pid_resolver()
        req = None
        if review_pid:
            req = PendingRequest(
                kind="review", group_openid=ctx.group_openid, user_openid=ctx.user_openid,
                message_id=ctx.message_id, command="review", nickname=ctx.name,
                payload=question, review_pid=review_pid, review_mentioned_user_ids=mentions,
                target_pid=review_pid,
            )
            coord._enqueue_locked(req)
    if req is None:
        await ctx.sender.send_group_at(
            ctx.group_openid, ctx.user_openid,
            "还没有已通过的题目可以 review 哦～先做出一道再来聊吧！",
            msg_id=ctx.message_id,
        )
        return
    await req.done_event.wait()


async def enqueue_clear_request(ctx: Ctx) -> None:
    coord = _get_coordinator(ctx.group_openid)
    async with coord.lock:
        problem = await state.current_problem(ctx.db, ctx.group_openid)
        pid = problem.get("today", "") if problem else ""
        req = PendingRequest(
            kind="clear", group_openid=ctx.group_openid, user_openid=ctx.user_openid,
            message_id=ctx.message_id, command="clear", nickname=ctx.name, target_pid=pid,
            from_callback=ctx.event.get("event_type") == "INTERACTION_CREATE",
        )
        coord._enqueue_locked(req)
    await req.done_event.wait()


async def handle(ctx: Ctx) -> None:
    """Handle /submit command."""
    stripped = ctx.raw_text.lstrip()
    match = re.match(r'/submit\s+', stripped)
    if not match:
        await ctx.sender.send_group_at(ctx.group_openid, ctx.user_openid, "用法：/sbm 你的做法。试试看？", msg_id=ctx.message_id)
        return
    submission = stripped[match.end():].strip()
    if not submission:
        await ctx.sender.send_group_at(ctx.group_openid, ctx.user_openid, "/sbm 后面要写你的做法呀，只发 /sbm 我不知道你做了啥 😅", msg_id=ctx.message_id)
        return
    await enqueue_submit_request(ctx, submission)


def register() -> None:
    registry.register(CommandDef(
        name="submit",
        aliases=["sbm"],
        description="提交你的做法，AI 判题",
        usage="你的做法",
        handler=handle,
    ))