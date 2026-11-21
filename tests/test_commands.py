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
from kouhai_bot.handlers import process_event, registry
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
        self.md_uploads: list[tuple[bytes, str]] = []
        self.md_deletes: list[str] = []
        self.acks: list[tuple[str, int]] = []

    async def send_group_message(self, group_openid, payload):
        self.sent.append((group_openid, payload))
        return {"id": f"M{len(self.sent)}"}

    async def upload_group_media(self, group_openid, payload):
        self.uploads.append((group_openid, payload))
        return {"file_info": "FI", "file_uuid": "u", "ttl": 300}

    async def upload_media_image(self, data, *, file_name="card.png", content_type="image/png"):
        self.md_uploads.append((data, file_name))
        return {"url": "qqbot-media://image/v1/TOKEN", "image_id": "IMG1", "ttl": 86400}

    async def delete_media_image(self, image_id):
        self.md_deletes.append(image_id)
        return True

    async def respond_interaction(self, interaction_id, *, code=0):
        self.acks.append((interaction_id, code))
        return True


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
        "kouhai_bot.handlers.cards.render_card_png", _no_render
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


def make_ctx(
    raw_text: str, *, user: str = U, group: str = G, nickname: str = "",
    event: dict | None = None,
) -> Ctx:
    global _msg_counter
    _msg_counter += 1
    return Ctx(
        group_openid=group,
        user_openid=user,
        message_id=f"msg-{_msg_counter}",
        raw_text=raw_text,
        nickname=nickname,
        event=event or {},
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


def sent_keyboards(rest: FakeRest) -> list[dict | None]:
    return [payload.get("keyboard") for _g, payload in rest.sent]


def button_labels(kb: dict | None) -> list[str]:
    if not kb:
        return []
    return [
        b["render_data"]["label"]
        for row in kb["content"]["rows"]
        for b in row["buttons"]
    ]


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
                "post_msg": f"{pid} 的题意简述",
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
    assert sent_texts(env.rest) == [f"{at_tag()} 用法：/sbm 你的做法。试试看？"]


async def test_submit_empty_body_reminder(env):
    await submit_cmd.handle(make_ctx("/submit   "))
    assert sent_texts(env.rest) == [
        f"{at_tag()} /sbm 后面要写你的做法呀，只发 /sbm 我不知道你做了啥 😅"
    ]


async def test_submit_no_current_problem(env):
    await submit_cmd.handle(make_ctx("/submit 我的做法"))
    assert sent_texts(env.rest) == [
        f"{at_tag()} 当前还没有题目可以提交～发 /np 刷新一道？"
    ]


# ── /submit: correct → first blood ──────────────────────────────────────


async def test_submit_correct_first_blood_scores_cheers_and_reveals(env, monkeypatch):
    await seed_problem(env.db, rating=2300, name="Nice Problem")
    calls = mock_judge(monkeypatch, verdict(True, reason="做法正确"))
    await submit_cmd.handle(make_ctx("/submit 贪心即可", nickname="Alice"))

    texts = sent_texts(env.rest)
    assert texts[0] == f"{at_tag()} 👀"
    cheer = texts[1]
    assert cheer.startswith(at_tag())
    assert "恭喜拿下本题一血！🎉 本题 +2 分（共 1 题，总分 2）" in cheer
    assert "当前第" not in cheer
    assert "本题来自 CF542D Nice Problem 2300✨" in cheer
    assert cheer.endswith(TAG)
    assert len(texts) == 2
    # congrats carries [跳转原题 → CF] + [再来一题 → /np auto-send] in one row
    kb = sent_keyboards(env.rest)[1]
    assert button_labels(kb) == ["跳转原题", "再来一题"]
    jump_btn, next_btn = kb["content"]["rows"][0]["buttons"]
    assert jump_btn["action"]["type"] == 0
    assert jump_btn["action"]["data"] == "https://codeforces.com/problemset/problem/542/D"
    assert next_btn["action"]["type"] == 1
    assert next_btn["action"]["data"] == "/np"
    assert "enter" not in next_btn["action"]

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
    assert texts == [f"{at_tag()} 已经有人解出本题了～想刷下一道可以发 /np 哦"]
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
    assert texts[0] == f"{at_tag()} 👀"
    # reply wins over reason; fake @ stripped; 😅 → ❤️; tag appended once
    assert texts[1] == f"{at_tag()} 复杂度超了哦❤️{TAG}"
    # rejection carries the [继续提交 / 清空上下文] action row
    kb = sent_keyboards(env.rest)[1]
    assert button_labels(kb) == ["继续提交", "清空上下文"]
    buttons = kb["content"]["rows"][0]["buttons"]
    assert [b["action"]["data"] for b in buttons] == ["/sbm", "/clear"]
    assert buttons[1]["action"]["type"] == 1  # clear is a no-arg callback
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
    assert button_labels(sent_keyboards(env.rest)[1]) == ["继续提交", "清空上下文"]


async def test_submit_incorrect_no_reason_default_text(env, monkeypatch):
    await seed_problem(env.db)
    mock_judge(monkeypatch, verdict(False))
    await submit_cmd.handle(make_ctx("/submit 做法"))
    assert sent_texts(env.rest)[1] == f"{at_tag()} 做法不太对呢。再想想？🤔{TAG}"
    assert button_labels(sent_keyboards(env.rest)[1]) == ["继续提交", "清空上下文"]


# ── /submit: offtopic leaves no trace ───────────────────────────────────


async def test_submit_offtopic_removes_pending_record(env, monkeypatch):
    await seed_problem(env.db)
    mock_judge(monkeypatch, verdict(False, reaction="123"))
    await submit_cmd.handle(make_ctx("/submit 讲个笑话"))
    texts = sent_texts(env.rest)
    assert texts == [f"{at_tag()} 👀", f"{at_tag()} 🙅"]  # tiny @-emoji acks, no verdict text
    records = await submissions.get_user_problem_submissions(env.db, G, U, PID)
    assert records == []  # pending archive removed by request_id
    assert await feedback.get_last_interaction(env.db, G, U) is None


async def test_clarify_offtopic_removes_pending_record(env, monkeypatch):
    await seed_problem(env.db)
    mock_chat(monkeypatch, json.dumps({"reply": "", "reaction": "123"}))
    await clarify_cmd.handle(make_ctx("/clarify 今天天气如何"))
    assert sent_texts(env.rest) == [f"{at_tag()} 👀", f"{at_tag()} 🙅"]
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
    assert texts[0] == f"{at_tag()} 👀"
    assert texts[1] == f"{at_tag()} 🧐"  # second-judge emoji reaction
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
    assert all("🧐" not in t for t in sent_texts(env.rest))
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
    assert texts[0] == f"{at_tag()} 👀"
    assert texts[1] == f"{at_tag()} n ≤ 1e5 哦～{TAG}"
    # clarify reply carries [提交 /sbm] + [继续询问 /clrf], both pre-fill
    kb = sent_keyboards(env.rest)[1]
    assert button_labels(kb) == ["提交", "继续询问"]
    buttons = kb["content"]["rows"][0]["buttons"]
    assert [b["action"]["data"] for b in buttons] == ["/sbm", "/clrf"]
    assert all(b["action"]["enter"] is False for b in buttons)
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
    assert sent_texts(env.rest) == [f"{at_tag()} 用法：/clrf 你的问题～"]


# ── /review ─────────────────────────────────────────────────────────────


async def test_review_targets_latest_solved_problem(env, monkeypatch):
    await seed_problem(env.db, pid="100A", name="Old Problem")
    await posts.record_solve(env.db, G, U, "100A", "Alice")
    await seed_problem(env.db, pid=PID, name="Current Unsolved")
    await posts.record_solve(env.db, G, U2, PID, "Bob")  # latest solve = PID
    calls = mock_chat(monkeypatch, "这道题的贪心是对的～")
    await review_cmd.handle(make_ctx("/review 我的做法对吗"))
    texts = sent_texts(env.rest)
    assert texts[0] == f"{at_tag()} 👀"
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
    await review_cmd.handle(make_ctx(
        f"/review 聊聊 <@{mentioned.lower()}>",
        event={"mentions": [
            {"member_openid": mentioned, "is_you": False, "bot": False, "scope": "single"},
        ]},
    ))
    user_msg = calls[0]["messages"][1]["content"]
    assert "被 @ 群友在此题的上下文" in user_msg
    assert mentioned in user_msg
    assert "被@群友的做法" in user_msg
    # mentioned user's history is read-only: only the pre-seeded record remains
    recs = await submissions.get_user_problem_submissions(env.db, G, mentioned, PID)
    assert len(recs) == 1


def test_review_mentions_skip_bot_and_self():
    ctx = make_ctx("/review x", user=U, event={"mentions": [
        {"member_openid": U, "scope": "single"},          # self → skip
        {"member_openid": "BOT1", "is_you": True},         # bot → skip
        {"member_openid": "BOT2", "bot": True},            # bot flag → skip
        {"member_openid": "FRIEND", "scope": "single"},    # kept
        {"member_openid": "FRIEND"},                       # duplicate → skip
    ]})
    assert review_cmd._mentioned_user_ids(ctx) == ["FRIEND"]


async def test_review_quoted_card_targets_older_solved_problem(env, monkeypatch):
    await seed_problem(env.db, PID, name="Latest Problem")
    await seed_problem(env.db, "111A", name="Older Problem")
    await posts.record_solve(env.db, G, U, "111A", "Alice")  # order 1
    await posts.record_solve(env.db, G, U, PID, "Alice")     # order 2 → latest
    await posts.record_message_ref(env.db, G, "REFIDX_old", "111A", "Mcard")
    calls = mock_chat(monkeypatch, "复盘旧题～")
    await review_cmd.handle(make_ctx("/review 聊聊", event={"ref_msg_idx": "REFIDX_old"}))
    user_msg = calls[0]["messages"][1]["content"]
    assert "Older Problem" in user_msg
    assert "Latest Problem" not in user_msg


async def test_review_quoted_unsolved_falls_back_to_latest(env, monkeypatch):
    await seed_problem(env.db, PID, name="Latest Problem")
    await seed_problem(env.db, "111A", name="Unsolved Problem")
    await posts.record_solve(env.db, G, U, PID, "Alice")  # only PID solved
    await posts.record_message_ref(env.db, G, "REFIDX_unsolved", "111A", "Mcard")
    calls = mock_chat(monkeypatch, "复盘～")
    await review_cmd.handle(make_ctx("/review 聊聊", event={"ref_msg_idx": "REFIDX_unsolved"}))
    user_msg = calls[0]["messages"][1]["content"]
    assert "Latest Problem" in user_msg
    assert "Unsolved Problem" not in user_msg


async def test_review_long_reply_uses_card_with_text_fallback(env, monkeypatch):
    await seed_problem(env.db)
    await posts.record_solve(env.db, G, U, PID, "Alice")
    long_reply = "复盘" * 150  # > 200 chars
    mock_chat(monkeypatch, long_reply)
    await review_cmd.handle(make_ctx("/review 详细复盘"))
    texts = sent_texts(env.rest)
    assert texts[0] == f"{at_tag()} 👀"
    assert long_reply + TAG in texts  # render failed → plain-text fallback
    assert texts[-1].strip() == at_tag()  # long image carries the reply; bare @ ping only


async def test_review_usage(env):
    await review_cmd.handle(make_ctx("/review"))
    assert sent_texts(env.rest) == [f"{at_tag()} 用法：/rv 你的问题～"]


# ── /clear ──────────────────────────────────────────────────────────────


async def test_clear_removes_current_problem_history_and_acks(env, monkeypatch):
    await seed_problem(env.db)
    mock_judge(monkeypatch, verdict(False, reason="不对"))
    await submit_cmd.handle(make_ctx("/submit 做法"))
    assert len(await submissions.get_user_problem_submissions(env.db, G, U, PID)) == 1
    env.rest.sent.clear()
    await clear_cmd.handle(make_ctx("/clear"))
    assert sent_texts(env.rest) == [f"{at_tag()} 👌"]
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


async def test_clear_with_args_shows_usage_not_false_ack(env):
    await clear_cmd.handle(make_ctx("/clear 全部"))
    assert sent_texts(env.rest) == [
        f"{at_tag()} 用法：/clear（清空自己在当前题目的上下文，后面不用加东西哦）"
    ]


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
    assert sent_texts(env.rest)[-1] == "🔴 目前正在处理的请求有：@USER1 的 submit～"
    judge_release.set()
    await asyncio.wait_for(task, 5)
    await stubs.handle_status(make_ctx("/status", user=U2))
    assert sent_texts(env.rest)[-1] == "🟢 bot 当前空闲，没有在处理请求～"


async def test_status_lists_multiple_active_requests(env, monkeypatch):
    await seed_problem(env.db)
    judge_started = asyncio.Event()
    release = asyncio.Event()

    async def _judge(*args, **kwargs):
        judge_started.set()
        await release.wait()
        return ChatCompletionResult(text=json.dumps(verdict(False, reason="x")))

    async def _chat(messages, **kwargs):
        await release.wait()
        return ChatCompletionResult(text=json.dumps({"reply": "ok", "reaction": ""}))

    monkeypatch.setattr("kouhai_bot.handlers.cmd.submit.judge_submission_result", _judge)
    monkeypatch.setattr("kouhai_bot.handlers.cmd.submit.call_chat_completion_result", _chat)
    t1 = asyncio.create_task(submit_cmd.handle(make_ctx("/submit 做法", nickname="Alice")))
    await asyncio.wait_for(judge_started.wait(), 5)
    t2 = asyncio.create_task(clarify_cmd.handle(make_ctx("/clarify 题意", user=U2, nickname="Bob")))
    await wait_for(lambda: len(submit_cmd.get_group_active_requests(G)) >= 2)
    await stubs.handle_status(make_ctx("/status", user=U2))
    assert sent_texts(env.rest)[-1] == "🔴 目前正在处理的请求有：@Alice 的 submit、@Bob 的 clarify～"
    release.set()
    await asyncio.wait_for(t1, 5)
    await asyncio.wait_for(t2, 5)
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
    assert "计分：2000 分 = 1 分，每 +300 分翻倍" in board
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


async def test_scoreboard_migration_note_only_for_service_group(env):
    await seed_problem(env.db, pid="100A", rating=2300)
    await posts.record_solve(env.db, G, U, "100A", "Alice")

    # default group: no migration note
    await stubs.handle_scoreboard(make_ctx("/scoreboard"))
    assert "数据正在迁移中" not in sent_texts(env.rest)[0]

    # configured legacy service group: gray note under the formula
    env.cfg.legacy_migration_enabled = True
    env.cfg.legacy_migration_group_openid = G
    env.rest.sent.clear()
    await stubs.handle_scoreboard(make_ctx("/scoreboard"))
    board = sent_texts(env.rest)[0]
    assert "计分：2000 分 = 1 分，每 +300 分翻倍" in board
    assert "数据正在迁移中，排名可能不完整。" in board
    # note sits directly under the formula, before the ranking rows
    assert board.index("数据正在迁移中") < board.index("#1 Alice")


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
    # render disabled → plain-text fallback (no thumbnail, no buttons)
    assert texts == ["题意简述正文\n\n样例一\n\n样例二\n\n样例解释"]
    assert env.rest.md_uploads == []


async def test_problem_solved_hint(env):
    await seed_problem(env.db)
    await posts.record_solve(env.db, G, U2, PID, "Bob")
    await stubs.handle_problem(make_ctx("/problem"))
    texts = sent_texts(env.rest)
    assert texts[-1] == "这题已经通过啦，可以发 /np 挑战下一题～"


async def test_problem_high_rating_caution(env):
    await seed_problem(env.db, rating=2900)
    await stubs.handle_problem(make_ctx("/problem"))
    texts = sent_texts(env.rest)
    assert any("这道题的难度较高" in t for t in texts)


async def test_problem_no_current(env):
    await stubs.handle_problem(make_ctx("/problem"))
    assert sent_texts(env.rest) == [
        f"{at_tag()} 暂时不能查看当前题目，可能是暂时还不存在当前题目，试试 /np？"
    ]


async def test_problem_missing_card_content(env):
    await seed_problem(env.db, card={})
    await stubs.handle_problem(make_ctx("/problem"))
    assert sent_texts(env.rest) == [
        f"{at_tag()} 暂时无法重新发送题目，试试 /tag 查看标签吧～"
    ]


async def test_problem_reuses_prerendered_cache(env, monkeypatch):
    sha = await seed_problem(env.db, card={
        "post_msg": "题意简述正文",
        "sample_messages": ["样例一"],
        "notes_message": "",
        "snake_enabled": False,
    })
    from kouhai_bot.handlers import cards

    fake_png = b"\x89PNG\r\n\x1a\nfake-card-bytes"

    async def _fake_render(*args, **kwargs):
        return fake_png

    monkeypatch.setattr(cards, "render_card_png", _fake_render)
    assert await cards.prerender_problem_card(
        pid=PID, statement_sha256=sha, summary="题意简述正文", model_tag="",
        sample_messages=["样例一"], notes_message="",
        bot_name=env.sender.bot_name, bot_avatar_url=env.sender.bot_avatar_url,
    )
    # env disables render_card_png (raises), so a cache hit must skip rendering
    # entirely and upload the cached PNG as a markdown thumbnail + buttons.
    await stubs.handle_problem(make_ctx("/problem"))
    assert env.rest.md_uploads
    assert env.rest.uploads == []
    card = env.rest.sent[-1][1]
    assert card["msg_type"] == 2
    # fake_png is < 24 bytes → no parseable IHDR → default 45×132 hint
    assert card["markdown"]["content"] == (
        "![题目 #45px #132px](qqbot-media://image/v1/TOKEN)"
    )
    assert button_labels(sent_keyboards(env.rest)[-1]) == ["提交", "询问题意"]
    assert env.rest.md_deletes == ["IMG1"]


async def test_problem_resend_ignores_statement_figures(env):
    statement = statement_payload("Test Problem")
    statement["images"] = [
        {"src": "https://x/g1.png", "kind": "graphic"},
        {"src": "https://x/f1.png", "kind": "formula"},
    ]
    await seed_problem(env.db, statement=statement, card={
        "post_msg": "题意简述正文",
        "sample_messages": ["样例一"],
        "snake_enabled": False,
    })
    await stubs.handle_problem(make_ctx("/problem"))
    # Statement figures feed the explanation LLM only; the card stays
    # summary + samples + generated pure-text explanation.
    assert sent_texts(env.rest) == ["题意简述正文\n\n样例一"]


# ── /help ───────────────────────────────────────────────────────────────


async def test_help_generated_from_registry(env):
    from kouhai_bot.handlers import registry

    registry.discover_commands()
    await help_cmd.handle(make_ctx("/help"))
    texts = sent_texts(env.rest)
    assert len(texts) == 1
    help_text = texts[0]
    # Themed sections, one bullet per command, short trigger + "short for" note.
    assert "📚 刷题" in help_text
    assert "✍️ 判题 · 讨论" in help_text
    assert "🔧 其他" in help_text
    assert "/sbm 你的做法 — 提交你的做法，AI 判题" in help_text
    assert "short for submit" in help_text
    assert "/clrf" in help_text
    assert "/fb" in help_text
    assert "/np" in help_text
    assert "/range [最小-最大]" in help_text
    assert "/np — 来一道新题" in help_text
    assert "冷却" not in help_text  # cooldown detail hidden from /help
    assert "--force" not in help_text  # force hint hidden; bot reminds when needed
    assert "/sync" in help_text
    # Alias-only interface: the old combined "/full(/alias)" form is gone.
    assert "/submit(/sbm)" not in help_text


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
        # Commands require an @bot; default to the chip/full-message form.
        "mentions": [{"member_openid": "BOT_PLACEHOLDER", "bot": True, "is_you": True}],
        "at_bot": False,
    }


