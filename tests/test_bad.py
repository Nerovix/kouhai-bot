"""Tests for /bad — record dissatisfaction with the AI's latest delivered reply.

Targeting goes through the per-user last-interaction cache (survives problem
switches and /clear), falling back to a full-history scan for replies older
than the cache. Reports snapshot the target record verbatim into
bad_reports.json (group: groups/<gid>/, private: private_judge/bad_reports/)
and never touch the frozen user_submissions stores.
"""

import sys, os, json, asyncio
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.handlers.cmd import bad as bad_cmd
from kouhai_bot.handlers.cmd.submit import PendingRequest, _get_coordinator
from kouhai_bot.handlers.shared import (
    append_bad_report,
    load_bad_reports,
    load_group_last_interaction,
    remember_group_last_interaction,
)
from kouhai_bot.private_judge import (
    append_private_bad_report,
    load_private_bad_reports,
    load_private_last_interaction,
    remember_private_last_interaction,
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


def _seed_group_cache(tmp_path, record, user_id=UID):
    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        remember_group_last_interaction(GID, user_id, record)


def _seed_private_cache(tmp_path, record):
    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        remember_private_last_interaction(UID, record)


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


def _group_cache_path(tmp_path):
    return tmp_path / "groups" / str(GID) / "last_interaction.json"


def _private_reports_path(tmp_path):
    return tmp_path / "private_judge" / "bad_reports" / f"{UID}.json"


def _private_cache_path(tmp_path):
    return tmp_path / "private_judge" / "last_interaction" / f"{UID}.json"


# ═══════════════════════════════════════════════════════════════════════
# bad_reports storage
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
        from kouhai_bot.private_judge import save_private_submission
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
        for fn in (load_bad_reports, append_bad_report):
            try:
                fn(GID) if fn is load_bad_reports else fn(GID, {"scope": "group", "note": ""})
                raised = False
            except ValueError:
                raised = True
            assert raised
    assert corrupt.read_text(encoding="utf-8") == "{not json"


def test_atomic_write_leaves_no_tmp_files(tmp_path):
    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        append_bad_report(GID, {"scope": "group", "note": ""})
        append_private_bad_report(UID, {"scope": "private", "note": ""})
    leftovers = list(tmp_path.rglob("*.tmp*"))
    assert not leftovers, leftovers


# ═══════════════════════════════════════════════════════════════════════
# last-interaction cache helpers
# ═══════════════════════════════════════════════════════════════════════

def test_group_last_interaction_is_per_user(tmp_path):
    record_a = _record("2026-09-11T20:00:03+08:00", type_="clarify", result="clarify", reply="A")
    record_b = _record("2026-09-11T20:01:03+08:00", type_="submit", result="correct", reply="B")
    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        remember_group_last_interaction(GID, 1, record_a)
        remember_group_last_interaction(GID, 2, record_b)
        entry_1 = load_group_last_interaction(GID, 1)
        entry_2 = load_group_last_interaction(GID, 2)
    assert entry_1["record"]["reply"] == "A"
    assert entry_2["record"]["reply"] == "B"


def test_corrupt_last_interaction_cache_reads_as_missing(tmp_path):
    _group_cache_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _group_cache_path(tmp_path).write_text("{not json", encoding="utf-8")
    _private_cache_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _private_cache_path(tmp_path).write_text("[1,2]", encoding="utf-8")
    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        assert load_group_last_interaction(GID, UID) is None
        assert load_private_last_interaction(UID) is None


# ═══════════════════════════════════════════════════════════════════════
# _save_context_record hook
# ═══════════════════════════════════════════════════════════════════════

def _pending_req(*, scope="group", seq=1, command="clarify"):
    return PendingRequest(
        kind=command, group_id=GID, user_id=UID,
        sender={"nickname": "Alice"}, message_id="msg_001", command=command,
        nickname="Alice", scope=scope, payload="问题", target_pid=PID, seq=seq,
    )


def test_save_context_record_updates_cache_for_replied_results(tmp_path):
    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)

        async def _scenario():
            coord = _get_coordinator(GID)
            replied = _record("2026-09-11T20:00:03+08:00", type_="clarify",
                              result="clarify", reply="J(x) 是…", request_id="local-1")
            await coord._save_context_record(_pending_req(), replied)
            pending = _record("2026-09-11T20:00:04+08:00", type_="submit",
                              result="pending", request_id="local-2")
            await coord._save_context_record(_pending_req(seq=2, command="submit"), pending)
            private_rec = _record("2026-09-11T20:00:05+08:00", type_="review",
                                  result="review", reply="复盘…", request_id="local-3")
            await coord._save_context_record(_pending_req(scope="private"), private_rec)

        asyncio.run(_scenario())
        group_cached = load_group_last_interaction(GID, UID)
        private_cached = load_private_last_interaction(UID)
    assert group_cached["record"]["request_id"] == "local-1"  # pending write did not replace it
    assert private_cached["record"]["request_id"] == "local-3"


