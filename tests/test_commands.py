"""Group command suite for the official-API + SQLite architecture.

Everything runs against the real handlers with a fake REST transport, a
tmp-path Database, and monkeypatched LLM helpers — no network, no browser.
Covers /submit (first blood, incorrect, offtopic, already-solved, backdoor,
second judge, same-user supersede, first-blood serialization), /clarify,
/review, /clear (incl. the clear watermark), /range, /status, /tag,
/scoreboard, /problem, /help, and process_event dispatch.

Removed-feature coverage (private judge, starred/dynamic CD, curfew,
jev/sbmjev, top5, NapCat segments) is intentionally dropped.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from kouhai_bot.config import BotConfig
from kouhai_bot.db import set_database
from kouhai_bot.db.repos import feedback, posts, problems, submissions, users
from kouhai_bot.editorial_content import OfficialEditorial, bundle_for_editorial
from kouhai_bot.handlers import process_event
from kouhai_bot.handlers.cmd import clear as clear_cmd
from kouhai_bot.handlers.cmd import clarify as clarify_cmd
from kouhai_bot.handlers.cmd import help as help_cmd
from kouhai_bot.handlers.cmd import range as range_cmd
from kouhai_bot.handlers.cmd import review as review_cmd
from kouhai_bot.handlers.cmd import stubs
from kouhai_bot.handlers.cmd import submit as submit_cmd
from kouhai_bot.handlers.context import Ctx
from kouhai_bot.handlers.runtime import Runtime, set_runtime
from kouhai_bot.llm import ChatCompletionResult
from kouhai_bot.official import OfficialSender, ReplyBudget
from kouhai_bot.problem_content import statement_fingerprint

G = "GROUP1"
U = "USER1"
U2 = "USER2"
PID = "542D"
TAG = "🐳"


# ── infrastructure ──────────────────────────────────────────────────────


class FakeRest:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []
        self.uploads: list[tuple[str, dict]] = []

    async def send_group_message(self, group_openid, payload):
        self.sent.append((group_openid, payload))
        return {"id": f"M{len(self.sent)}"}

    async def upload_group_media(self, group_openid, payload):
        self.uploads.append((group_openid, payload))
        return {"file_info": "FI", "file_uuid": "u", "ttl": 300}


@pytest.fixture
async def env(db, tmp_path, monkeypatch):
    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.config._config", cfg)
    rest = FakeRest()
    sender = OfficialSender(rest, ReplyBudget(db), bot_name="算法助手")
    set_runtime(Runtime(db=db, sender=sender, browser=None))
    set_database(db)

    async def _no_render(*args, **kwargs):
        raise RuntimeError("render disabled in tests")

    monkeypatch.setattr(
        "kouhai_bot.render.forward_card.render_card_png", _no_render
    )
    followups: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "kouhai_bot.handlers.cmd.submit.schedule_post_solve_editorial_followup",
        lambda group, pid: followups.append((group, pid)),
    )
    return SimpleNamespace(
        db=db, rest=rest, sender=sender, cfg=cfg, followups=followups
    )


_msg_counter = 0


def make_ctx(raw_text: str, *, user: str = U, group: str = G, nickname: str = "") -> Ctx:
    global _msg_counter
    _msg_counter += 1
    return Ctx(
        group_openid=group,
        user_openid=user,
        message_id=f"msg-{_msg_counter}",
        raw_text=raw_text,
        nickname=nickname,
    )


def sent_texts(rest: FakeRest) -> list[str]:
    out = []
    for _g, payload in rest.sent:
        if payload["msg_type"] == 0:
            out.append(payload["content"])
        elif payload["msg_type"] == 2:
            out.append(payload["markdown"]["content"])
        else:
            out.append(f"<media:{payload['media']['file_info']}>")
    return out


def at_tag(user: str = U) -> str:
    return f'<qqbot-at-user id="{user}"/>'


def mock_judge(monkeypatch, verdict: dict, *, tag: str = TAG):
    calls: list[dict] = []

    async def _judge(problem_text, submission, history=None, problem_rating=None):
        calls.append({
            "problem_text": problem_text,
            "submission": submission,
            "history": history,
            "problem_rating": problem_rating,
        })
        return ChatCompletionResult(
            text=json.dumps(verdict, ensure_ascii=False), model_tag=tag
        )

    monkeypatch.setattr(
        "kouhai_bot.handlers.cmd.submit.judge_submission_result", _judge
    )
    return calls


def mock_chat(monkeypatch, text: str, *, tag: str = TAG):
    calls: list[dict] = []

    async def _chat(messages, **kwargs):
        calls.append({"messages": messages, **kwargs})
        return ChatCompletionResult(text=text, model_tag=tag)

    monkeypatch.setattr(
        "kouhai_bot.handlers.cmd.submit.call_chat_completion_result", _chat
    )
    return calls


def statement_payload(name: str = "Test Problem") -> dict:
    return {
        "name": name,
        "description": "Given n, compute the answer.",
        "input": "One integer n.",
        "output": "The answer.",
        "samples": [{"input": "1\n", "output": "2\n"}],
        "time_limit": "2 seconds",
        "memory_limit": "256 megabytes",
    }


async def seed_problem(
    db,
    pid: str = PID,
    *,
    group: str = G,
    rating: int | None = 2300,
    name: str = "Test Problem",
    tags: list[str] | None = None,
    statement: dict | None = None,
    card: dict | None = None,
    post: bool = True,
) -> str:
    stmt = statement if statement is not None else statement_payload(name)
    sha = statement_fingerprint(stmt)
    await problems.upsert_problem(
        db, pid,
        contest_id=542, idx="D", name=name, rating=rating,
        tags=["dp", "greedy"] if tags is None else tags,
        statement_sha256=sha,
        statement_json=json.dumps(stmt, ensure_ascii=False),
    )
    if post:
        if card is None:
            card = {
                "post_msg": f"来看看这道新题吧！\n\n{pid} 的题意简述",
                "sample_messages": ["输入\n1\n输出\n2"],
                "notes_message": "",
                "snake_enabled": False,
            }
        await posts.post_problem(
            db, group, pid, posted_at=1700000000,
            card_json=json.dumps(card, ensure_ascii=False),
        )
    return sha


async def wait_for(predicate, timeout: float = 5.0):
    async def _loop():
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_loop(), timeout)


def verdict(correct: bool, *, reason: str = "", reply: str = "", reaction: str = "") -> dict:
    return {"correct": correct, "reason": reason, "reply": reply, "reaction": reaction}


# ── /submit: validation ─────────────────────────────────────────────────


async def test_submit_without_args_shows_usage(env):
    await submit_cmd.handle(make_ctx("/submit"))
    assert sent_texts(env.rest) == [f"{at_tag()} 用法：/submit 你的做法。试试看？"]


async def test_submit_empty_body_reminder(env):
    await submit_cmd.handle(make_ctx("/submit   "))
    assert sent_texts(env.rest) == [
        f"{at_tag()} /submit 后面要写你的做法呀，只发 /submit 我不知道你做了啥 😅"
    ]


async def test_submit_no_current_problem(env):
    await submit_cmd.handle(make_ctx("/submit 我的做法"))
    assert sent_texts(env.rest) == [
        f"{at_tag()} 当前还没有题目可以提交～发 /newproblem 刷新一道？"
    ]


# ── /submit: correct → first blood ──────────────────────────────────────


async def test_submit_correct_first_blood_scores_cheers_and_reveals(env, monkeypatch):
    await seed_problem(env.db, rating=2300, name="Nice Problem")
    calls = mock_judge(monkeypatch, verdict(True, reason="做法正确"))
    await submit_cmd.handle(make_ctx("/submit 贪心即可", nickname="Alice"))

    texts = sent_texts(env.rest)
    assert texts[0] == "👀"
    cheer = texts[1]
    assert cheer.startswith(at_tag())
    assert "恭喜拿下本题一血！🎉 本题 +2 分（共 1 题，总分 2），当前第 1" in cheer
    assert "本题来自 CF542D Nice Problem 2300✨" in cheer
    assert cheer.endswith(TAG)
    assert len(texts) == 2

    # judge saw the statement, the submission and the rating
    assert calls[0]["submission"] == "贪心即可"
    assert "Given n, compute the answer." in calls[0]["problem_text"]
    assert calls[0]["problem_rating"] == 2300

    # ledger + post solved markers
    solves = await posts.get_group_solves(env.db, G)
    assert len(solves) == 1
    assert solves[0]["user_openid"] == U
    assert solves[0]["nickname"] == "Alice"
    post = await posts.get_current_post(env.db, G)
    assert post["first_solver_openid"] == U
    assert post["solved_at"] is not None

    # record persisted with the dedicated model_tag field (reply stays raw)
    records = await submissions.get_user_problem_submissions(env.db, G, U, PID)
    assert len(records) == 1
    assert records[0]["result"] == "correct"
    assert records[0]["model_tag"] == TAG
    assert records[0]["request_id"]

    # last-interaction cache updated for /feedback
    cached = await feedback.get_last_interaction(env.db, G, U)
    assert cached["result"] == "correct"

    # editorial follow-up scheduled exactly once, outside finalize
    assert env.followups == [(G, PID)]


async def test_submit_correct_after_solved_does_not_rescore(env, monkeypatch):
    await seed_problem(env.db)
    await posts.record_solve(env.db, G, U2, PID, "Bob")
    calls = mock_judge(monkeypatch, verdict(True))
    await submit_cmd.handle(make_ctx("/submit 另一个做法"))
    texts = sent_texts(env.rest)
    assert texts == [f"{at_tag()} 已经有人解出本题了～想刷下一道可以发 /newproblem 哦"]
    assert calls == []  # judge skipped entirely
    solves = await posts.get_group_solves(env.db, G)
    assert len(solves) == 1  # no new ledger row
    assert env.followups == []


async def test_submit_ac_backdoor_skips_judge(env, monkeypatch):
    await seed_problem(env.db)
    env.cfg.submit_ac_backdoor = "OPEN SESAME"
    calls = mock_judge(monkeypatch, verdict(False, reason="should not run"))
    await submit_cmd.handle(make_ctx("/submit 做法 OPEN SESAME"))
    assert calls == []
    # backdoor returns before the 👀 ack, so the cheer is the first message
    assert "恭喜拿下本题一血" in sent_texts(env.rest)[0]
    records = await submissions.get_user_problem_submissions(env.db, G, U, PID)
    assert records[0]["result"] == "correct"


# ── /submit: incorrect ──────────────────────────────────────────────────


async def test_submit_incorrect_reply_mentions_asker(env, monkeypatch):
    await seed_problem(env.db)
    mock_judge(monkeypatch, verdict(False, reason="复杂度超了", reply="@某人 复杂度超了哦😅"))
    await submit_cmd.handle(make_ctx("/submit 暴力枚举"))
    texts = sent_texts(env.rest)
    assert texts[0] == "👀"
    # reply wins over reason; fake @ stripped; 😅 → ❤️; tag appended once
    assert texts[1] == f"{at_tag()} 复杂度超了哦❤️{TAG}"
    records = await submissions.get_user_problem_submissions(env.db, G, U, PID)
    assert records[0]["result"] == "incorrect"
    assert records[0]["reason"] == "复杂度超了"
    assert await posts.get_group_solves(env.db, G) == []
    assert env.followups == []


async def test_submit_incorrect_empty_reply_falls_back_to_reason(env, monkeypatch):
    await seed_problem(env.db)
    mock_judge(monkeypatch, verdict(False, reason="边界没处理"))
    await submit_cmd.handle(make_ctx("/submit 做法"))
    assert sent_texts(env.rest)[1] == f"{at_tag()} 边界没处理。再想想？🤔{TAG}"


async def test_submit_incorrect_no_reason_default_text(env, monkeypatch):
    await seed_problem(env.db)
    mock_judge(monkeypatch, verdict(False))
    await submit_cmd.handle(make_ctx("/submit 做法"))
    assert sent_texts(env.rest)[1] == f"{at_tag()} 做法不太对呢。再想想？🤔{TAG}"


# ── /submit: offtopic leaves no trace ───────────────────────────────────


async def test_submit_offtopic_removes_pending_record(env, monkeypatch):
    await seed_problem(env.db)
    mock_judge(monkeypatch, verdict(False, reaction="123"))
    await submit_cmd.handle(make_ctx("/submit 讲个笑话"))
    texts = sent_texts(env.rest)
    assert texts == ["👀", "😵"]  # tiny emoji messages, no verdict text
    records = await submissions.get_user_problem_submissions(env.db, G, U, PID)
    assert records == []  # pending archive removed by request_id
    assert await feedback.get_last_interaction(env.db, G, U) is None


async def test_clarify_offtopic_removes_pending_record(env, monkeypatch):
    await seed_problem(env.db)
    mock_chat(monkeypatch, json.dumps({"reply": "", "reaction": "123"}))
    await clarify_cmd.handle(make_ctx("/clarify 今天天气如何"))
    assert sent_texts(env.rest) == ["👀", "😵"]
    assert await submissions.get_user_problem_submissions(env.db, G, U, PID) == []


# ── /submit: failures ───────────────────────────────────────────────────


async def test_submit_judge_failure_saves_service_unavailable(env, monkeypatch):
    await seed_problem(env.db)

    async def _judge(*args, **kwargs):
        return ChatCompletionResult(text=None, failure_kind="timeout", model_tag=TAG)

    monkeypatch.setattr("kouhai_bot.handlers.cmd.submit.judge_submission_result", _judge)
    await submit_cmd.handle(make_ctx("/submit 做法"))
    texts = sent_texts(env.rest)
    assert texts[1] == f"{at_tag()} 模型服务出故障了，联系一下管理员帮帮忙吧～"
    records = await submissions.get_user_problem_submissions(env.db, G, U, PID)
    assert records[0]["result"] == "timeout"
    # failure notices are markable by /feedback
    cached = await feedback.get_last_interaction(env.db, G, U)
    assert cached["result"] == "timeout"


async def test_submit_missing_statement(env, monkeypatch):
    await seed_problem(env.db, statement={})
    calls = mock_judge(monkeypatch, verdict(True))
    await submit_cmd.handle(make_ctx("/submit 做法"))
    assert calls == []
    assert sent_texts(env.rest)[0] == f"{at_tag()} 加载题目信息失败，稍后再试试？"
    records = await submissions.get_user_problem_submissions(env.db, G, U, PID)
    assert records[0]["result"] == "no_statement"


# ── /submit: second judge with verified editorial ───────────────────────


async def seed_editorial(db, pid: str, sha: str) -> OfficialEditorial:
    editorial = OfficialEditorial(
        text="The intended solution is a greedy sweep with a priority queue. " * 3,
        tutorial_url="https://codeforces.com/blog/entry/1",
        tutorial_title="Editorial",
    )
    bundle = bundle_for_editorial(editorial)
    await problems.upsert_editorial_source(
        db, pid,
        tutorial_url=editorial.tutorial_url,
        tutorial_title=editorial.tutorial_title,
        sections_json=json.dumps(bundle["sections"], ensure_ascii=False),
        source_sha256="src-sha",
        statement_sha256=sha,
    )
    await problems.set_editorial_translation(db, pid, "中文题解正文。" * 30, "verified")
    return editorial


async def test_submit_second_judge_overrides_first_correct(env, monkeypatch):
    sha = await seed_problem(env.db)
    await seed_editorial(env.db, PID, sha)
    mock_judge(monkeypatch, verdict(True, reason="一审通过"))
    second_calls: list[dict] = []

    async def _second(problem_text, submission, history, first_judge_result,
                      editorial_text, editorial_source="", **kwargs):
        second_calls.append({
            "first": first_judge_result,
            "editorial_text": editorial_text,
            "editorial_source": editorial_source,
        })
        return ChatCompletionResult(
            text=json.dumps(verdict(False, reason="二审发现反例", reply="再想想边界哦")),
            model_tag="🦊",
        )

    monkeypatch.setattr(
        "kouhai_bot.handlers.cmd.submit.second_judge_submission_result", _second
    )
    await submit_cmd.handle(make_ctx("/submit 贪心"))
    texts = sent_texts(env.rest)
    assert texts[0] == "👀"
    assert texts[1] == "🧐"  # second-judge emoji reaction
    assert texts[2] == f"{at_tag()} 再想想边界哦🦊"
    assert len(second_calls) == 1
    assert second_calls[0]["first"]["correct"] is True
    assert "greedy sweep" in second_calls[0]["editorial_text"]
    assert "codeforces.com/blog/entry/1" in second_calls[0]["editorial_source"]
    records = await submissions.get_user_problem_submissions(env.db, G, U, PID)
    assert records[0]["result"] == "incorrect"
    assert records[0]["model_tag"] == "🦊"
    assert await posts.get_group_solves(env.db, G) == []


async def test_submit_no_editorial_no_second_judge(env, monkeypatch):
    await seed_problem(env.db)
    mock_judge(monkeypatch, verdict(True))

    async def _second(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("second judge must not run without editorial")

    monkeypatch.setattr(
        "kouhai_bot.handlers.cmd.submit.second_judge_submission_result", _second
    )
    await submit_cmd.handle(make_ctx("/submit 做法"))
    assert "🧐" not in sent_texts(env.rest)
    assert "恭喜拿下本题一血" in sent_texts(env.rest)[1]


# ── /submit: history context ────────────────────────────────────────────


async def test_submit_judge_sees_prior_history_not_own_pending(env, monkeypatch):
    await seed_problem(env.db)
    await submissions.save_submission(env.db, {
        "request_id": "old-1", "group_openid": G, "user_openid": U, "pid": PID,
        "type": "submit", "content": "之前的做法", "result": "incorrect",
        "reason": "旧判定", "reply": "", "timestamp": "2026-01-01T00:00:00+08:00",
    })
    calls = mock_judge(monkeypatch, verdict(False, reason="还是不对"))
    await submit_cmd.handle(make_ctx("/submit 新做法"))
    history = calls[0]["history"]
    contents = [item["content"] for item in history]
    assert "之前的做法" in contents
    assert "新做法" not in contents  # own pending archive excluded by request_id
    # the pending archive was upserted in place, not appended
    records = await submissions.get_user_problem_submissions(env.db, G, U, PID)
    assert len(records) == 2
    new = [r for r in records if r["content"] == "新做法"][0]
    assert new["result"] == "incorrect"


# ── /submit: same-user supersede ────────────────────────────────────────


async def test_second_submit_supersedes_earlier_same_user_submit(env, monkeypatch):
    await seed_problem(env.db)
    first_started = asyncio.Event()
    first_release = asyncio.Event()

    async def _judge(problem_text, submission, history=None, problem_rating=None):
        if submission == "第一版做法":
            first_started.set()
            await first_release.wait()
            return ChatCompletionResult(text=json.dumps(verdict(True)))
        return ChatCompletionResult(
            text=json.dumps(verdict(False, reason="第二版判定"), ensure_ascii=False)
        )

    monkeypatch.setattr("kouhai_bot.handlers.cmd.submit.judge_submission_result", _judge)
    ctx1 = make_ctx("/submit 第一版做法")
    ctx2 = make_ctx("/submit 第二版做法")
    task1 = asyncio.create_task(submit_cmd.handle(ctx1))
    await asyncio.wait_for(first_started.wait(), 5)
    await submit_cmd.handle(ctx2)  # supersedes ctx1 and completes
    await asyncio.wait_for(task1, 5)
    first_release.set()
    await asyncio.sleep(0.05)

    records = await submissions.get_user_problem_submissions(env.db, G, U, PID)
    by_content = {r["content"]: r for r in records}
    assert by_content["第一版做法"]["result"] == "superseded"
    assert by_content["第二版做法"]["result"] == "incorrect"
    texts = sent_texts(env.rest)
    # only the second submit produced a verdict; the first was silently dropped
    assert f"{at_tag()} 第二版判定。再想想？🤔" in texts
    assert not any("第一版" in t for t in texts)
    assert await posts.get_group_solves(env.db, G) == []


# ── /submit: first-blood serialization across users ─────────────────────


async def test_later_correct_waits_for_earlier_candidate(env, monkeypatch):
    await seed_problem(env.db)
    slow_started = asyncio.Event()
    slow_release = asyncio.Event()

    async def _judge(problem_text, submission, history=None, problem_rating=None):
        if submission == "慢做法":
            slow_started.set()
            await slow_release.wait()
        return ChatCompletionResult(text=json.dumps(verdict(True), ensure_ascii=False))

    monkeypatch.setattr("kouhai_bot.handlers.cmd.submit.judge_submission_result", _judge)
    task1 = asyncio.create_task(submit_cmd.handle(make_ctx("/submit 慢做法", user=U)))
    await asyncio.wait_for(slow_started.wait(), 5)
    task2 = asyncio.create_task(submit_cmd.handle(make_ctx("/submit 快做法", user=U2)))
    await wait_for(lambda: any(
        "前面还有更早发出的提交正在判题" in t for t in sent_texts(env.rest)
    ))
    slow_release.set()
    await asyncio.wait_for(task1, 5)
    await asyncio.wait_for(task2, 5)

    texts = sent_texts(env.rest)
    cheers = [t for t in texts if "恭喜拿下本题一血" in t]
    assert len(cheers) == 1
    assert at_tag(U) in cheers[0]  # earliest admission wins first blood
    waiting = [t for t in texts if "前面还有更早发出的提交正在判题" in t]
    assert len(waiting) == 1 and at_tag(U2) in waiting[0]

    solves = await posts.get_group_solves(env.db, G)
    assert [s["user_openid"] for s in solves] == [U]
    # the later correct submit is still saved as a correct record
    recs2 = await submissions.get_user_problem_submissions(env.db, G, U2, PID)
    assert recs2[0]["result"] == "correct"
    assert env.followups == [(G, PID)]


# ── /clarify ────────────────────────────────────────────────────────────


async def test_clarify_replies_with_markdown_at(env, monkeypatch):
    await seed_problem(env.db)
    calls = mock_chat(monkeypatch, json.dumps({"reply": "n ≤ 1e5 哦～", "reaction": ""}))
    await clarify_cmd.handle(make_ctx("/clarify n 的范围是多少"))
    texts = sent_texts(env.rest)
    assert texts[0] == "👀"
    assert texts[1] == f"{at_tag()} n ≤ 1e5 哦～{TAG}"
    assert calls[0]["task"] == "clarify"
    assert calls[0]["response_format"] == {"type": "json_object"}
    user_msg = calls[0]["messages"][1]["content"]
    assert "n 的范围是多少" in user_msg
    assert "Given n, compute the answer." in user_msg
    records = await submissions.get_user_problem_submissions(env.db, G, U, PID)
    assert records[0]["type"] == "clarify"
    assert records[0]["result"] == "clarify"
    assert records[0]["reply"] == "n ≤ 1e5 哦～"
    cached = await feedback.get_last_interaction(env.db, G, U)
    assert cached["result"] == "clarify"


async def test_clarify_uses_verified_summary(env, monkeypatch):
    sha = await seed_problem(env.db)
    await problems.set_problem_summary(env.db, PID, "给定 n，求答案。", "verified", sha)
    calls = mock_chat(monkeypatch, json.dumps({"reply": "是的哦", "reaction": ""}))
    await clarify_cmd.handle(make_ctx("/clarify 题意"))
    assert "给定 n，求答案。" in calls[0]["messages"][1]["content"]


async def test_clarify_truncates_long_reply_and_swaps_emoji(env, monkeypatch):
    await seed_problem(env.db)
    mock_chat(monkeypatch, json.dumps({"reply": "😅" + "长" * 600, "reaction": ""}))
    await clarify_cmd.handle(make_ctx("/clarify 详细解释"))
    reply_msg = sent_texts(env.rest)[1]
    assert "长" * 499 + "…" in reply_msg
    assert "😅" not in reply_msg and "❤️" in reply_msg


async def test_clarify_no_problem(env, monkeypatch):
    calls = mock_chat(monkeypatch, "{}")
    await clarify_cmd.handle(make_ctx("/clarify 问题"))
    assert sent_texts(env.rest) == [f"{at_tag()} 还没有今日题目哦～"]
    assert calls == []


async def test_clarify_usage(env):
    await clarify_cmd.handle(make_ctx("/clarify"))
    assert sent_texts(env.rest) == [f"{at_tag()} 用法：/clarify 你的问题～"]


# ── /review ─────────────────────────────────────────────────────────────


async def test_review_targets_latest_solved_problem(env, monkeypatch):
    await seed_problem(env.db, pid="100A", name="Old Problem")
    await posts.record_solve(env.db, G, U, "100A", "Alice")
    await seed_problem(env.db, pid=PID, name="Current Unsolved")
    await posts.record_solve(env.db, G, U2, PID, "Bob")  # latest solve = PID
    calls = mock_chat(monkeypatch, "这道题的贪心是对的～")
    await review_cmd.handle(make_ctx("/review 我的做法对吗"))
    texts = sent_texts(env.rest)
    assert texts[0] == "👀"
    assert texts[1] == f"{at_tag()} 这道题的贪心是对的～{TAG}"
    assert calls[0]["task"] == "review"
    # latest solved = PID (order_num 2), not the older 100A
    assert "Problem: Current Unsolved" in calls[0]["messages"][1]["content"]
    records = await submissions.get_user_problem_submissions(env.db, G, U, PID)
    assert records[0]["type"] == "review"
    assert records[0]["result"] == "review"


async def test_review_without_solved_problem_rejected_before_llm(env, monkeypatch):
    await seed_problem(env.db)
    calls = mock_chat(monkeypatch, "should not run")
    await review_cmd.handle(make_ctx("/review 聊聊"))
    assert sent_texts(env.rest) == [
        f"{at_tag()} 还没有已通过的题目可以 review 哦～先做出一道再来聊吧！"
    ]
    assert calls == []


async def test_review_folds_mentioned_user_context(env, monkeypatch):
    await seed_problem(env.db)
    await posts.record_solve(env.db, G, U, PID, "Alice")
    mentioned = "AABBCCDD11223344"
    await submissions.save_submission(env.db, {
        "group_openid": G, "user_openid": mentioned, "pid": PID,
        "type": "submit", "content": "被@群友的做法", "result": "incorrect",
        "reason": "他的判定", "reply": "", "timestamp": "2026-01-01T00:00:00+08:00",
    })
    calls = mock_chat(monkeypatch, "一起复盘～")
    await review_cmd.handle(make_ctx(f"/review 聊聊 <@!{mentioned.lower()}>"))
    user_msg = calls[0]["messages"][1]["content"]
    assert "被 @ 群友在此题的上下文" in user_msg
    assert mentioned in user_msg
    assert "被@群友的做法" in user_msg
    # mentioned user's history is read-only: only the pre-seeded record remains
    recs = await submissions.get_user_problem_submissions(env.db, G, mentioned, PID)
    assert len(recs) == 1


async def test_review_long_reply_uses_card_with_text_fallback(env, monkeypatch):
    await seed_problem(env.db)
    await posts.record_solve(env.db, G, U, PID, "Alice")
    long_reply = "复盘" * 150  # > 200 chars
    mock_chat(monkeypatch, long_reply)
    await review_cmd.handle(make_ctx("/review 详细复盘"))
    texts = sent_texts(env.rest)
    assert texts[0] == "👀"
    assert long_reply + TAG in texts  # render failed → plain-text fallback
    assert texts[-1] == f"{at_tag()} 回复较长，已折叠到卡片里啦 👆"


async def test_review_usage(env):
    await review_cmd.handle(make_ctx("/review"))
    assert sent_texts(env.rest) == [f"{at_tag()} 用法：/review 你的问题～"]


# ── /clear ──────────────────────────────────────────────────────────────


async def test_clear_removes_current_problem_history_and_acks(env, monkeypatch):
    await seed_problem(env.db)
    mock_judge(monkeypatch, verdict(False, reason="不对"))
    await submit_cmd.handle(make_ctx("/submit 做法"))
    assert len(await submissions.get_user_problem_submissions(env.db, G, U, PID)) == 1
    env.rest.sent.clear()
    await clear_cmd.handle(make_ctx("/clear"))
    assert sent_texts(env.rest) == ["👌"]
    assert await submissions.get_user_problem_submissions(env.db, G, U, PID) == []
    # the last-interaction cache survives /clear (feedback still works)
    assert (await feedback.get_last_interaction(env.db, G, U))["result"] == "incorrect"


async def test_clear_only_touches_current_problem_and_caller(env, monkeypatch):
    await seed_problem(env.db, pid="100A")
    await seed_problem(env.db, pid=PID)  # current = PID
    for pid, user in ((PID, U), ("100A", U), (PID, U2)):
        await submissions.save_submission(env.db, {
            "group_openid": G, "user_openid": user, "pid": pid,
            "type": "submit", "content": "x", "result": "incorrect",
            "timestamp": "2026-01-01T00:00:00+08:00",
        })
    await clear_cmd.handle(make_ctx("/clear"))
    assert await submissions.get_user_problem_submissions(env.db, G, U, PID) == []
    assert len(await submissions.get_user_problem_submissions(env.db, G, U, "100A")) == 1
    assert len(await submissions.get_user_problem_submissions(env.db, G, U2, PID)) == 1


async def test_clear_with_args_just_acks(env):
    await clear_cmd.handle(make_ctx("/clear 全部"))
    assert sent_texts(env.rest) == ["👌"]


async def test_clear_watermark_blocks_late_finalize_write(env, monkeypatch):
    """A submit that already entered terminal handling cannot resurrect
    history after /clear deleted it (clear watermark guard)."""
    await seed_problem(env.db)
    mock_judge(monkeypatch, verdict(False, reason="判定"))
    terminal_started = asyncio.Event()
    terminal_gate = asyncio.Event()
    original_finalize = submit_cmd.GroupCoordinator._finalize_submit

    async def _gated_finalize(self, req):
        req.submit_terminal_started = True  # protect from supersede-discard
        terminal_started.set()
        await terminal_gate.wait()
        await original_finalize(self, req)

    monkeypatch.setattr(
        submit_cmd.GroupCoordinator, "_finalize_submit", _gated_finalize
    )
    task = asyncio.create_task(submit_cmd.handle(make_ctx("/submit 做法")))
    await asyncio.wait_for(terminal_started.wait(), 5)
    await clear_cmd.handle(make_ctx("/clear"))
    terminal_gate.set()
    await asyncio.wait_for(task, 5)
    # the late finalize still replied, but its record was refused
    assert any("判定。再想想？🤔" in t for t in sent_texts(env.rest))
    assert await submissions.get_user_problem_submissions(env.db, G, U, PID) == []


async def test_clear_cancels_inflight_submit(env, monkeypatch):
    await seed_problem(env.db)
    judge_started = asyncio.Event()
    judge_release = asyncio.Event()

    async def _judge(*args, **kwargs):
        judge_started.set()
        await judge_release.wait()
        return ChatCompletionResult(text=json.dumps(verdict(True)))

    monkeypatch.setattr("kouhai_bot.handlers.cmd.submit.judge_submission_result", _judge)
    task = asyncio.create_task(submit_cmd.handle(make_ctx("/submit 做法")))
    await asyncio.wait_for(judge_started.wait(), 5)
    await clear_cmd.handle(make_ctx("/clear"))
    await asyncio.wait_for(task, 5)  # discarded → returns without a verdict
    judge_release.set()
    await asyncio.sleep(0.05)
    assert await submissions.get_user_problem_submissions(env.db, G, U, PID) == []
    assert not any("恭喜" in t for t in sent_texts(env.rest))


# ── /range ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("args", [
    "2000-2600", "2000~2600", "2000～2600", "2000到2600", "2000 2600",
])
async def test_range_smart_parsing(env, args):
    await range_cmd.handle(make_ctx(f"/range {args}"))
    assert sent_texts(env.rest) == [
        f"{at_tag()} 好嘞，本群难度区间已设为 2000–2600，下一道题就按这个范围选～"
    ]
    assert await users.get_group_rating(env.db, G) == (2000, 2600)


@pytest.mark.parametrize("args", [
    "2600-2000", "2000-2000", "2000", "2000-2600-3000", "abc", "0-5000",
])
async def test_range_invalid_shows_usage(env, args):
    await range_cmd.handle(make_ctx(f"/range {args}"))
    texts = sent_texts(env.rest)
    assert texts == [
        f"{at_tag()} 用法：/range 2000-2600（两个数，小到大；分隔符随意，"
        "比如 2000~2600、2000 到 2600）"
    ]
    assert await users.get_group_rating(env.db, G) == (None, None)


async def test_range_view_shows_override(env):
    await users.set_group_rating(env.db, G, 2100, 2700)
    await range_cmd.handle(make_ctx("/range"))
    assert sent_texts(env.rest) == [
        f"{at_tag()} 当前难度区间：2100–2700。用 /range 2000-2600 可以修改本群区间～"
    ]


async def test_range_view_defaults_to_config(env):
    await range_cmd.handle(make_ctx("/range"))
    assert "当前难度区间：2000–3000" in sent_texts(env.rest)[0]


# ── /status ─────────────────────────────────────────────────────────────


async def test_status_idle(env):
    await stubs.handle_status(make_ctx("/status"))
    assert sent_texts(env.rest) == ["🟢 bot 当前空闲，没有在处理请求～"]


async def test_status_reports_active_submit(env, monkeypatch):
    await seed_problem(env.db)
    judge_started = asyncio.Event()
    judge_release = asyncio.Event()

    async def _judge(*args, **kwargs):
        judge_started.set()
        await judge_release.wait()
        return ChatCompletionResult(text=json.dumps(verdict(False, reason="x")))

    monkeypatch.setattr("kouhai_bot.handlers.cmd.submit.judge_submission_result", _judge)
    task = asyncio.create_task(submit_cmd.handle(make_ctx("/submit 做法")))
    await asyncio.wait_for(judge_started.wait(), 5)
    await stubs.handle_status(make_ctx("/status", user=U2))
    assert sent_texts(env.rest)[-1] == "🔴 bot 当前有 submit 请求还在处理中～"
    judge_release.set()
    await asyncio.wait_for(task, 5)
    await stubs.handle_status(make_ctx("/status", user=U2))
    assert sent_texts(env.rest)[-1] == "🟢 bot 当前空闲，没有在处理请求～"


# ── /tag ────────────────────────────────────────────────────────────────


async def test_tag_lists_tags(env):
    await seed_problem(env.db, tags=["dp", "greedy"])
    await stubs.handle_tag(make_ctx("/tag"))
    assert sent_texts(env.rest) == [f"{at_tag()} 当前题目的 tags：dp、greedy"]


async def test_tag_no_problem(env):
    await stubs.handle_tag(make_ctx("/tag"))
    assert sent_texts(env.rest) == [f"{at_tag()} 还没有今日题目～"]


async def test_tag_no_tags(env):
    await seed_problem(env.db, tags=[])
    await stubs.handle_tag(make_ctx("/tag"))
    assert sent_texts(env.rest) == [f"{at_tag()} 当前题目没有 tag 信息～"]


# ── /scoreboard ─────────────────────────────────────────────────────────


async def test_scoreboard_empty(env):
    await stubs.handle_scoreboard(make_ctx("/scoreboard"))
    assert sent_texts(env.rest) == [f"{at_tag()} 还没有人解出过题目呢，来做第一人吧！🚀"]


async def test_scoreboard_weighted_no_top5(env):
    await seed_problem(env.db, pid="100A", rating=2300)  # 2 points
    await seed_problem(env.db, pid=PID, rating=2600)     # 4 points
    await posts.record_solve(env.db, G, U, "100A", "AliceSnap")
    await posts.record_solve(env.db, G, U2, PID, "BobSnap")
    await users.set_user_nickname(env.db, U, "AliceLive")
    await stubs.handle_scoreboard(make_ctx("/scoreboard"))
    texts = sent_texts(env.rest)
    assert len(texts) == 1  # render failed → single text fallback
    board = texts[0]
    assert "📊 累计解题排行榜（共 2 人）" in board
    assert "计分公式：2000=1 分，每 +300 分翻倍（2^((rating-2000)/300)）" in board
    assert "#1 BobSnap — 1 题 / 4 分" in board
    assert "#2 AliceLive — 1 题 / 2 分" in board  # live nickname preferred
    assert "Top" not in board and "top5" not in board


async def test_scoreboard_equal_scores_share_rank(env):
    await seed_problem(env.db, pid="100A", rating=2300)
    await seed_problem(env.db, pid=PID, rating=2300)
    await posts.record_solve(env.db, G, U, "100A", "Alice")
    await posts.record_solve(env.db, G, U2, PID, "Bob")
    await stubs.handle_scoreboard(make_ctx("/scoreboard"))
    board = sent_texts(env.rest)[0]
    assert "#1 Alice — 1 题 / 2 分" in board
    assert "#1 Bob — 1 题 / 2 分" in board
    assert "#2" not in board


# ── /problem ────────────────────────────────────────────────────────────


async def test_problem_resends_cached_card_as_text_fallback(env):
    await seed_problem(env.db, card={
        "post_msg": "题意简述正文",
        "sample_messages": ["样例一", "样例二"],
        "notes_message": "样例解释",
        "snake_enabled": False,
    })
    await stubs.handle_problem(make_ctx("/problem"))
    texts = sent_texts(env.rest)
    assert texts == ["题意简述正文\n\n样例一\n\n样例二\n\n样例解释"]


async def test_problem_solved_hint(env):
    await seed_problem(env.db)
    await posts.record_solve(env.db, G, U2, PID, "Bob")
    await stubs.handle_problem(make_ctx("/problem"))
    texts = sent_texts(env.rest)
    assert texts[-1] == "这题已经通过啦，可以发 /newproblem 挑战下一题～"


async def test_problem_high_rating_caution(env):
    await seed_problem(env.db, rating=2900)
    await stubs.handle_problem(make_ctx("/problem"))
    texts = sent_texts(env.rest)
    assert any("这道题的难度较高" in t for t in texts)


async def test_problem_no_current(env):
    await stubs.handle_problem(make_ctx("/problem"))
    assert sent_texts(env.rest) == [
        f"{at_tag()} 暂时不能查看当前题目，可能是暂时还不存在当前题目，试试 /newproblem？"
    ]


async def test_problem_missing_card_content(env):
    await seed_problem(env.db, card={})
    await stubs.handle_problem(make_ctx("/problem"))
    assert sent_texts(env.rest) == [
        f"{at_tag()} 暂时无法重新发送题目，试试 /tag 查看标签吧～"
    ]


# ── /help ───────────────────────────────────────────────────────────────


async def test_help_generated_from_registry(env):
    from kouhai_bot.handlers import registry

    registry.discover_commands()
    await help_cmd.handle(make_ctx("/help"))
    texts = sent_texts(env.rest)
    assert len(texts) == 1
    help_text = texts[0]
    assert help_text.startswith("可用指令：")
    assert "/submit(/sbm) 你的做法 — 提交你的做法，AI 判题" in help_text
    assert "/clarify(/clrf)" in help_text
    assert "/feedback(/fb)" in help_text
    assert "/range [最小-最大]" in help_text
    assert "5分钟冷却" in help_text  # newproblem cooldown from config (300s)
    assert "/sync" in help_text


# ── process_event dispatch ──────────────────────────────────────────────


def group_event(content: str, *, user: str = U, group: str = G, msg_id: str = "EV1") -> dict:
    return {
        "type": "message",
        "message_type": "group",
        "group_openid": group,
        "user_openid": user,
        "message_id": msg_id,
        "content": content,
        "author": {"member_openid": user},
    }


async def test_dispatch_alias_rewrites_to_canonical(env):
    await process_event(group_event("/sbm 做法"), spawn_handlers=False)
    assert sent_texts(env.rest) == [
        f"{at_tag()} 当前还没有题目可以提交～发 /newproblem 刷新一道？"
    ]


async def test_dispatch_pb_alias_resends_problem(env):
    await seed_problem(env.db)
    await process_event(group_event("/pb", msg_id="EV2"), spawn_handlers=False)
    assert "题意简述" in sent_texts(env.rest)[0]


async def test_dispatch_cooldown_blocks_second_call(env):
    await process_event(group_event("/range 2000-2600", msg_id="EV3"), spawn_handlers=False)
    assert await users.get_group_rating(env.db, G) == (2000, 2600)
    await process_event(group_event("/range 2100-2700", msg_id="EV4"), spawn_handlers=False)
    # /range has a 10s cooldown → second call ignored, state unchanged
    assert await users.get_group_rating(env.db, G) == (2000, 2600)


async def test_dispatch_strips_leading_zero_width_junk(env):
    await process_event(
        group_event("\u200b/range 2000-2600", msg_id="EV5"), spawn_handlers=False
    )
    assert await users.get_group_rating(env.db, G) == (2000, 2600)


async def test_dispatch_ignores_non_group_and_unknown(env):
    assert await process_event(
        {"type": "message", "message_type": "private", "content": "/help",
         "group_openid": G, "user_openid": U, "message_id": "EV6"},
        spawn_handlers=False,
    ) is None
    await process_event(group_event("/notacommand", msg_id="EV7"), spawn_handlers=False)
    await process_event(group_event("普通聊天消息", msg_id="EV8"), spawn_handlers=False)
    assert sent_texts(env.rest) == []


async def test_dispatch_spawns_handler_task(env):
    task = await process_event(group_event("/tag", msg_id="EV9"), spawn_handlers=True)
    assert task is not None
    await task
    assert sent_texts(env.rest) == [f"{at_tag()} 还没有今日题目～"]