async def test_dispatch_alias_rewrites_to_canonical(env):
    await process_event(group_event("/sbm 做法"), spawn_handlers=False)
    assert sent_texts(env.rest) == [
        f"{at_tag()} 当前还没有题目可以提交～发 /np 刷新一道？"
    ]


async def test_dispatch_rejects_full_name_of_aliased_command(env):
    # Alias-only interface: /submit is internal identity, not a usable trigger.
    await process_event(group_event("/submit 做法", msg_id="EVFULL"), spawn_handlers=False)
    assert sent_texts(env.rest) == []


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


def interaction_event(
    button_data: str, *, user: str = U, group: str = G, iid: str = "INT1",
    interaction_type: int = 11, scene: str = "group",
) -> dict:
    return {
        "type": "interaction",
        "group_openid": group,
        "user_openid": user,
        "interaction_id": iid,
        "interaction_type": interaction_type,
        "scene": scene,
        "button_data": button_data,
        "button_id": "btn",
        "data": {"type": interaction_type,
                 "resolved": {"button_data": button_data, "button_id": "btn"}},
        "raw": {},
    }


async def test_interaction_clear_callback_clears_without_ok_ack(env, monkeypatch):
    registry.discover_commands()
    await seed_problem(env.db)
    mock_judge(monkeypatch, verdict(False, reason="不对"))
    await submit_cmd.handle(make_ctx("/submit 做法"))
    assert len(await submissions.get_user_problem_submissions(env.db, G, U, PID)) == 1
    env.rest.sent.clear()
    # Clicking 清空上下文 (type=1 callback "/clear") clears history exactly like
    # typing /clear and acks the interaction (PUT code=0). Unlike typed /clear it
    # sends NO 👌 — QQ already shows the clicker an "操作成功" toast.
    await process_event(interaction_event("/clear", iid="INT_CLEAR"), spawn_handlers=False)
    assert sent_texts(env.rest) == []
    assert await submissions.get_user_problem_submissions(env.db, G, U, PID) == []
    assert env.rest.acks == [("INT_CLEAR", 0)]


