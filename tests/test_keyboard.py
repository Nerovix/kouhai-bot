"""Inline-keyboard (message button) builders for the official API.

Buttons are markdown-only; these tests pin the payload shape (action types,
permission, enter/reply flags, row layout) and the CF URL parsing used by the
跳转原题 button.
"""

from __future__ import annotations

import pytest

from kouhai_bot.config import BotConfig
from kouhai_bot.official.keyboard import (
    cf_problem_url,
    callback_button,
    clarify_action_keyboard,
    command_button,
    congrats_keyboard,
    jump_original_keyboard,
    keyboard,
    problem_action_keyboard,
    reject_action_keyboard,
    url_button,
    view_problem_keyboard,
)


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(
        "kouhai_bot.config._config", BotConfig(data_dir="/tmp", app_id="1900000000")
    )


# ── CF URL parsing ──────────────────────────────────────────────────────


@pytest.mark.parametrize("pid,url", [
    ("1856E1", "https://codeforces.com/problemset/problem/1856/E1"),
    ("542D", "https://codeforces.com/problemset/problem/542/D"),
    ("1A", "https://codeforces.com/problemset/problem/1/A"),
    (" 542D ", "https://codeforces.com/problemset/problem/542/D"),
])
def test_cf_problem_url(pid, url):
    assert cf_problem_url(pid) == url


@pytest.mark.parametrize("pid", ["", "abc", "123", "E1", "12/", None])
def test_cf_problem_url_unparseable(pid):
    assert cf_problem_url(pid) == ""


# ── primitive button shapes ─────────────────────────────────────────────


def test_command_button_shape():
    b = command_button("submit", "提交", "/sbm")
    assert b["id"] == "submit"
    assert b["render_data"] == {"label": "提交", "style": 1}
    assert b["action"]["type"] == 2
    assert b["action"]["permission"] == {"type": 2}
    assert b["action"]["data"] == "/sbm"
    assert b["action"]["enter"] is False
    assert b["action"]["reply"] is False
    assert b["action"]["unsupport_tips"]


def test_command_button_enter_sends_immediately():
    b = command_button("clear", "清空上下文", "/clear", enter=True)
    assert b["action"]["enter"] is True


def test_url_button_shape():
    b = url_button("original", "跳转原题", "https://example.com")
    assert b["action"]["type"] == 0
    assert b["action"]["data"] == "https://example.com"
    assert b["action"]["permission"] == {"type": 2}


def test_keyboard_rows_and_appid():
    kb = keyboard([[command_button("a", "A", "/a")]], bot_appid="1900000000")
    assert kb["content"]["bot_appid"] == 1900000000
    rows = kb["content"]["rows"]
    assert len(rows) == 1
    assert len(rows[0]["buttons"]) == 1


def test_keyboard_omits_appid_when_unset(monkeypatch):
    monkeypatch.setattr("kouhai_bot.config._config", BotConfig(data_dir="/tmp"))
    kb = problem_action_keyboard()
    assert "bot_appid" not in kb["content"]


# ── convenience keyboards ───────────────────────────────────────────────


def _labels(kb):
    return [b["render_data"]["label"] for row in kb["content"]["rows"] for b in row["buttons"]]


def test_problem_action_keyboard_one_row_two_buttons():
    kb = problem_action_keyboard()
    assert len(kb["content"]["rows"]) == 1
    assert _labels(kb) == ["提交", "询问题意"]
    buttons = kb["content"]["rows"][0]["buttons"]
    assert [b["action"]["data"] for b in buttons] == ["/sbm", "/clrf"]
    assert all(b["action"]["enter"] is False for b in buttons)


def test_clarify_action_keyboard_one_row_two_buttons():
    kb = clarify_action_keyboard()
    assert len(kb["content"]["rows"]) == 1
    assert _labels(kb) == ["提交", "继续询问"]
    buttons = kb["content"]["rows"][0]["buttons"]
    assert [b["action"]["data"] for b in buttons] == ["/sbm", "/clrf"]
    assert all(b["action"]["enter"] is False for b in buttons)


def test_reject_action_keyboard_clear_callback():
    kb = reject_action_keyboard()
    assert len(kb["content"]["rows"]) == 1
    assert _labels(kb) == ["继续提交", "清空上下文"]
    buttons = kb["content"]["rows"][0]["buttons"]
    assert [b["action"]["data"] for b in buttons] == ["/sbm", "/clear"]
    assert buttons[0]["action"]["type"] == 2
    assert buttons[0]["action"]["enter"] is False
    # clear is a no-arg callback (type=1), not an auto-send command button
    assert buttons[1]["action"]["type"] == 1
    assert "enter" not in buttons[1]["action"]


def test_jump_original_keyboard_single_full_width():
    kb = jump_original_keyboard("1856E1")
    assert len(kb["content"]["rows"]) == 1
    assert len(kb["content"]["rows"][0]["buttons"]) == 1
    assert _labels(kb) == ["跳转原题"]
    btn = kb["content"]["rows"][0]["buttons"][0]
    assert btn["action"]["type"] == 0
    assert btn["action"]["data"] == "https://codeforces.com/problemset/problem/1856/E1"


def test_jump_original_keyboard_none_for_bad_pid():
    assert jump_original_keyboard("") is None
    assert jump_original_keyboard("not-a-pid") is None


def test_view_problem_keyboard_single_callback():
    kb = view_problem_keyboard()
    assert len(kb["content"]["rows"]) == 1
    assert len(kb["content"]["rows"][0]["buttons"]) == 1
    assert _labels(kb) == ["查看当前题目"]
    btn = kb["content"]["rows"][0]["buttons"][0]
    assert btn["action"]["type"] == 1
    assert btn["action"]["data"] == "/pb"
    assert "enter" not in btn["action"]


def test_congrats_keyboard_jump_plus_next():
    kb = congrats_keyboard("1856E1")
    assert len(kb["content"]["rows"]) == 1
    assert _labels(kb) == ["跳转原题", "再来一题"]
    jump_btn, next_btn = kb["content"]["rows"][0]["buttons"]
    assert jump_btn["action"]["type"] == 0
    assert jump_btn["action"]["data"] == "https://codeforces.com/problemset/problem/1856/E1"
    assert next_btn["action"]["type"] == 1
    assert next_btn["action"]["data"] == "/np"
    assert "enter" not in next_btn["action"]


def test_congrats_keyboard_bad_pid_next_only():
    kb = congrats_keyboard("not-a-pid")
    assert _labels(kb) == ["再来一题"]
    btn = kb["content"]["rows"][0]["buttons"][0]
    assert btn["action"]["type"] == 1
    assert btn["action"]["data"] == "/np"
    assert "enter" not in btn["action"]


def test_callback_button_shape():
    b = callback_button("next", "再来一题", "/np")
    assert b["id"] == "next"
    assert b["render_data"] == {"label": "再来一题", "style": 1}
    assert b["action"]["type"] == 1
    assert b["action"]["permission"] == {"type": 2}
    assert b["action"]["data"] == "/np"
    assert b["action"]["unsupport_tips"]
    assert "enter" not in b["action"] and "reply" not in b["action"]