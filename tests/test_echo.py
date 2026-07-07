import asyncio
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.echo import GroupEcho
from kouhai_bot.handlers import process_event


BOT_QQ = 1234567890


def _plain(text: str) -> list[dict]:
    return [{"type": "text", "data": {"text": text}}]


ECHO_MESSAGE = _plain("复读")


class _TestConfig:
    bot_qq = BOT_QQ
    current_group = 123456


def _event(text: str, *, user_id: int = 42, group_id: int = _TestConfig.current_group) -> dict:
    return {
        "type": "message",
        "message_type": "group",
        "group_id": group_id,
        "user_id": user_id,
        "sender": {"nickname": "Alice", "card": "", "user_id": user_id},
        "message_id": f"msg_{group_id}_{user_id}_{text}",
        "raw_message": text,
        "message": [{"type": "text", "data": {"text": text}}],
    }


class _Rng:
    def __init__(self, values: list[float]):
        self.values = list(values)

    def __call__(self) -> float:
        if not self.values:
            return 1.0
        return self.values.pop(0)


async def _feed(echo: GroupEcho, texts: list[str], *, users: list[int] | None = None):
    sent: list[tuple[int, list[dict]]] = []

    async def _send_group_msg(group_id: int, message: list[dict]):
        sent.append((group_id, message))
        return len(sent)

    with patch("kouhai_bot.config._config", _TestConfig()), \
            patch("kouhai_bot.echo.send_group_msg", _send_group_msg):
        for i, text in enumerate(texts):
            user_id = users[i] if users is not None else i + 1
            await echo.check_and_echo(
                group_id=_TestConfig.current_group,
                user_id=user_id,
                raw_text=text,
                message_id=f"msg_{i}",
            )

    return sent


def test_two_identical_messages_can_make_bot_third_repeater():
    echo = GroupEcho(rng=lambda: 0.0)

    sent = asyncio.run(_feed(echo, ["复读", "复读"]))

    assert sent == [(_TestConfig.current_group, ECHO_MESSAGE)]
    snapshot = echo.buffer_snapshot()
    assert [entry.raw_text for entry in snapshot] == ["复读", "复读", "复读"]
    assert snapshot[-1].user_id == BOT_QQ


def test_missed_echo_can_trigger_on_later_repeater():
    echo = GroupEcho(rng=_Rng([0.9, 0.0]))

    sent = asyncio.run(_feed(echo, ["复读", "复读", "复读"]))

    assert sent == [(_TestConfig.current_group, ECHO_MESSAGE)]
    snapshot = echo.buffer_snapshot()
    assert [entry.raw_text for entry in snapshot] == ["复读", "复读", "复读", "复读"]
    assert snapshot[-1].user_id == BOT_QQ


def test_probability_miss_leaves_repeat_streak_in_buffer():
    echo = GroupEcho(rng=lambda: 0.9)

    sent = asyncio.run(_feed(echo, ["复读", "复读"]))

    assert sent == []
    assert [entry.raw_text for entry in echo.buffer_snapshot()] == ["复读", "复读"]


def test_bot_in_streak_prevents_duplicate_echo():
    echo = GroupEcho(rng=lambda: 0.0)

    sent = asyncio.run(_feed(echo, ["复读", "复读", "复读", "复读", "复读"]))

    assert sent == [(_TestConfig.current_group, ECHO_MESSAGE)]
    snapshot = echo.buffer_snapshot()
    assert [entry.user_id for entry in snapshot].count(BOT_QQ) == 1


def test_existing_bot_in_user_streak_prevents_echo():
    echo = GroupEcho(rng=lambda: 0.0)

    sent = asyncio.run(_feed(
        echo,
        ["复读", "复读", "复读"],
        users=[101, BOT_QQ, 102],
    ))

    assert sent == []
    assert [entry.user_id for entry in echo.buffer_snapshot()] == [101, BOT_QQ, 102]


def test_command_messages_break_streak_and_stay_in_buffer():
    echo = GroupEcho(rng=lambda: 0.0)

    sent = asyncio.run(_feed(echo, ["复读", "/submit 复读", "复读", "复读"]))

    assert sent == [(_TestConfig.current_group, ECHO_MESSAGE)]
    assert [entry.raw_text for entry in echo.buffer_snapshot()] == [
        "复读",
        "/submit 复读",
        "复读",
        "复读",
        "复读",
    ]


def test_messages_after_command_can_trigger_echo():
    echo = GroupEcho(rng=lambda: 0.0)

    sent = asyncio.run(_feed(echo, ["msg", "msg", "/cmd", "msg", "msg"]))

    assert sent == [(_TestConfig.current_group, _plain("msg")), (_TestConfig.current_group, _plain("msg"))]
    assert [entry.raw_text for entry in echo.buffer_snapshot()] == ["msg", "msg", "msg", "/cmd", "msg", "msg", "msg"]


def test_non_identical_messages_do_not_trigger():
    echo = GroupEcho(rng=lambda: 0.0)

    sent = asyncio.run(_feed(echo, ["a", "b", "a", "b", "c"]))

    assert sent == []


def test_buffer_is_bounded():
    echo = GroupEcho(max_entries=5, trigger_count=99, rng=lambda: 0.0)

    sent = asyncio.run(_feed(echo, [f"msg {i}" for i in range(8)]))

    assert sent == []
    assert [entry.raw_text for entry in echo.buffer_snapshot()] == [
        "msg 3",
        "msg 4",
        "msg 5",
        "msg 6",
        "msg 7",
    ]


def test_process_event_hooks_echo_for_non_command_group_messages():
    sent: list[tuple[int, list[dict]]] = []

    async def _send_group_msg(group_id: int, message: list[dict]):
        sent.append((group_id, message))
        return len(sent)

    async def _run():
        with patch("kouhai_bot.config._config", _TestConfig()), \
                patch("kouhai_bot.echo._echo", GroupEcho(rng=lambda: 0.0)), \
                patch("kouhai_bot.echo.send_group_msg", _send_group_msg):
            for i in range(2):
                await process_event(_event("复读", user_id=i + 10), spawn_handlers=False)

    asyncio.run(_run())

    assert sent == [(_TestConfig.current_group, ECHO_MESSAGE)]
