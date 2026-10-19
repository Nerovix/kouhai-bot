import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.handlers.cmd import submit as submit_mod
from kouhai_bot.jev import JevResult


GID = 999999
UID = 42
PID = "542D"


def _jev_result(choice: str = "correct") -> JevResult:
    return JevResult(
        choice=choice,
        probabilities={"correct": 0.88 if choice == "correct" else 0.12,
                       "incorrect": 0.12 if choice == "correct" else 0.88},
        confidence=0.76,
        model="jev-1.13.0",
        input_tokens=340,
    )


def _patches(**overrides):
    """Patch every dependency of the /sbmjev handler."""
    defaults = {
        "get_today_problem": {"today": PID, "rating": 2500},
        "get_private_current_problem": {"today": PID, "rating": 2500},
        "load_problem_statement": "Problem statement",
        "_load_user_problem_history": [],
        "get_verified_official_editorial": None,
        "is_curfew_active": False,
        "build_jev_second_judge_request": {"state": {"x": 1}, "questions": {"verdict": {}}},
        "run_jev": _jev_result(),
    }
    defaults.update(overrides)
    return defaults


@pytest.mark.asyncio
async def test_group_sbmjev_reacts_without_writing_state():
    values = _patches()
    with patch.object(submit_mod, "get_today_problem", return_value=values["get_today_problem"]), \
         patch.object(submit_mod, "load_problem_statement", return_value=values["load_problem_statement"]), \
         patch.object(submit_mod, "_load_user_problem_history", return_value=values["_load_user_problem_history"]), \
         patch.object(submit_mod, "get_verified_official_editorial", return_value=None), \
         patch.object(submit_mod, "is_curfew_active", return_value=False), \
         patch.object(submit_mod, "build_jev_second_judge_request", return_value=values["build_jev_second_judge_request"]) as build, \
         patch.object(submit_mod, "run_jev", new=AsyncMock(return_value=values["run_jev"])) as run, \
         patch.object(submit_mod, "send_group_msg") as send_group, \
         patch.object(submit_mod, "react_emoji", new=AsyncMock()) as react, \
         patch.object(submit_mod, "save_user_submission") as save_group, \
         patch.object(submit_mod, "save_private_submission") as save_private:
        await submit_mod.handle_jev(
            GID, UID, {"nickname": "Alice"}, "m1",
            "/sbmjev 我的做法", [], {"message_type": "group"},
        )

    build.assert_called_once()
    assert build.call_args.args[1] == "我的做法"
    run.assert_awaited_once()
    save_group.assert_not_called()
    save_private.assert_not_called()
    send_group.assert_not_called()
    react.assert_awaited_once_with("m1", "478")


@pytest.mark.asyncio
async def test_group_sbmjev_incorrect_reacts_479():
    values = _patches()
    with patch.object(submit_mod, "get_today_problem", return_value=values["get_today_problem"]), \
         patch.object(submit_mod, "load_problem_statement", return_value=values["load_problem_statement"]), \
         patch.object(submit_mod, "_load_user_problem_history", return_value=[]), \
         patch.object(submit_mod, "get_verified_official_editorial", return_value=None), \
         patch.object(submit_mod, "is_curfew_active", return_value=False), \
         patch.object(submit_mod, "build_jev_second_judge_request", return_value=values["build_jev_second_judge_request"]), \
         patch.object(submit_mod, "run_jev", new=AsyncMock(return_value=_jev_result("incorrect"))), \
         patch.object(submit_mod, "send_group_msg") as send_group, \
         patch.object(submit_mod, "react_emoji", new=AsyncMock()) as react:
        await submit_mod.handle_jev(
            GID, UID, {"nickname": "Alice"}, "m1b",
            "/sbmjev 我的做法", [], {"message_type": "group"},
        )

    send_group.assert_not_called()
    react.assert_awaited_once_with("m1b", "479")


@pytest.mark.asyncio
async def test_private_sbmjev_degrades_to_face_message():
    sent = []

    async def fake_send_private(user_id, message):
        sent.append((user_id, message))
        return 1

    values = _patches(load_problem_statement="Problem statement")
    with patch.object(submit_mod, "get_private_current_problem", return_value=values["get_private_current_problem"]), \
         patch.object(submit_mod, "load_problem_statement", return_value=values["load_problem_statement"]), \
         patch.object(submit_mod, "_load_user_problem_history", return_value=[]), \
         patch.object(submit_mod, "get_verified_official_editorial", return_value=None), \
         patch.object(submit_mod, "is_curfew_active", return_value=False), \
         patch.object(submit_mod, "build_jev_second_judge_request", return_value=values["build_jev_second_judge_request"]), \
         patch.object(submit_mod, "run_jev", new=AsyncMock(return_value=_jev_result("incorrect"))), \
         patch.object(submit_mod, "send_private_msg", side_effect=fake_send_private), \
         patch.object(submit_mod, "send_group_msg") as send_group, \
         patch.object(submit_mod, "react_emoji", new=AsyncMock()) as react, \
         patch.object(submit_mod, "save_private_submission") as save_private:
        await submit_mod.handle_jev(
            GID, UID, {"nickname": "Alice"}, "m2",
            "/sbmjev 我的做法", [], {"message_type": "private"},
        )

    send_group.assert_not_called()
    react.assert_not_awaited()
    save_private.assert_not_called()
    assert len(sent) == 1
    assert sent[0][1] == [{"type": "face", "data": {"id": "479"}}]


@pytest.mark.asyncio
async def test_sbmjev_requires_submission_text():
    sent = []

    async def fake_send_group(group_id, message):
        sent.append((group_id, message))
        return 1

    with patch.object(submit_mod, "send_group_msg", side_effect=fake_send_group), \
         patch.object(submit_mod, "run_jev", new=AsyncMock()) as run:
        await submit_mod.handle_jev(
            GID, UID, {"nickname": "Alice"}, "m3",
            "/sbmjev", [], {"message_type": "group"},
        )

    run.assert_not_awaited()
    assert len(sent) == 1
    assert "用法" in sent[0][1][1]["data"]["text"]


@pytest.mark.asyncio
async def test_sbmjev_failure_reacts_312():
    values = _patches()
    with patch.object(submit_mod, "get_today_problem", return_value=values["get_today_problem"]), \
         patch.object(submit_mod, "load_problem_statement", return_value=values["load_problem_statement"]), \
         patch.object(submit_mod, "_load_user_problem_history", return_value=[]), \
         patch.object(submit_mod, "get_verified_official_editorial", return_value=None), \
         patch.object(submit_mod, "is_curfew_active", return_value=False), \
         patch.object(submit_mod, "build_jev_second_judge_request", return_value=values["build_jev_second_judge_request"]), \
         patch.object(submit_mod, "run_jev", new=AsyncMock(return_value=JevResult(failure_kind="timeout"))), \
         patch.object(submit_mod, "send_group_msg") as send_group, \
         patch.object(submit_mod, "react_emoji", new=AsyncMock()) as react:
        await submit_mod.handle_jev(
            GID, UID, {"nickname": "Alice"}, "m4",
            "/sbmjev 我的做法", [], {"message_type": "group"},
        )

    send_group.assert_not_called()
    react.assert_awaited_once_with("m4", "312")
