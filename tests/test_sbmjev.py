import os
import sys
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.handlers.cmd import submit as submit_mod
from kouhai_bot.jev import JevResult


GID = 999999
UID = 42
PID = "542D"

REQUEST = {"state": {"x": 1}, "questions": {"verdict": {}}}
FIRST_PASS = {"correct": True, "reason": "", "reply": "", "reaction": ""}


def _editorial():
    return SimpleNamespace(
        text="Official editorial body",
        tutorial_title="Tutorial",
        tutorial_url="https://example.com/editorial",
    )


@contextmanager
def _handler_env(*, results, editorial=None, first_error=None):
    """Patch every dependency of the /sbmjev handler and expose the mocks."""
    env = SimpleNamespace()
    env.run = AsyncMock(
        side_effect=results if isinstance(results, list) else [results]
    )
    env.build_first = Mock(return_value=REQUEST)
    env.build_second = Mock(return_value=REQUEST)
    env.send_group = AsyncMock()
    env.send_private = AsyncMock()
    env.react = AsyncMock()
    env.save_group = Mock()
    env.save_private = Mock()

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(
                submit_mod, "get_today_problem",
                return_value={"today": PID, "rating": 2500},
            )
        )
        stack.enter_context(
            patch.object(
                submit_mod, "get_private_current_problem",
                return_value={"today": PID, "rating": 2500},
            )
        )
        stack.enter_context(
            patch.object(
                submit_mod, "load_problem_statement",
                return_value="Problem statement",
            )
        )
        stack.enter_context(
            patch.object(submit_mod, "_load_user_problem_history", return_value=[])
        )
        stack.enter_context(
            patch.object(
                submit_mod, "get_verified_official_editorial", return_value=editorial
            )
        )
        stack.enter_context(
            patch.object(submit_mod, "is_curfew_active", return_value=False)
        )
        if first_error is not None:
            stack.enter_context(
                patch.object(
                    submit_mod, "build_jev_first_judge_request",
                    side_effect=first_error,
                )
            )
        else:
            stack.enter_context(
                patch.object(
                    submit_mod, "build_jev_first_judge_request", env.build_first
                )
            )
        stack.enter_context(
            patch.object(
                submit_mod, "build_jev_second_judge_request", env.build_second
            )
        )
        stack.enter_context(patch.object(submit_mod, "run_jev", new=env.run))
        stack.enter_context(
            patch.object(submit_mod, "send_group_msg", new=env.send_group)
        )
        stack.enter_context(
            patch.object(submit_mod, "send_private_msg", new=env.send_private)
        )
        stack.enter_context(patch.object(submit_mod, "react_emoji", new=env.react))
        stack.enter_context(
            patch.object(submit_mod, "save_user_submission", new=env.save_group)
        )
        stack.enter_context(
            patch.object(submit_mod, "save_private_submission", new=env.save_private)
        )
        yield env


async def _invoke(message_id: str = "m1", *, private: bool = False):
    await submit_mod.handle_jev(
        GID, UID, {"nickname": "Alice"}, message_id,
        "/sbmjev 我的做法", [],
        {"message_type": "private" if private else "group"},
    )


@pytest.mark.asyncio
async def test_first_incorrect_reacts_479_without_second_pass():
    with _handler_env(results=[JevResult(choice="incorrect")]) as env:
        await _invoke("m1")

    env.build_first.assert_called_once()
    assert env.build_first.call_args.args[1] == "我的做法"
    env.run.assert_awaited_once()
    env.build_second.assert_not_called()
    env.send_group.assert_not_awaited()
    env.react.assert_awaited_once_with("m1", "479")
    env.save_group.assert_not_called()
    env.save_private.assert_not_called()


@pytest.mark.asyncio
async def test_first_correct_without_editorial_reacts_478():
    with _handler_env(results=[JevResult(choice="correct")]) as env:
        await _invoke("m2")

    env.run.assert_awaited_once()
    env.build_second.assert_not_called()
    env.react.assert_awaited_once_with("m2", "478")


@pytest.mark.asyncio
async def test_second_pass_runs_and_can_react_478():
    with _handler_env(
        results=[JevResult(choice="correct"), JevResult(choice="correct")],
        editorial=_editorial(),
    ) as env:
        await _invoke("m3")

    assert env.run.await_count == 2
    env.build_second.assert_called_once()
    assert env.build_second.call_args.kwargs["first_judge_result"] == FIRST_PASS
    assert env.build_second.call_args.args[3] == "Official editorial body"
    env.react.assert_awaited_once_with("m3", "478")


@pytest.mark.asyncio
async def test_second_pass_can_overturn_to_479():
    with _handler_env(
        results=[JevResult(choice="correct"), JevResult(choice="incorrect")],
        editorial=_editorial(),
    ) as env:
        await _invoke("m4")

    assert env.run.await_count == 2
    env.react.assert_awaited_once_with("m4", "479")


@pytest.mark.asyncio
async def test_second_pass_failure_falls_back_to_first_verdict():
    with _handler_env(
        results=[JevResult(choice="correct"), JevResult(failure_kind="timeout")],
        editorial=_editorial(),
    ) as env:
        await _invoke("m5")

    assert env.run.await_count == 2
    env.react.assert_awaited_once_with("m5", "478")


@pytest.mark.asyncio
async def test_private_sbmjev_degrades_to_text_emoji():
    with _handler_env(results=[JevResult(choice="incorrect")]) as env:
        await _invoke("m6", private=True)

    env.react.assert_not_awaited()
    env.send_group.assert_not_awaited()
    env.send_private.assert_awaited_once_with(
        UID, [{"type": "text", "data": {"text": "❌"}}]
    )
    env.save_private.assert_not_called()


@pytest.mark.asyncio
async def test_private_sbmjev_failure_sends_warning_emoji():
    with _handler_env(results=[JevResult(failure_kind="timeout")]) as env:
        await _invoke("m10", private=True)

    env.send_private.assert_awaited_once_with(
        UID, [{"type": "text", "data": {"text": "⚠️"}}]
    )


@pytest.mark.asyncio
async def test_sbmjev_requires_submission_text():
    with patch.object(submit_mod, "send_group_msg", new=AsyncMock()) as send_group, \
         patch.object(submit_mod, "run_jev", new=AsyncMock()) as run:
        await submit_mod.handle_jev(
            GID, UID, {"nickname": "Alice"}, "m7",
            "/sbmjev", [], {"message_type": "group"},
        )

    run.assert_not_awaited()
    send_group.assert_awaited_once()
    text = send_group.call_args.args[1][1]["data"]["text"]
    assert "用法" in text


@pytest.mark.asyncio
async def test_first_pass_crash_reacts_312():
    with _handler_env(
        results=[JevResult(choice="correct")],
        first_error=RuntimeError("prompt drift"),
    ) as env:
        await _invoke("m8")

    env.run.assert_not_awaited()
    env.react.assert_awaited_once_with("m8", "312")


@pytest.mark.asyncio
async def test_first_pass_failure_reacts_312():
    with _handler_env(results=[JevResult(failure_kind="timeout")]) as env:
        await _invoke("m9")

    env.build_first.assert_called_once()
    env.react.assert_awaited_once_with("m9", "312")
