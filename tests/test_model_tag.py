"""Unit tests for model-tag persistence and rendering.

Covers the two gaps fixed together:
- judge (submit) records persist `model_tag` as a dedicated field so the
  forwarded history card can render it on 🤖 lines;
- the /sync scored cheer appends the model tag of the synced private correct
  record, matching the in-group /submit scoreboard message.
"""

import sys, os, asyncio
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.annotations.exporter import _collect_rounds_for_problem, _history_item
from kouhai_bot.config import UserGroupConfig
from kouhai_bot.handlers.cmd import sync as sync_mod
from kouhai_bot.handlers.cmd.submit import PendingRequest, _context_record
from kouhai_bot.llm import append_model_tag
from kouhai_bot.private_judge import (
    copy_records,
    first_correct_submit_record,
    format_history_records,
    private_correct_record_model_tag,
    private_record_has_correct,
)

GID = 999999
UID = 42
PID = "542D"
TAG = "『DSv4pro』"


def _req(**overrides) -> PendingRequest:
    kwargs = dict(
        kind="submit",
        group_id=GID,
        user_id=UID,
        sender={"nickname": "Alice"},
        message_id="m-1",
        command="submit",
        nickname="Alice",
        payload="我的做法是……",
        target_pid=PID,
        seq=7,
    )
    kwargs.update(overrides)
    return PendingRequest(**kwargs)


# ═══════════════════════════════════════════════════════════════════════
# _context_record: judge records persist the model tag as a field
# ═══════════════════════════════════════════════════════════════════════

def test_context_record_stores_model_tag_when_present():
    record = _context_record(_req(), result="incorrect", reason="不对哦", reply="再想想～", model_tag=TAG)
    assert record["model_tag"] == TAG
    assert record["type"] == "submit"
    assert record["result"] == "incorrect"


def test_context_record_omits_model_tag_when_empty():
    record = _context_record(_req(), result="correct", reason="对！", reply="恭喜～")
    assert "model_tag" not in record


# ═══════════════════════════════════════════════════════════════════════
# format_history_records: forwarded history card renders the tag
# ═══════════════════════════════════════════════════════════════════════

def _judge_record(reply: str, model_tag: str = TAG) -> dict:
    return {
        "timestamp": "2026-09-09T12:00:00+08:00",
        "type": "submit",
        "content": "把(ai,bi)看成无序对……",
        "result": "incorrect",
        "reason": "还差一点",
        "reply": reply,
        "problem": PID,
        "model_tag": model_tag,
    }


def test_history_card_appends_model_tag_to_judge_reply():
    text = format_history_records([_judge_record("相邻区间什么时候归入同一段呀～")], user_display_name="张三")
    lines = text.splitlines()
    assert lines[0] == "张三在当前的历史记录如下："
    assert lines[1] == "👤：把(ai,bi)看成无序对……"
    assert lines[2] == "🤖：相邻区间什么时候归入同一段呀～" + TAG


def test_history_card_renders_tag_on_reply_only_record():
    record = _judge_record("再检查一下奇偶转移～")
    record.pop("content")
    text = format_history_records([record], user_display_name="张三")
    assert text.splitlines()[1] == "🤖：再检查一下奇偶转移～" + TAG


def test_history_card_without_model_tag_field_unchanged():
    record = _judge_record("嗯嗯思路对啦～")
    record.pop("model_tag")
    text = format_history_records([record], user_display_name="张三")
    assert text.splitlines()[2] == "🤖：嗯嗯思路对啦～"


def test_history_card_does_not_double_render_embedded_clarify_tag():
    # clarify/review records embed the tag in `reply` at save time and carry no
    # dedicated field — the card must render it exactly once.
    record = {
        "timestamp": "2026-09-09T12:05:00+08:00",
        "type": "clarify",
        "content": "n 的范围是多少？",
        "result": "clarify",
        "reply": "n ≤ 1e5 哦～" + TAG,
        "problem": PID,
    }
    text = format_history_records([record], user_display_name="张三")
    assert text.splitlines()[2] == "🤖：n ≤ 1e5 哦～" + TAG