async def test_unsolved_np_rejection_carries_view_problem_callback(env):
    registry.discover_commands()
    grp = "G_UNSOLVED"
    await seed_problem(env.db, group=grp)  # current problem, unsolved
    await process_event(group_event("/np", group=grp, msg_id="EVNP"), spawn_handlers=False)
    assert sent_texts(env.rest) == [
        f"{at_tag()} 当前题目还没有人解出来呢～不能直接刷题哦。\n"
        "可使用 /pb 查看当前题目。\n"
        "如果确定要换题，请发 /np --force"
    ]
    kb = sent_keyboards(env.rest)[0]
    assert button_labels(kb) == ["查看当前题目"]
    btn = kb["content"]["rows"][0]["buttons"][0]
    assert btn["action"]["type"] == 1          # callback, not auto-send command
    assert btn["action"]["data"] == "/pb"
    assert "enter" not in btn["action"]
    # Clicking 查看当前题目 (callback "/pb") resends the current problem card,
    # exactly like typing /pb.
    env.rest.sent.clear()
    await process_event(
        interaction_event("/pb", group=grp, iid="INT_PB"), spawn_handlers=False
    )
    assert any("题意简述" in t for t in sent_texts(env.rest))
    assert env.rest.acks == [("INT_PB", 0)]


