"""Tests for daily achievement reports."""

import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.achievements import achievement_window, build_achievement_report
from kouhai_bot.eventlog import TZ, log_command_finished, log_command_received
from kouhai_bot.scheduler.engine import _normalize_enabled_jobs


GID = 123456
BOT_QQ = 1234567890


class _TestConfig:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.bot_qq = BOT_QQ
        self.current_group = GID


def _temp_data_dir():
    root = tempfile.mkdtemp(prefix="xcpc_achievements_")
    data_dir = os.path.join(root, "data")
    os.makedirs(data_dir, exist_ok=True)
    return root, data_dir


def _log_command(
    *,
    group_id: int = GID,
    user_id: int,
    nickname: str,
    command: str,
    at: datetime,
    status: str = "ok",
) -> None:
    with patch("kouhai_bot.eventlog.now_tz", return_value=at):
        meta = log_command_received(
            group_id=group_id,
            user_id=user_id,
            sender={"nickname": nickname, "card": "", "user_id": user_id},
            command=command,
            message_id=f"msg_{user_id}_{command}_{at.timestamp()}",
            raw_text=f"/{command} payload",
        )
    with patch("kouhai_bot.eventlog.now_tz", return_value=at + timedelta(seconds=2)):
        log_command_finished(meta, status=status, problem="542D")


def test_achievement_window_uses_4am_cutoff():
    now = datetime(2026, 5, 15, 12, 0, tzinfo=TZ)
    window = achievement_window(now)
    assert window.start == datetime(2026, 5, 14, 4, 0, tzinfo=TZ)
    assert window.end == datetime(2026, 5, 15, 4, 0, tzinfo=TZ)

    early_now = datetime(2026, 5, 15, 3, 30, tzinfo=TZ)
    early_window = achievement_window(early_now)
    assert early_window.start == datetime(2026, 5, 13, 4, 0, tzinfo=TZ)
    assert early_window.end == datetime(2026, 5, 14, 4, 0, tzinfo=TZ)


def test_build_achievement_report_counts_window_and_statuses():
    root, data_dir = _temp_data_dir()
    try:
        with patch("kouhai_bot.config._config", _TestConfig(data_dir)):
            _log_command(
                user_id=1,
                nickname="TooEarly",
                command="submit",
                at=datetime(2026, 5, 14, 3, 59, tzinfo=TZ),
                status="correct",
            )
            _log_command(
                user_id=1,
                nickname="Alice",
                command="submit",
                at=datetime(2026, 5, 14, 4, 1, tzinfo=TZ),
                status="incorrect",
            )
            _log_command(
                user_id=1,
                nickname="Alice",
                command="submit",
                at=datetime(2026, 5, 14, 8, 0, tzinfo=TZ),
                status="correct",
            )
            _log_command(
                user_id=2,
                nickname="Bob",
                command="submit",
                at=datetime(2026, 5, 15, 3, 50, tzinfo=TZ),
                status="correct",
            )
            _log_command(
                user_id=2,
                nickname="Bob",
                command="clarify",
                at=datetime(2026, 5, 14, 5, 0, tzinfo=TZ),
            )
            _log_command(
                user_id=1,
                nickname="Alice",
                command="review",
                at=datetime(2026, 5, 14, 9, 0, tzinfo=TZ),
            )
            _log_command(
                user_id=1,
                nickname="Alice",
                command="review",
                at=datetime(2026, 5, 14, 10, 0, tzinfo=TZ),
            )
            _log_command(
                user_id=3,
                nickname="TooLate",
                command="submit",
                at=datetime(2026, 5, 15, 4, 1, tzinfo=TZ),
                status="correct",
            )

            report = build_achievement_report(
                GID,
                now=datetime(2026, 5, 15, 12, 0, tzinfo=TZ),
            )

        assert "统计窗口：05-14 04:00 ~ 05-15 04:00" in report
        assert "最早 submit：Alice（04:01）" in report
        assert "最晚 submit：Bob（03:50）" in report
        assert "通过题目最多：Alice、Bob（1 次）" in report
        assert "submit 尝试最多：Alice（2 次）" in report
        assert "review 最多：Alice（2 次）" in report
        assert "clarify 最多：Bob（1 次）" in report
        assert "TooEarly" not in report
        assert "TooLate" not in report
    finally:
        shutil.rmtree(root)


def test_build_achievement_report_handles_empty_day():
    root, data_dir = _temp_data_dir()
    try:
        with patch("kouhai_bot.config._config", _TestConfig(data_dir)):
            report = build_achievement_report(
                GID,
                now=datetime(2026, 5, 15, 12, 0, tzinfo=TZ),
            )
        assert "昨日还没有可统计的指令记录。" in report
    finally:
        shutil.rmtree(root)


def test_scheduler_normalizes_old_daily_post_configs():
    jobs = _normalize_enabled_jobs(["daily_post", "contest_check"])
    assert jobs == ["daily_achievements", "daily_post", "contest_check"]

    disabled = _normalize_enabled_jobs(
        ["daily_post", "contest_check"],
        ["daily_achievements"],
    )
    assert disabled == ["daily_post", "contest_check"]