def test_save_context_record_cache_respects_clear_watermark(tmp_path):
    """A request finalized after /clear must not resurrect a cache entry."""
    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)

        async def _scenario():
            coord = _get_coordinator(GID)
            coord.clear_watermarks[("group", GID, UID, PID)] = 5
            replied = _record("2026-09-11T20:00:03+08:00", type_="clarify",
                              result="clarify", reply="…", request_id="local-1")
            return await coord._save_context_record(_pending_req(seq=1), replied)

        ok = asyncio.run(_scenario())
        cached = load_group_last_interaction(GID, UID)
    assert ok is False
    assert cached is None


# ═══════════════════════════════════════════════════════════════════════
# Handler — group scope
# ═══════════════════════════════════════════════════════════════════════

def test_bad_group_marks_cached_record_verbatim(tmp_path):
    _reset_captures()
    _write_group_state(tmp_path)
    record = _record(
        "2026-09-11T20:00:04+08:00", type_="submit", result="incorrect",
        reply="", reason="复杂度分析说 O(2^n)", request_id="local-4",
        model_tag="deepseek-v4-pro", content="我 submission 的内容",
    )
    _seed_group_cache(tmp_path, record)
    _write_scoreboard(tmp_path, [
        _record("2026-09-11T20:00:01+08:00", type_="submit", result="pending", request_id="local-1"),
    ])
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
    assert report["target"]["record"] == record  # verbatim snapshot
    assert (tmp_path / "groups" / str(GID) / "scoreboard.json").read_text(encoding="utf-8") == scoreboard_before


def test_bad_group_crosses_problem_switch(tmp_path):
    """刷新题后仍可标注上一题的回复。"""
    _reset_captures()
    _write_group_state(tmp_path, pid=OTHER_PID)  # problem already refreshed
    record = _record("2026-09-11T20:00:03+08:00", type_="clarify", result="clarify",
                     reply="旧题的回复", pid=PID, request_id="local-1")
    _seed_group_cache(tmp_path, record)

    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        asyncio.run(bad_cmd.handle(**_kwargs(_group_event("/bad 刷题前的回复"))))

    data = json.loads(_group_reports_path(tmp_path).read_text(encoding="utf-8"))
    assert data["reports"][0]["target"]["problem"] == PID
    assert data["reports"][0]["target"]["record"]["reply"] == "旧题的回复"


def test_bad_group_survives_clear(tmp_path):
    """/clear 只清 history，不清 last-interaction 缓存。"""
    _reset_captures()
    _write_group_state(tmp_path)
    record = _record("2026-09-11T20:00:03+08:00", type_="clarify", result="clarify",
                     reply="clear 前的回复", request_id="local-1")
    _seed_group_cache(tmp_path, record)
    # /clear wiped the history: scoreboard has nothing for this user anymore.
    _write_scoreboard(tmp_path, [])

    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        asyncio.run(bad_cmd.handle(**_kwargs(_group_event("/bad"))))

    data = json.loads(_group_reports_path(tmp_path).read_text(encoding="utf-8"))
    assert data["reports"][0]["target"]["record"]["reply"] == "clear 前的回复"