# ── /np cooldown reminder: round up + friendly duration ─────────────────


def test_format_wait_friendly_duration():
    from kouhai_bot.handlers.cmd.newproblem import _format_wait

    assert _format_wait(1) == "1 秒"
    assert _format_wait(10) == "10 秒"
    assert _format_wait(59) == "59 秒"
    assert _format_wait(60) == "1 分钟"
    assert _format_wait(90) == "1 分 30 秒"
    assert _format_wait(299) == "4 分 59 秒"
    assert _format_wait(300) == "5 分钟"


async def test_np_cooldown_reminder_never_shows_zero_seconds(env, monkeypatch):
    import time

    from kouhai_bot.handlers.cmd import newproblem as np_cmd

    registry.discover_commands()
    grp = "G_NP_CD_ZERO"
    monkeypatch.setattr(env.cfg, "newproblem_cooldown", 10)
    # 0.3s left on a 10s cooldown — the old int() truncation rendered "等 0 秒".
    np_cmd._cooldowns[grp] = time.monotonic() - 9.7
    try:
        await process_event(
            group_event("/np", group=grp, msg_id="EVCD0"), spawn_handlers=False
        )
        texts = sent_texts(env.rest)
        assert texts == [f"{at_tag()} 刷新太频繁啦，等 1 秒再试哦～"]
        assert "0 秒" not in texts[0]
    finally:
        np_cmd._cooldowns.pop(grp, None)


