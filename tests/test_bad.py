"""Tests for /bad — record dissatisfaction with the AI's latest replied interaction.

The report must snapshot the target record verbatim into bad_reports.json
(group: groups/<gid>/, private: private_judge/bad_reports/<uid>.json), append
on repeat, and never touch the frozen user_submissions stores.
"""

import sys, os, json, asyncio
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.handlers.cmd import bad as bad_cmd
from kouhai_bot.handlers.shared import append_bad_report, load_bad_reports
from kouhai_bot.private_judge import (
    append_private_bad_report,
    load_private_bad_reports,
    save_private_submission,
    set_private_current_problem,
)

GID = 999999
UID = 42
PID = "542D"
OTHER_PID = "100A"

_sent: list[dict] = []
_reacted: list[tuple] = []
_private_sent: list[dict] = []


async def _mock_send_group(group_id, message):
    _sent.append({"group_id": group_id, "message": message})
    return True


async def _mock_react(message_id, emoji_id):
    _reacted.append((message_id, emoji_id))


async def _mock_send_private(user_id, message):
    _private_sent.append({"user_id": user_id, "message": message})
    return 1000 + len(_private_sent)


def _reset_captures():
    _sent.clear()
    _reacted.clear()
    _private_sent.clear()


def _patches(tmp_path):
    """Patch config paths + the send/react bindings bad.py resolves at import."""
    cfg = SimpleNamespace(data_dir=str(tmp_path))
    return [
        patch("kouhai_bot.handlers.shared.get_config", return_value=cfg),
        patch("kouhai_bot.private_judge.get_config", return_value=cfg),
        patch.object(bad_cmd, "send_group_msg", _mock_send_group),
        patch.object(bad_cmd, "send_private_msg", _mock_send_private),
        patch.object(bad_cmd, "react_emoji", _mock_react),
    ]


def _group_event(text="/bad", message_id="msg_001"):
    return {
        "type": "message",
        "message_type": "group",
        "group_id": GID,
        "user_id": UID,
        "sender": {"nickname": "Alice", "card": ""},
        "message_id": message_id,
        "raw_message": text,
        "message": [{"type": "text", "data": {"text": text}}],
    }


def _private_event(text="/bad", message_id="priv_001"):
    return {
        "type": "message",
        "message_type": "private",
        "group_id": GID,
        "user_id": UID,
        "sender": {"nickname": "Alice", "card": ""},
        "message_id": message_id,
        "raw_message": text,
        "message": [{"type": "text", "data": {"text": text}}],
    }


def _kwargs(event):
    return {
        "group_id": event["group_id"],
        "user_id": event["user_id"],
        "sender": event["sender"],
        "message_id": event["message_id"],
        "raw_text": event["raw_message"],
        "segments": event["message"],
        "event": event,
    }


def _last_group_text() -> str:
    if not _sent:
        return ""
    msg = _sent[-1]["message"]
    if isinstance(msg, list):
        return " ".join(
            seg.get("data", {}).get("text", "")
            for seg in msg if isinstance(seg, dict) and seg.get("type") == "text"
        )
    return str(msg)


def _last_private_text() -> str:
    if not _private_sent:
        return ""
    msg = _private_sent[-1]["message"]
    if isinstance(msg, list):
        return " ".join(
            seg.get("data", {}).get("text", "")
            for seg in msg if isinstance(seg, dict) and seg.get("type") == "text"
        )
    return str(msg)


def _record(ts, *, type_="submit", result="", reply="", reason="", pid=PID,
            request_id="", model_tag="", content="用户内容"):
    record = {
        "timestamp": ts,
        "type": type_,
        "content": content,
        "result": result,
        "reason": reason,
        "reply": reply,
        "problem": pid,
    }
    if request_id:
        record["request_id"] = request_id
    if model_tag:
        record["model_tag"] = model_tag
    return record


def _write_group_state(tmp_path, pid=PID):
    d = tmp_path / "groups" / str(GID)
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps({"today": pid}), encoding="utf-8")


