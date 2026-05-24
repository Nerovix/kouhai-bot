"""Tests for backfilling command event logs from scoreboard history."""

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.achievements import build_achievement_report
from kouhai_bot.eventlog import TZ, load_events
from kouhai_bot.eventlog_backfill import backfill_command_events


GID = 123456
BOT_QQ = 1234567890


class _TestConfig:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.bot_qq = BOT_QQ
        self.current_group = GID


def _temp_data_dir():
    root = tempfile.mkdtemp(prefix="xcpc_backfill_")
    data_dir = os.path.join(root, "data")
    os.makedirs(os.path.join(data_dir, "groups", str(GID)), exist_ok=True)
    return root, data_dir


def _write_scoreboard(data_dir: str, data: dict) -> None:
    path = os.path.join(data_dir, "groups", str(GID), "scoreboard.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def test_backfill_command_events_from_scoreboard_history():
    root, data_dir = _temp_data_dir()
    try:
        _write_scoreboard(data_dir, {
            "solves": [
                {"user_id": 42, "nickname": "Alice", "date": "2026-05-14", "problem": "542D", "order": 1},
                {"user_id": 99, "nickname": "Bob", "date": "2026-05-14", "problem": "542D", "order": 2},
            ],
            "user_submissions": {
                "42": [
                    {
                        "timestamp": "2026-05-14T05:00:00+08:00",
                        "content": "wrong idea",
                        "result": "incorrect",
                        "reason": "no",
                        "reply": "",
                        "problem": "542D",
                    },
                    {
                        "timestamp": "2026-05-14T06:00:00+08:00",
                        "content": "right idea",
                        "result": "correct",
                        "reason": "yes",
                        "reply": "",
                        "problem": "542D",
                    },
                    {
                        "timestamp": "2026-05-14T07:00:00+08:00",
                        "content": "why?",
                        "result": "clarify",
                        "reason": "",
                        "reply": "ok",
                        "problem": "542D",
                    },
                ],
                "99": [
                    {
                        "timestamp": "2026-05-14T08:00:00+08:00",
                        "content": "review this",
                        "result": "review",
                        "reason": "",
                        "reply": "ok",
                        "problem": "542D",
                    },
                ],
            },
        })

        with patch("kouhai_bot.config._config", _TestConfig(data_dir)):
            summary = backfill_command_events(
                group_ids=[GID],
                since=datetime(2026, 5, 14, 4, 0, tzinfo=TZ),
                until=datetime(2026, 5, 15, 4, 0, tzinfo=TZ),
            )
            assert summary.records_seen == 4
            assert summary.records_written == 4
            assert summary.events_written == 8

            again = backfill_command_events(
                group_ids=[GID],
                since=datetime(2026, 5, 14, 4, 0, tzinfo=TZ),
                until=datetime(2026, 5, 15, 4, 0, tzinfo=TZ),
            )
            assert again.records_seen == 4
            assert again.records_written == 0
            assert again.events_written == 0

            events = load_events(GID, "2026-05-14")
            assert len(events) == 8
            assert all(item.get("source") == "backfill_scoreboard" for item in events)
            assert [item["type"] for item in events[:2]] == ["received", "finished"]
            assert events[0]["command"] == "submit"
            assert events[1]["status"] == "incorrect"

            report = build_achievement_report(
                GID,
                now=datetime(2026, 5, 15, 12, 0, tzinfo=TZ),
            )
            assert "通过题目最多：Alice（1 次）" in report
            assert "submit 尝试最多：Alice（2 次）" in report
            assert "review 最多：Bob（1 次）" in report
            assert "clarify 最多：Alice（1 次）" in report
    finally:
        shutil.rmtree(root)