async def test_np_cooldown_reminder_formats_minutes(env):
    import time

    from kouhai_bot.handlers.cmd import newproblem as np_cmd

    registry.discover_commands()
    grp = "G_NP_CD_MIN"
    # Just posted on the production 300s cooldown → "5 分钟", not "300 秒".
    np_cmd._cooldowns[grp] = time.monotonic() - 0.3
    try:
        await process_event(
            group_event("/np", group=grp, msg_id="EVCDM"), spawn_handlers=False
        )
        assert sent_texts(env.rest) == [
            f"{at_tag()} 刷新太频繁啦，等 5 分钟再试哦～"
        ]
    finally:
        np_cmd._cooldowns.pop(grp, None)


# ── /np --force skip notice: markdown + 跳转原题 button ─────────────────


async def test_forced_skip_notice_carries_jump_original_button(env, monkeypatch):
    from kouhai_bot.handlers.cmd import newproblem as np_cmd

    captured: list[tuple[str, str, dict | None]] = []

    class _Sender:
        async def send_group_markdown(self, group, markdown, *, keyboard=None, msg_id=None):
            captured.append((group, markdown, keyboard))
            return "M1"

    monkeypatch.setattr(
        np_cmd, "get_runtime", lambda: SimpleNamespace(sender=_Sender())
    )

    async def _no_editorial(pid):
        return None

    monkeypatch.setattr(np_cmd, "get_verified_official_editorial", _no_editorial)

    problem = {"today": "1856E1", "name": "Some Problem", "rating": "2500"}
    ok = await np_cmd._send_forced_skip_followup(G, problem)
    assert ok is True
    assert len(captured) == 1
    group, markdown, kb = captured[0]
    assert group == G
    assert markdown == "当前题目已被跳过，原题为 CF1856E1 Some Problem 2500。"
    assert button_labels(kb) == ["跳转原题"]
    btn = kb["content"]["rows"][0]["buttons"][0]
    assert btn["action"]["type"] == 0
    assert btn["action"]["data"] == "https://codeforces.com/problemset/problem/1856/E1"