def _write_scoreboard(tmp_path, records, user_id=UID):
    d = tmp_path / "groups" / str(GID)
    d.mkdir(parents=True, exist_ok=True)
    (d / "scoreboard.json").write_text(
        json.dumps({"solves": [], "user_submissions": {str(user_id): records}},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _group_reports_path(tmp_path):
    return tmp_path / "groups" / str(GID) / "bad_reports.json"


def _private_reports_path(tmp_path):
    return tmp_path / "private_judge" / "bad_reports" / f"{UID}.json"


# ═══════════════════════════════════════════════════════════════════════
# Storage helpers
# ═══════════════════════════════════════════════════════════════════════

def test_append_bad_report_assigns_incrementing_ids_and_shape(tmp_path):
    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        first = append_bad_report(GID, {"scope": "group", "note": ""})
        second = append_bad_report(GID, {"scope": "group", "note": "第二条"})
    assert (first, second) == (1, 2)
    data = json.loads(_group_reports_path(tmp_path).read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert [r["id"] for r in data["reports"]] == [1, 2]
    assert data["reports"][1]["note"] == "第二条"


def test_append_bad_report_does_not_touch_scoreboard(tmp_path):
    _write_scoreboard(tmp_path, [_record("2026-09-11T20:00:00+08:00", result="correct")])
    before = (tmp_path / "groups" / str(GID) / "scoreboard.json").read_text(encoding="utf-8")
    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        append_bad_report(GID, {"scope": "group", "note": ""})
    after = (tmp_path / "groups" / str(GID) / "scoreboard.json").read_text(encoding="utf-8")
    assert before == after


def test_append_private_bad_report_shape_and_state_untouched(tmp_path):
    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        save_private_submission(UID, {"request_id": "a", "result": "correct", "problem": PID})
        state_before = (tmp_path / "private_judge" / "users" / f"{UID}.json").read_text(encoding="utf-8")
        report_id = append_private_bad_report(UID, {"scope": "private", "note": "判错了"})
        loaded = load_private_bad_reports(UID)
    assert report_id == 1
    assert loaded["reports"][0]["note"] == "判错了"
    state_after = (tmp_path / "private_judge" / "users" / f"{UID}.json").read_text(encoding="utf-8")
    assert state_after == state_before


def test_load_bad_reports_missing_file_returns_empty(tmp_path):
    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        data = load_bad_reports(GID)
    assert data == {"version": 1, "reports": []}


def test_bad_reports_without_reports_key_are_repaired_by_append(tmp_path):
    """An operator-created {} file must not permanently brick /bad."""
    path = tmp_path / "groups" / str(GID) / "bad_reports.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1}), encoding="utf-8")
    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        report_id = append_bad_report(GID, {"scope": "group", "note": "修复形状"})
        data = load_bad_reports(GID)
    assert report_id == 1
    assert [r["note"] for r in data["reports"]] == ["修复形状"]


def test_corrupt_bad_reports_raise_and_are_not_overwritten(tmp_path):
    _write_group_state(tmp_path)
    corrupt = tmp_path / "groups" / str(GID) / "bad_reports.json"
    corrupt.write_text("{not json", encoding="utf-8")
    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        try:
            load_bad_reports(GID)
            raised = False
        except ValueError:
            raised = True
        assert raised
        try:
            append_bad_report(GID, {"scope": "group", "note": ""})
            append_raised = False
        except ValueError:
            append_raised = True
        assert append_raised
    assert corrupt.read_text(encoding="utf-8") == "{not json"


def test_atomic_write_leaves_no_tmp_files(tmp_path):
    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        append_bad_report(GID, {"scope": "group", "note": ""})
        append_private_bad_report(UID, {"scope": "private", "note": ""})
    leftovers = list(tmp_path.rglob("*.tmp*")) + [
        p for p in tmp_path.rglob(".*.tmp*")
    ]
    assert not leftovers, leftovers


# ═══════════════════════════════════════════════════════════════════════
# _received_reply / _record_kind
# ═══════════════════════════════════════════════════════════════════════

def test_received_reply_classifies_records():
    f = bad_cmd._received_reply
    # Legacy record without "type" still counts via the result fallback.
    assert f({"result": "correct", "reply": ""})
    assert f({"result": "incorrect", "reply": "", "reason": "复杂度错了"})
    assert f({"type": "clarify", "result": "clarify", "reply": "J(x) 是…"})
    assert f({"type": "review", "result": "review", "reply": "复盘…"})
    # Failure notices were delivered to the user, so they are markable.
    assert f({"type": "submit", "result": "timeout"})
    assert f({"type": "submit", "result": "service_unavailable"})
    assert f({"type": "submit", "result": "no_statement"})
    assert f({"type": "submit", "result": "image_unsupported"})
    assert f({"type": "clarify", "result": "service_unavailable", "reply": ""})
    # Never-delivered outcomes.
    assert not f({"type": "submit", "result": "pending"})
    assert not f({"type": "submit", "result": "superseded"})
    # Defensive: an LLM-answer result with an empty reply never reached the user.
    assert not f({"type": "clarify", "result": "clarify", "reply": ""})
    assert not f({"type": "review", "result": "review", "reply": "   "})
    # Unknown kinds never count.
    assert not f({"type": "clear", "result": "cleared"})
    assert not f({"result": "whatever"})
    assert not f({"result": "timeout"})  # failure result but not an interaction kind


# ═══════════════════════════════════════════════════════════════════════
# Handler — group scope
# ═══════════════════════════════════════════════════════════════════════

def test_bad_group_targets_latest_replied_record(tmp_path):
    _reset_captures()
    _write_group_state(tmp_path)
    records = [
        _record("2026-09-11T20:00:01+08:00", type_="submit", result="pending",
                request_id="local-1"),
        _record("2026-09-11T20:00:02+08:00", type_="submit", result="superseded",
                request_id="local-2"),
        _record("2026-09-11T20:00:03+08:00", type_="clarify", result="clarify",
                reply="J(x) 是特殊因子和…", request_id="local-3", model_tag="deepseek-v4-flash"),
        _record("2026-09-11T20:00:04+08:00", type_="submit", result="incorrect",
                reply="", reason="复杂度分析说 O(2^n)", request_id="local-4",
                model_tag="deepseek-v4-pro", content="我 submission 的内容"),
    ]
    _write_scoreboard(tmp_path, records)
    scoreboard_before = (tmp_path / "groups" / str(GID) / "scoreboard.json").read_text(encoding="utf-8")

    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        asyncio.run(bad_cmd.handle(**_kwargs(_group_event("/bad 判错了吧"))))

    assert _reacted == [("msg_001", "128076")], _reacted
    assert not _sent, _sent  # success ack in group is react-only
    data = json.loads(_group_reports_path(tmp_path).read_text(encoding="utf-8"))
    assert len(data["reports"]) == 1
    report = data["reports"][0]
    assert report["scope"] == "group"
    assert report["note"] == "判错了吧"
    assert report["cmd_message_id"] == "msg_001"
    assert report["target"]["problem"] == PID
    assert report["target"]["record_index"] == 3
    assert report["target"]["record"] == records[3]  # verbatim snapshot
    assert (tmp_path / "groups" / str(GID) / "scoreboard.json").read_text(encoding="utf-8") == scoreboard_before


def test_bad_group_repeat_appends_new_report(tmp_path):
    _reset_captures()
    _write_group_state(tmp_path)
    _write_scoreboard(tmp_path, [
        _record("2026-09-11T20:00:03+08:00", type_="clarify", result="clarify", reply="…"),
    ])
    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        asyncio.run(bad_cmd.handle(**_kwargs(_group_event("/bad"))))
        asyncio.run(bad_cmd.handle(**_kwargs(_group_event("/bad 补充：说错了", message_id="msg_002"))))
    data = json.loads(_group_reports_path(tmp_path).read_text(encoding="utf-8"))
    assert [r["id"] for r in data["reports"]] == [1, 2]
    assert data["reports"][0]["note"] == ""
    assert data["reports"][1]["note"] == "补充：说错了"
    assert data["reports"][1]["cmd_message_id"] == "msg_002"


def test_bad_group_concurrent_reports_get_sequential_ids(tmp_path):
    """Two in-flight /bad must not interleave the read-modify-write of bad_reports.json."""
    _reset_captures()
    _write_group_state(tmp_path)
    _write_scoreboard(tmp_path, [
        _record("2026-09-11T20:00:03+08:00", type_="clarify", result="clarify", reply="…"),
    ])

    async def _run_two():
        await asyncio.gather(
            bad_cmd.handle(**_kwargs(_group_event("/bad 第一条"))),
            bad_cmd.handle(**_kwargs(_group_event("/bad 第二条", message_id="msg_002"))),
        )

    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        asyncio.run(_run_two())

    data = json.loads(_group_reports_path(tmp_path).read_text(encoding="utf-8"))
    assert sorted(r["id"] for r in data["reports"]) == [1, 2]
    assert {r["note"] for r in data["reports"]} == {"第一条", "第二条"}
    assert len(_reacted) == 2


def test_bad_group_ignores_other_problem_records(tmp_path):
    _reset_captures()
    _write_group_state(tmp_path)
    records = [
        _record("2026-09-11T20:00:03+08:00", type_="clarify", result="clarify", reply="当前题回复"),
        _record("2026-09-11T20:30:00+08:00", type_="clarify", result="clarify",
                reply="别的题更新回复", pid=OTHER_PID),
    ]
    _write_scoreboard(tmp_path, records)
    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        asyncio.run(bad_cmd.handle(**_kwargs(_group_event("/bad"))))
    data = json.loads(_group_reports_path(tmp_path).read_text(encoding="utf-8"))
    assert data["reports"][0]["target"]["record"]["reply"] == "当前题回复"
    assert data["reports"][0]["target"]["record_index"] == 0


def test_bad_group_no_current_problem(tmp_path):
    _reset_captures()
    _write_scoreboard(tmp_path, [
        _record("2026-09-11T20:00:03+08:00", type_="clarify", result="clarify", reply="…"),
    ])
    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        asyncio.run(bad_cmd.handle(**_kwargs(_group_event("/bad"))))
    assert "还没有今日题目哦" in _last_group_text(), _sent
    assert not _group_reports_path(tmp_path).exists()
    assert not _reacted


def test_bad_group_no_history_for_problem(tmp_path):
    _reset_captures()
    _write_group_state(tmp_path, pid=OTHER_PID)
    _write_scoreboard(tmp_path, [
        _record("2026-09-11T20:00:03+08:00", type_="clarify", result="clarify", reply="…"),
    ])
    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        asyncio.run(bad_cmd.handle(**_kwargs(_group_event("/bad"))))
    assert "还没有和 AI 的交流记录" in _last_group_text(), _sent
    assert not _group_reports_path(tmp_path).exists()


def test_bad_group_no_replied_records(tmp_path):
    _reset_captures()
    _write_group_state(tmp_path)
    _write_scoreboard(tmp_path, [
        _record("2026-09-11T20:00:01+08:00", type_="submit", result="pending", request_id="local-1"),
        _record("2026-09-11T20:00:02+08:00", type_="submit", result="superseded",
                request_id="local-2"),
    ])
    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        asyncio.run(bad_cmd.handle(**_kwargs(_group_event("/bad"))))
    assert "还没有收到 AI 回复" in _last_group_text(), _sent
    assert not _group_reports_path(tmp_path).exists()


def test_bad_group_marks_delivered_failure_notice(tmp_path):
    """A timeout that delivered '模型服务出故障了' is a markable latest reply."""
    _reset_captures()
    _write_group_state(tmp_path)
    _write_scoreboard(tmp_path, [
        _record("2026-09-11T20:00:01+08:00", type_="clarify", result="clarify",
                reply="J(x) 是特殊因子和…", request_id="local-1"),
        _record("2026-09-11T20:00:02+08:00", type_="submit", result="timeout",
                request_id="local-2"),
    ])
    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        asyncio.run(bad_cmd.handle(**_kwargs(_group_event("/bad 超时了"))))

    assert _reacted == [("msg_001", "128076")], _reacted
    data = json.loads(_group_reports_path(tmp_path).read_text(encoding="utf-8"))
    assert data["reports"][0]["note"] == "超时了"
    assert data["reports"][0]["target"]["record_index"] == 1
    assert data["reports"][0]["target"]["record"]["result"] == "timeout"


def test_bad_group_corrupt_store_shows_failure(tmp_path):
    _reset_captures()
    _write_group_state(tmp_path)
    _write_scoreboard(tmp_path, [
        _record("2026-09-11T20:00:03+08:00", type_="clarify", result="clarify", reply="…"),
    ])
    _group_reports_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _group_reports_path(tmp_path).write_text("{not json", encoding="utf-8")
    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        asyncio.run(bad_cmd.handle(**_kwargs(_group_event("/bad"))))
    assert "记录反馈失败了" in _last_group_text(), _sent
    assert not _reacted
    assert _group_reports_path(tmp_path).read_text(encoding="utf-8") == "{not json"


# ═══════════════════════════════════════════════════════════════════════
# Handler — private scope
# ═══════════════════════════════════════════════════════════════════════

def _setup_private_problem(tmp_path):
    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        set_private_current_problem(UID, {
            "today": PID, "contestId": 542, "index": "D",
            "name": "Superhero's Job", "rating": 2600,
        })


def test_bad_private_skips_pending_tail(tmp_path):
    _reset_captures()
    _setup_private_problem(tmp_path)
    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        save_private_submission(UID, _record(
            "2026-09-11T20:00:03+08:00", type_="clarify", result="clarify",
            reply="J(x) 是特殊因子和…", request_id="local-3", model_tag="deepseek-v4-flash",
        ))
        save_private_submission(UID, _record(
            "2026-09-11T20:00:04+08:00", type_="submit", result="pending", request_id="local-4",
        ))
    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        asyncio.run(bad_cmd.handle(**_kwargs(_private_event("/bad 不对"))))

    assert "收到～" in _last_private_text(), _private_sent  # clear-style private reaction fallback
    data = json.loads(_private_reports_path(tmp_path).read_text(encoding="utf-8"))
    assert len(data["reports"]) == 1
    report = data["reports"][0]
    assert report["scope"] == "private"
    assert report["group_id"] == GID  # dispatcher rewrites private group_id to current_group
    assert report["note"] == "不对"
    assert report["target"]["record_index"] == 0
    assert report["target"]["record"]["request_id"] == "local-3"


def test_bad_private_no_current_problem(tmp_path):
    _reset_captures()
    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        asyncio.run(bad_cmd.handle(**_kwargs(_private_event("/bad"))))
    assert "/setproblem" in _last_private_text(), _private_sent
    assert not _private_reports_path(tmp_path).exists()


# ═══════════════════════════════════════════════════════════════════════
# Note parsing
# ═══════════════════════════════════════════════════════════════════════

def test_bad_note_parsing_and_truncation(tmp_path):
    _reset_captures()
    _write_group_state(tmp_path)
    long_note = "长" * 600
    _write_scoreboard(tmp_path, [
        _record("2026-09-11T20:00:03+08:00", type_="clarify", result="clarify", reply="…"),
    ])
    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        asyncio.run(bad_cmd.handle(**_kwargs(_group_event("/bad"))))
        asyncio.run(bad_cmd.handle(**_kwargs(_group_event("/bad   多余空白  ", message_id="msg_002"))))
        asyncio.run(bad_cmd.handle(**_kwargs(_group_event(f"/bad {long_note}", message_id="msg_003"))))
    data = json.loads(_group_reports_path(tmp_path).read_text(encoding="utf-8"))
    notes = [r["note"] for r in data["reports"]]
    assert notes[0] == ""
    assert notes[1] == "多余空白"
    assert notes[2] == "长" * 500


def test_bad_usage_error_both_scopes(tmp_path):
    _reset_captures()
    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        asyncio.run(bad_cmd.handle(**_kwargs(_group_event("/BAD 大写"))))
        asyncio.run(bad_cmd.handle(**_kwargs(_private_event("/bad!"))))
    assert "用法：/bad" in _last_group_text(), _sent
    assert not _group_reports_path(tmp_path).exists()
    assert "用法：/bad" in _last_private_text(), _private_sent
    assert not _private_reports_path(tmp_path).exists()