def test_history_card_mixed_records_keep_order_and_tags():
    records = [
        _judge_record("第一步就错啦～", model_tag="『A』"),
        {
            "timestamp": "2026-09-09T12:05:00+08:00",
            "type": "clarify",
            "content": "时空限制？",
            "result": "clarify",
            "reply": "2s / 256MB～",
            "problem": PID,
        },
        _judge_record("这次对啦🎉", model_tag="『B』"),
    ]
    text = format_history_records(records, user_display_name="张三")
    lines = text.splitlines()
    assert lines[2] == "🤖：第一步就错啦～『A』"
    assert lines[4] == "🤖：2s / 256MB～"
    assert lines[6] == "🤖：这次对啦🎉『B』"


def test_history_card_skips_doubled_tag_when_reply_already_tagged():
    # An LLM that saw tagged history can echo the suffix in its own reply;
    # the card must render the tag exactly once.
    record = _judge_record("思路对啦" + TAG)
    text = format_history_records([record], user_display_name="张三")
    assert text.splitlines()[2] == "🤖：思路对啦" + TAG


def test_history_card_incorrect_empty_reply_falls_back_to_reason():
    # The live message for an empty-reply incorrect verdict sends
    # "{reason}。再想想？🤔" + tag (submit.py _finalize_submit); the card mirrors it.
    record = _judge_record("")
    record["reason"] = "会 TLE"
    text = format_history_records([record], user_display_name="张三")
    assert text.splitlines()[2] == "🤖：会 TLE。再想想？🤔" + TAG


def test_history_card_correct_empty_reply_renders_no_bot_line():
    record = _judge_record("")
    record["result"] = "correct"
    record["reason"] = "做法完全正确"
    text = format_history_records([record], user_display_name="张三")
    assert text.splitlines() == ["张三在当前的历史记录如下：", "👤：把(ai,bi)看成无序对……"]


def test_copy_records_preserves_model_tag_across_sync():
    copied = copy_records([_judge_record("回答～")])
    assert copied[0]["model_tag"] == TAG
    assert private_correct_record_model_tag(
        [{**copied[0], "result": "correct", "reply": "做法完全正确！"}], PID,
    ) == TAG


# ═══════════════════════════════════════════════════════════════════════
# append_model_tag: shared append helper
# ═══════════════════════════════════════════════════════════════════════

def test_append_model_tag_cases():
    assert append_model_tag("做法对啦", "") == "做法对啦"
    assert append_model_tag("做法对啦", TAG) == "做法对啦" + TAG
    assert append_model_tag("做法对啦" + TAG, TAG) == "做法对啦" + TAG
    assert append_model_tag("做法对啦 \n", TAG) == "做法对啦" + TAG


# ═══════════════════════════════════════════════════════════════════════
# first_correct_submit_record: shared scoring gate + tag lookup
# ═══════════════════════════════════════════════════════════════════════

def test_first_correct_submit_record_matches_has_correct():
    records = [
        _judge_record("不对哦"),
        {**_judge_record("这次对啦"), "result": "correct"},
    ]
    found = first_correct_submit_record(records, PID)
    assert found is not None and found["result"] == "correct" and found["model_tag"] == TAG
    assert first_correct_submit_record(records, "100A") is None
    assert private_record_has_correct(records, PID) is True


# ═══════════════════════════════════════════════════════════════════════
# annotation exporter keeps tag provenance symmetric with the card
# ═══════════════════════════════════════════════════════════════════════

def test_exporter_history_item_carries_model_tag():
    item = _history_item(_judge_record("再想想～"))
    assert item["model_tag"] == TAG
    assert _history_item({"reply": "x"})["model_tag"] == ""


def test_exporter_round_carries_model_tag():
    scoreboard = {
        "solves": [{"user_id": UID, "problem": PID, "nickname": "Alice"}],
        "user_submissions": {str(UID): [{**_judge_record("做法对啦"), "result": "correct"}]},
    }
    rounds = _collect_rounds_for_problem(scoreboard, PID)
    assert len(rounds) == 1
    assert rounds[0]["model_tag"] == TAG
    assert rounds[0]["history_before"] == []