def test_bad_group_marks_delivered_failure_notice(tmp_path):
    """A timeout that delivered '模型服务出故障了' is a markable latest reply."""
    _reset_captures()
    _write_group_state(tmp_path)
    _seed_group_cache(tmp_path, _record(
        "2026-09-11T20:00:02+08:00", type_="submit", result="timeout", request_id="local-2",
    ))

    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        asyncio.run(bad_cmd.handle(**_kwargs(_group_event("/bad 超时了"))))

    assert _reacted == [("msg_001", "128076")], _reacted
    data = json.loads(_group_reports_path(tmp_path).read_text(encoding="utf-8"))
    assert data["reports"][0]["note"] == "超时了"
    assert data["reports"][0]["target"]["record"]["result"] == "timeout"


def test_bad_group_no_target_without_cache(tmp_path):
    """Cache-only targeting: stored history alone (e.g. replies that predate
    the cache) is deliberately NOT consulted — no cache entry, nothing to mark."""
    _reset_captures()
    _write_group_state(tmp_path)
    _write_scoreboard(tmp_path, [
        _record("2026-09-11T20:00:01+08:00", type_="submit", result="pending", request_id="local-1"),
        _record("2026-09-11T20:00:02+08:00", type_="submit", result="superseded", request_id="local-2"),
        _record("2026-09-11T20:00:03+08:00", type_="clarify", result="clarify",
                reply="历史里的回复", request_id="local-3"),
    ])

    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        asyncio.run(bad_cmd.handle(**_kwargs(_group_event("/bad"))))

    assert "还没有可以标注的 AI 回复" in _last_group_text(), _sent
    assert not _group_reports_path(tmp_path).exists()
    assert not _reacted


def test_bad_group_repeat_appends_new_report(tmp_path):
    _reset_captures()
    _seed_group_cache(tmp_path, _record(
        "2026-09-11T20:00:03+08:00", type_="clarify", result="clarify", reply="…",
    ))
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
    _seed_group_cache(tmp_path, _record(
        "2026-09-11T20:00:03+08:00", type_="clarify", result="clarify", reply="…",
    ))

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


def test_bad_group_corrupt_store_shows_failure(tmp_path):
    _reset_captures()
    _seed_group_cache(tmp_path, _record(
        "2026-09-11T20:00:03+08:00", type_="clarify", result="clarify", reply="…",
    ))
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

def test_bad_private_marks_cached_record(tmp_path):
    _reset_captures()
    _seed_private_cache(tmp_path, _record(
        "2026-09-11T20:00:03+08:00", type_="clarify", result="clarify",
        reply="J(x) 是特殊因子和…", request_id="local-3", model_tag="deepseek-v4-flash",
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
    assert report["target"]["record"]["request_id"] == "local-3"


def test_bad_private_no_target(tmp_path):
    _reset_captures()
    with ExitStack() as stack:
        for p in _patches(tmp_path):
            stack.enter_context(p)
        asyncio.run(bad_cmd.handle(**_kwargs(_private_event("/bad"))))
    assert "还没有可以标注的 AI 回复" in _last_private_text(), _private_sent
    assert not _private_reports_path(tmp_path).exists()


# ═══════════════════════════════════════════════════════════════════════
# Note parsing
# ═══════════════════════════════════════════════════════════════════════

def test_bad_note_parsing_and_truncation(tmp_path):
    _reset_captures()
    long_note = "长" * 600
    _seed_group_cache(tmp_path, _record(
        "2026-09-11T20:00:03+08:00", type_="clarify", result="clarify", reply="…",
    ))
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
