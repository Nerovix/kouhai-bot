"""/feedback on the official-API + SQLite architecture.

Targeting reads the per-user last_interaction cache ONLY (written by the
coordinator whenever a replied record is persisted): it crosses problem
switches, survives /clear, and never falls back to scanning stored history.
Reports are append-only rows in bad_reports with a verbatim record snapshot.
The 👌 ack is a tiny emoji text message (no official reaction API).

Private-judge feedback coverage is dropped (private judge removed).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from kouhai_bot.config import BotConfig
from kouhai_bot.db import set_database
from kouhai_bot.db.repos import feedback, posts, problems, submissions
from kouhai_bot.handlers import process_event
from kouhai_bot.handlers.cmd import clear as clear_cmd
from kouhai_bot.handlers.cmd import feedback as feedback_cmd
from kouhai_bot.handlers.cmd import submit as submit_cmd
from kouhai_bot.handlers.context import Ctx
from kouhai_bot.handlers.runtime import Runtime, set_runtime
from kouhai_bot.llm import ChatCompletionResult
from kouhai_bot.official import OfficialSender, ReplyBudget
from kouhai_bot.problem_content import statement_fingerprint

G = "GROUP1"
U = "USER1"
PID = "542D"
PID2 = "543E"
TAG = "🐳"

EMPTY_CACHE_MSG = (
    "这边还没有可以标注的 AI 回复哦～先 /submit、/clarify 或 /review 一次，"
    "收到回复后再来 /feedback 吧。"
)


# ── infrastructure ──────────────────────────────────────────────────────


class FakeRest:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    async def send_group_message(self, group_openid, payload):
        self.sent.append((group_openid, payload))
        return {"id": f"M{len(self.sent)}"}

    async def upload_group_media(self, group_openid, payload):
        raise AssertionError("feedback tests must not upload media")


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
    monkeypatch.setattr(
        "kouhai_bot.handlers.cmd.submit.schedule_post_solve_editorial_followup",
        lambda group, pid: None,
    )
    return SimpleNamespace(db=db, rest=rest, sender=sender, cfg=cfg)


_msg_counter = 0


def make_ctx(raw_text: str, *, user: str = U, group: str = G) -> Ctx:
    global _msg_counter
    _msg_counter += 1
    return Ctx(
        group_openid=group,
        user_openid=user,
        message_id=f"msg-{_msg_counter}",
        raw_text=raw_text,
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


def verdict(correct: bool, *, reason: str = "", reply: str = "", reaction: str = "") -> dict:
    return {"correct": correct, "reason": reason, "reply": reply, "reaction": reaction}


def mock_judge(monkeypatch, verdict_dict: dict, *, tag: str = TAG, failure_kind=None):
    async def _judge(problem_text, submission, history=None, problem_rating=None):
        return ChatCompletionResult(
            text=json.dumps(verdict_dict, ensure_ascii=False),
            model_tag=tag,
            failure_kind=failure_kind,
        )

    monkeypatch.setattr(
        "kouhai_bot.handlers.cmd.submit.judge_submission_result", _judge
    )


def mock_chat(monkeypatch, text: str, *, tag: str = TAG):
    async def _chat(messages, **kwargs):
        return ChatCompletionResult(text=text, model_tag=tag)

    monkeypatch.setattr(
        "kouhai_bot.handlers.cmd.submit.call_chat_completion_result", _chat
    )


async def seed_problem(db, pid: str = PID, *, group: str = G) -> None:
    stmt = {
        "name": "Test Problem",
        "description": "Given n, compute the answer.",
        "input": "One integer n.",
        "output": "The answer.",
        "samples": [{"input": "1\n", "output": "2\n"}],
        "time_limit": "2 seconds",
        "memory_limit": "256 megabytes",
    }
    await problems.upsert_problem(
        db, pid,
        contest_id=542, idx="D", name="Test Problem", rating=2300,
        tags=["dp"],
        statement_sha256=statement_fingerprint(stmt),
        statement_json=json.dumps(stmt, ensure_ascii=False),
    )
    await posts.post_problem(
        db, group, pid, posted_at=1700000000,
        card_json=json.dumps({"post_msg": "新题", "sample_messages": []}),
    )


async def wait_for(predicate, timeout: float = 5.0):
    async def _loop():
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_loop(), timeout)


async def do_incorrect_submit(env, monkeypatch, *, content: str = "我的做法", pid: str = PID):
    await seed_problem(env.db, pid)
    mock_judge(monkeypatch, verdict(False, reason="复杂度不对"))
    await submit_cmd.handle(make_ctx(f"/submit {content}"))


# ── targeting: empty cache / syntax ─────────────────────────────────────


async def test_feedback_empty_cache_friendly_message(env):
    ctx = make_ctx("/feedback 不太行")
    await feedback_cmd.handle(ctx)
    assert sent_texts(env.rest) == [f"{at_tag()} {EMPTY_CACHE_MSG}"]
    assert await feedback.load_bad_reports(env.db, G) == []


async def test_feedback_bad_syntax_shows_usage(env):
    await feedback_cmd.handle(make_ctx("/feedbackx 备注"))
    assert sent_texts(env.rest) == [f"{at_tag()} 用法：/feedback [简短备注]～"]
    assert await feedback.load_bad_reports(env.db, G) == []


async def test_feedback_without_note_is_allowed(env, monkeypatch):
    await do_incorrect_submit(env, monkeypatch)
    await feedback_cmd.handle(make_ctx("/feedback"))
    assert sent_texts(env.rest)[-1] == "👌"
    reports = await feedback.load_bad_reports(env.db, G)
    assert len(reports) == 1 and reports[0]["note"] == ""


async def test_feedback_cache_only_never_scans_history(env):
    # A replied record exists in storage but the cache was never written:
    # /feedback must NOT fall back to scanning history.
    await submissions.save_submission(env.db, {
        "group_openid": G, "user_openid": U, "pid": PID,
        "type": "submit", "content": "旧做法", "result": "incorrect",
        "reason": "旧判定", "reply": "", "request_id": "manual-1",
    })
    await feedback_cmd.handle(make_ctx("/feedback"))
    assert sent_texts(env.rest) == [f"{at_tag()} {EMPTY_CACHE_MSG}"]
    assert await feedback.load_bad_reports(env.db, G) == []


# ── report content ──────────────────────────────────────────────────────


async def test_feedback_after_submit_snapshots_verbatim(env, monkeypatch):
    await do_incorrect_submit(env, monkeypatch)
    cached = await feedback.get_last_interaction(env.db, G, U)
    assert cached["result"] == "incorrect"

    ctx = make_ctx("/feedback 复杂度分析说错了")
    await feedback_cmd.handle(ctx)
    assert sent_texts(env.rest)[-1] == "👌"

    reports = await feedback.load_bad_reports(env.db, G)
    assert len(reports) == 1
    report = reports[0]
    assert report["id"] == 1
    assert report["scope"] == "group"
    assert report["group_openid"] == G
    assert report["user_openid"] == U
    assert report["note"] == "复杂度分析说错了"
    assert report["cmd_message_id"] == ctx.message_id
    assert report["reported_at"]
    assert report["target"] == {"problem": PID, "record": cached}
    # verbatim snapshot keeps the dedicated model_tag + request_id fields
    assert report["target"]["record"]["model_tag"] == TAG
    assert report["target"]["record"]["request_id"] == cached["request_id"]
    assert report["target"]["record"]["reason"] == "复杂度不对"


async def test_feedback_repeat_appends_new_entry(env, monkeypatch):
    await do_incorrect_submit(env, monkeypatch)
    await feedback_cmd.handle(make_ctx("/feedback 第一次"))
    await feedback_cmd.handle(make_ctx("/feedback 第二次"))
    reports = await feedback.load_bad_reports(env.db, G)
    assert [r["id"] for r in reports] == [1, 2]
    assert [r["note"] for r in reports] == ["第一次", "第二次"]
    assert reports[0]["target"] == reports[1]["target"]


async def test_feedback_note_stripped_and_capped(env, monkeypatch):
    await do_incorrect_submit(env, monkeypatch)
    await feedback_cmd.handle(make_ctx("/feedback  " + "备" * 600 + "  "))
    report = (await feedback.load_bad_reports(env.db, G))[0]
    assert report["note"] == "备" * 500


async def test_feedback_correct_reply_markable(env, monkeypatch):
    await seed_problem(env.db)
    mock_judge(monkeypatch, verdict(True, reason="做法正确"))
    await submit_cmd.handle(make_ctx("/submit 贪心即可"))
    await feedback_cmd.handle(make_ctx("/feedback 判得太松"))
    report = (await feedback.load_bad_reports(env.db, G))[0]
    assert report["target"]["record"]["result"] == "correct"
    assert sent_texts(env.rest)[-1] == "👌"


async def test_feedback_failure_notice_markable(env, monkeypatch):
    await seed_problem(env.db)

    async def _judge(problem_text, submission, history=None, problem_rating=None):
        # failure_kind only applies when the provider returned no text
        return ChatCompletionResult(text="", failure_kind="timeout")

    monkeypatch.setattr("kouhai_bot.handlers.cmd.submit.judge_submission_result", _judge)
    await submit_cmd.handle(make_ctx("/submit 做法"))
    assert "模型服务出故障了" in sent_texts(env.rest)[-1]
    await feedback_cmd.handle(make_ctx("/feedback 一直超时"))
    report = (await feedback.load_bad_reports(env.db, G))[0]
    assert report["target"]["record"]["result"] == "timeout"


async def test_feedback_clarify_reply_markable(env, monkeypatch):
    await seed_problem(env.db)
    mock_chat(monkeypatch, json.dumps({"reply": "可以这样理解题意", "reaction": ""}))
    from kouhai_bot.handlers.cmd import clarify as clarify_cmd

    await clarify_cmd.handle(make_ctx("/clarify 这句话什么意思"))
    await feedback_cmd.handle(make_ctx("/feedback 没讲清楚"))
    report = (await feedback.load_bad_reports(env.db, G))[0]
    assert report["target"]["record"]["result"] == "clarify"
    assert report["target"]["record"]["reply"] == "可以这样理解题意"


# ── targeting across problem switches and /clear ────────────────────────


async def test_feedback_targets_latest_reply_across_problem_switch(env, monkeypatch):
    await do_incorrect_submit(env, monkeypatch, pid=PID)
    # a new problem is posted and answered; the cache must follow it
    await seed_problem(env.db, PID2)
    mock_chat(monkeypatch, json.dumps({"reply": "新题的解释", "reaction": ""}))
    from kouhai_bot.handlers.cmd import clarify as clarify_cmd

    await clarify_cmd.handle(make_ctx("/clarify 新题疑问"))
    await feedback_cmd.handle(make_ctx("/feedback"))
    report = (await feedback.load_bad_reports(env.db, G))[0]
    assert report["target"]["problem"] == PID2
    assert report["target"]["record"]["result"] == "clarify"


async def test_feedback_survives_clear(env, monkeypatch):
    await do_incorrect_submit(env, monkeypatch)
    await clear_cmd.handle(make_ctx("/clear"))
    assert await submissions.get_user_problem_submissions(env.db, G, U, PID) == []
    # the cache is deliberately untouched by /clear
    await feedback_cmd.handle(make_ctx("/feedback 清空了也要标"))
    assert sent_texts(env.rest)[-1] == "👌"
    report = (await feedback.load_bad_reports(env.db, G))[0]
    assert report["target"]["problem"] == PID
    assert report["target"]["record"]["result"] == "incorrect"


# ── pending / superseded / offtopic are unmarkable ──────────────────────


async def test_feedback_pending_submit_unmarkable(env, monkeypatch):
    await seed_problem(env.db)
    started = asyncio.Event()
    release = asyncio.Event()

    async def _judge(problem_text, submission, history=None, problem_rating=None):
        started.set()
        await release.wait()
        return ChatCompletionResult(text=json.dumps(verdict(False, reason="不对")))

    monkeypatch.setattr("kouhai_bot.handlers.cmd.submit.judge_submission_result", _judge)
    task = asyncio.create_task(submit_cmd.handle(make_ctx("/submit 慢慢想")))
    await asyncio.wait_for(started.wait(), 5)

    async def _pending_saved():
        while True:
            records = await submissions.get_user_problem_submissions(env.db, G, U, PID)
            if records:
                return records
            await asyncio.sleep(0.01)

    # pending record archived, nothing delivered yet → nothing markable
    records = await asyncio.wait_for(_pending_saved(), 5)
    assert records[0]["result"] == "pending"
    await feedback_cmd.handle(make_ctx("/feedback"))
    assert sent_texts(env.rest)[-1] == f"{at_tag()} {EMPTY_CACHE_MSG}"
    assert await feedback.load_bad_reports(env.db, G) == []

    release.set()
    await asyncio.wait_for(task, 5)
    # once the reply is delivered the same record becomes markable
    await feedback_cmd.handle(make_ctx("/feedback 现在可以了"))
    assert sent_texts(env.rest)[-1] == "👌"
    report = (await feedback.load_bad_reports(env.db, G))[0]
    assert report["target"]["record"]["result"] == "incorrect"


async def test_feedback_superseded_submit_unmarkable(env, monkeypatch):
    await seed_problem(env.db)
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    second_release = asyncio.Event()

    async def _judge(problem_text, submission, history=None, problem_rating=None):
        if submission == "第一版":
            first_started.set()
            await asyncio.Event().wait()  # cancelled by supersede
        second_started.set()
        await second_release.wait()
        return ChatCompletionResult(text=json.dumps(verdict(False, reason="第二版判定")))

    monkeypatch.setattr("kouhai_bot.handlers.cmd.submit.judge_submission_result", _judge)
    task1 = asyncio.create_task(submit_cmd.handle(make_ctx("/submit 第一版")))
    await asyncio.wait_for(first_started.wait(), 5)
    task2 = asyncio.create_task(submit_cmd.handle(make_ctx("/submit 第二版")))
    await asyncio.wait_for(second_started.wait(), 5)
    await asyncio.wait_for(task1, 5)

    records = await submissions.get_user_problem_submissions(env.db, G, U, PID)
    by_content = {r["content"]: r for r in records}
    assert by_content["第一版"]["result"] == "superseded"
    assert by_content["第二版"]["result"] == "pending"
    # neither record has delivered a reply → cache still empty
    await feedback_cmd.handle(make_ctx("/feedback"))
    assert sent_texts(env.rest)[-1] == f"{at_tag()} {EMPTY_CACHE_MSG}"

    second_release.set()
    await asyncio.wait_for(task2, 5)
    await feedback_cmd.handle(make_ctx("/feedback"))
    report = (await feedback.load_bad_reports(env.db, G))[0]
    assert report["target"]["record"]["content"] == "第二版"
    assert report["target"]["record"]["request_id"] == by_content["第二版"]["request_id"]


async def test_feedback_offtopic_leaves_nothing_markable(env, monkeypatch):
    await seed_problem(env.db)
    mock_judge(monkeypatch, verdict(False, reaction="123"))
    await submit_cmd.handle(make_ctx("/submit 无关内容"))
    assert sent_texts(env.rest)[-1] == "😵"
    assert await submissions.get_user_problem_submissions(env.db, G, U, PID) == []
    assert await feedback.get_last_interaction(env.db, G, U) is None
    await feedback_cmd.handle(make_ctx("/feedback"))
    assert sent_texts(env.rest)[-1] == f"{at_tag()} {EMPTY_CACHE_MSG}"


# ── corruption semantics ────────────────────────────────────────────────


async def test_feedback_corrupt_cache_reads_as_empty(env, monkeypatch):
    await do_incorrect_submit(env, monkeypatch)
    await env.db.execute(
        "UPDATE last_interaction SET record_json='{bad json' "
        "WHERE scope='group' AND group_openid=? AND user_openid=?",
        (G, U),
    )
    assert await feedback.get_last_interaction(env.db, G, U) is None
    await feedback_cmd.handle(make_ctx("/feedback"))
    assert sent_texts(env.rest)[-1] == f"{at_tag()} {EMPTY_CACHE_MSG}"


async def test_corrupt_report_store_raises_but_append_still_works(env):
    rid = await feedback.append_bad_report(
        env.db, group_openid=G, user_openid=U, note="好报告",
        cmd_message_id="m1", target_pid=PID, target_record={"result": "incorrect"},
    )
    assert rid == 1
    await env.db.execute(
        "UPDATE bad_reports SET target_record_json='{bad json' WHERE id=1"
    )
    with pytest.raises(json.JSONDecodeError):
        await feedback.load_bad_reports(env.db, G)
    # append-only writes never read the store, so a new report still lands
    rid2 = await feedback.append_bad_report(
        env.db, group_openid=G, user_openid=U, note="再来",
        cmd_message_id="m2", target_pid=PID, target_record={"result": "clarify"},
    )
    assert rid2 == 2


async def test_feedback_append_failure_sends_admin_message(env, monkeypatch):
    await do_incorrect_submit(env, monkeypatch)

    async def _boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(
        "kouhai_bot.db.repos.feedback.append_bad_report", _boom
    )
    await feedback_cmd.handle(make_ctx("/feedback"))
    assert sent_texts(env.rest)[-1] == f"{at_tag()} 记录反馈失败了，联系一下管理员帮帮忙吧～"


# ── dispatch alias ──────────────────────────────────────────────────────


async def test_fb_alias_dispatches_to_feedback(env, monkeypatch):
    from kouhai_bot.handlers import registry

    registry.discover_commands()
    await do_incorrect_submit(env, monkeypatch)
    await process_event(
        {
            "type": "message", "message_type": "group",
            "group_openid": G, "user_openid": U,
            "message_id": "EV-FB", "content": "/fb 别名反馈",
            "author": {"member_openid": U},
        },
        spawn_handlers=False,
    )
    reports = await feedback.load_bad_reports(env.db, G)
    assert len(reports) == 1
    assert reports[0]["note"] == "别名反馈"
    assert reports[0]["cmd_message_id"] == "EV-FB"