# ═══════════════════════════════════════════════════════════════════════
# private_correct_record_model_tag: tag lookup for the sync cheer
# ═══════════════════════════════════════════════════════════════════════

def test_correct_record_model_tag_returns_first_correct_submit_tag():
    records = [
        _judge_record("不对哦"),
        {**_judge_record("这次对啦"), "result": "correct"},
        {**_judge_record("又提交了一次"), "result": "correct", "model_tag": "『B』"},
    ]
    assert private_correct_record_model_tag(records, PID) == TAG


def test_correct_record_model_tag_empty_without_correct_record():
    records = [_judge_record("不对哦")]
    assert private_record_has_correct(records, PID) is False
    assert private_correct_record_model_tag(records, PID) == ""


def test_correct_record_model_tag_empty_for_legacy_records():
    record = {**_judge_record("对啦"), "result": "correct"}
    record.pop("model_tag")
    assert private_correct_record_model_tag([record], PID) == ""


def test_correct_record_model_tag_ignores_other_problems_and_types():
    records = [
        {**_judge_record("别的题"), "problem": "100A", "result": "correct"},
        {"type": "clarify", "result": "clarify", "problem": PID, "reply": "x", "model_tag": "『C』"},
    ]
    assert private_correct_record_model_tag(records, PID) == ""


# ═══════════════════════════════════════════════════════════════════════
# /sync scored cheer appends the correct record's model tag
# ═══════════════════════════════════════════════════════════════════════

async def _run_scored_cheer(model_tag: str, user_group: UserGroupConfig | None = None) -> str:
    ranked_entry = {"user_id": str(UID), "rank": 1, "score": 12.0, "nickname": "Alice"}
    top5 = [{"rank": 1, "user_id": UID, "nickname": "Alice", "solved": 3, "score": 12.0}]
    with patch.object(sync_mod, "get_today_problem", return_value={"today": PID}), \
         patch.object(sync_mod, "is_group_problem_solved", return_value=False), \
         patch.object(
             sync_mod,
             "run_group_state_update",
             AsyncMock(return_value=(True, 3, top5, {"solves": []})),
         ), \
         patch.object(sync_mod, "get_user_group", return_value=user_group), \
         patch(
             "kouhai_bot.handlers.shared.build_scoreboard_entries",
             return_value=[ranked_entry],
         ), \
         patch.object(sync_mod, "load_known_problem_ratings", return_value={PID: 2300}), \
         patch.object(sync_mod, "fetch_group_member_nickname_map", AsyncMock(return_value={})), \
         patch.object(sync_mod, "_reveal_problem_source", AsyncMock(return_value="本题来自 CF542D Matrix God 2300✨")), \
         patch.object(sync_mod, "schedule_post_solve_editorial_followup", MagicMock()):
        text, scored = await sync_mod._score_synced_private_ac(
            GID, UID, {"nickname": "Alice"}, PID, model_tag=model_tag,
        )
    assert scored is True
    return text


def test_sync_cheer_appends_model_tag():
    text = asyncio.run(_run_scored_cheer(TAG))
    assert text.startswith("恭喜拿下本题一血！🎉")
    assert text.endswith("本题来自 CF542D Matrix God 2300✨" + TAG)
    assert "🏆 Top 5：" in text


def test_sync_cheer_without_model_tag_unchanged():
    text = asyncio.run(_run_scored_cheer(""))
    assert text.endswith("本题来自 CF542D Matrix God 2300✨")
    assert TAG not in text


def test_sync_cheer_named_user_group_gets_group_titled_top5():
    # Parity with the in-group /submit scoreboard message (submit.py
    # _send_scoreboard_success): named user groups get "🏆 {display_name} Top 5：".
    text = asyncio.run(_run_scored_cheer(TAG, UserGroupConfig(name="star", display_name="打星组")))
    assert "🏆 打星组 Top 5：" in text
    assert text.endswith("本题来自 CF542D Matrix God 2300✨" + TAG)
