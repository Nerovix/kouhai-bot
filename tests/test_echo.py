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


def _face(face_id: int | str) -> list[dict]:
    return [{"type": "face", "data": {"id": face_id}}]


def _text_face(text: str, face_id: int | str) -> list[dict]:
    return [{"type": "text", "data": {"text": text}}, {"type": "face", "data": {"id": face_id}}]


def _raw_text(segments: list[dict]) -> str:
    return " ".join(
        seg.get("data", {}).get("text", "").strip()
        for seg in segments
        if seg.get("type") == "text" and seg.get("data", {}).get("text", "").strip()
    ).strip()


ECHO_MESSAGE = _plain("复读")


class _TestConfig:
    bot_qq = BOT_QQ
    current_group = 123456


def _event(
    text: str | None = None,
    *,
    segments: list[dict] | None = None,
    user_id: int = 42,
    group_id: int = _TestConfig.current_group,
) -> dict:
    message = segments if segments is not None else _plain(text or "")
    raw_message = _raw_text(message)
    return {
        "type": "message",
        "message_type": "group",
        "group_id": group_id,
        "user_id": user_id,
        "sender": {"nickname": "Alice", "card": "", "user_id": user_id},
        "message_id": f"msg_{group_id}_{user_id}_{raw_message}",
        "raw_message": raw_message,
        "message": message,
    }


class _Rng:
    def __init__(self, values: list[float]):
        self.values = list(values)

    def __call__(self) -> float:
        if not self.values:
            return 1.0
        return self.values.pop(0)


async def _feed(
    echo: GroupEcho,
    messages: list[str | list[dict]],
    *,
    users: list[int] | None = None,
):
    sent: list[tuple[int, list[dict]]] = []

    async def _send_group_msg(group_id: int, message: list[dict]):
        sent.append((group_id, message))
        return len(sent)

    with patch("kouhai_bot.config._config", _TestConfig()), \
            patch("kouhai_bot.echo.send_group_msg", _send_group_msg):
        for i, message in enumerate(messages):
            user_id = users[i] if users is not None else i + 1
            segments = _plain(message) if isinstance(message, str) else message
            await echo.check_and_echo(
                group_id=_TestConfig.current_group,
                user_id=user_id,
                segments=segments,
                raw_text=_raw_text(segments),
                message_id=f"msg_{i}",
            )

    return sent


def test_two_identical_messages_can_make_bot_third_repeater():
    echo = GroupEcho(rng=lambda: 0.0)

    sent = asyncio.run(_feed(echo, ["复读", "复读"]))

    assert sent == [(_TestConfig.current_group, ECHO_MESSAGE)]
    snapshot = echo.buffer_snapshot()
    assert [entry.echo_key for entry in snapshot] == ["text:复读", "text:复读", "text:复读"]
    assert [entry.segments for entry in snapshot] == [ECHO_MESSAGE, ECHO_MESSAGE, ECHO_MESSAGE]
    assert snapshot[-1].user_id == BOT_QQ


def test_missed_echo_can_trigger_on_later_repeater():
    echo = GroupEcho(rng=_Rng([0.9, 0.0]))

    sent = asyncio.run(_feed(echo, ["复读", "复读", "复读"]))

    assert sent == [(_TestConfig.current_group, ECHO_MESSAGE)]
    snapshot = echo.buffer_snapshot()
    assert [entry.echo_key for entry in snapshot] == ["text:复读", "text:复读", "text:复读", "text:复读"]
    assert snapshot[-1].user_id == BOT_QQ


def test_probability_miss_leaves_repeat_streak_in_buffer():
    echo = GroupEcho(rng=lambda: 0.9)

    sent = asyncio.run(_feed(echo, ["复读", "复读"]))

    assert sent == []
    assert [entry.echo_key for entry in echo.buffer_snapshot()] == ["text:复读", "text:复读"]


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
    assert [entry.echo_key for entry in echo.buffer_snapshot()] == [
        "text:复读",
        "text:/submit 复读",
        "text:复读",
        "text:复读",
        "text:复读",
    ]
    assert [entry.is_command for entry in echo.buffer_snapshot()] == [False, True, False, False, False]


def test_messages_after_command_can_trigger_echo():
    echo = GroupEcho(rng=lambda: 0.0)

    sent = asyncio.run(_feed(echo, ["msg", "msg", "/cmd", "msg", "msg"]))

    assert sent == [(_TestConfig.current_group, _plain("msg")), (_TestConfig.current_group, _plain("msg"))]
    assert [entry.echo_key for entry in echo.buffer_snapshot()] == [
        "text:msg",
        "text:msg",
        "text:msg",
        "text:/cmd",
        "text:msg",
        "text:msg",
        "text:msg",
    ]


def test_non_identical_messages_do_not_trigger():
    echo = GroupEcho(rng=lambda: 0.0)

    sent = asyncio.run(_feed(echo, ["a", "b", "a", "b", "c"]))

    assert sent == []


def test_buffer_is_bounded():
    echo = GroupEcho(max_entries=5, trigger_count=99, rng=lambda: 0.0)

    sent = asyncio.run(_feed(echo, [f"msg {i}" for i in range(8)]))

    assert sent == []
    assert [entry.echo_key for entry in echo.buffer_snapshot()] == [
        "text:msg 3",
        "text:msg 4",
        "text:msg 5",
        "text:msg 6",
        "text:msg 7",
    ]


def test_three_identical_face_only_messages_trigger_echo():
    echo = GroupEcho(trigger_count=3, rng=lambda: 0.0)
    message = _face(123)

    sent = asyncio.run(_feed(echo, [message, message, message]))

    assert sent == [(_TestConfig.current_group, message)]
    snapshot = echo.buffer_snapshot()
    assert [entry.echo_key for entry in snapshot] == ["face:123", "face:123", "face:123", "face:123"]
    assert snapshot[-1].segments == message
    assert snapshot[-1].user_id == BOT_QQ


def test_three_identical_text_face_messages_trigger_echo():
    echo = GroupEcho(trigger_count=3, rng=lambda: 0.0)
    message = _text_face("复读", 66)

    sent = asyncio.run(_feed(echo, [message, message, message]))

    assert sent == [(_TestConfig.current_group, message)]
    assert [entry.echo_key for entry in echo.buffer_snapshot()] == [
        "text:复读\x1fface:66",
        "text:复读\x1fface:66",
        "text:复读\x1fface:66",
        "text:复读\x1fface:66",
    ]


def test_text_face_and_text_only_are_not_same_streak():
    echo = GroupEcho(rng=lambda: 0.0)

    sent = asyncio.run(_feed(echo, [_text_face("复读", 66), "复读"]))

    assert sent == []
    assert [entry.echo_key for entry in echo.buffer_snapshot()] == [
        "text:复读\x1fface:66",
        "text:复读",
    ]


def test_different_face_ids_break_streak():
    echo = GroupEcho(rng=lambda: 0.0)

    sent = asyncio.run(_feed(echo, [_face(123), _face(456), _face(123)]))

    assert sent == []
    assert [entry.echo_key for entry in echo.buffer_snapshot()] == ["face:123", "face:456", "face:123"]


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


def test_process_event_hooks_echo_for_face_only_group_messages():
    sent: list[tuple[int, list[dict]]] = []
    message = _face(123)

    async def _send_group_msg(group_id: int, message: list[dict]):
        sent.append((group_id, message))
        return len(sent)

    async def _run():
        with patch("kouhai_bot.config._config", _TestConfig()), \
                patch("kouhai_bot.echo._echo", GroupEcho(rng=lambda: 0.0)), \
                patch("kouhai_bot.echo.send_group_msg", _send_group_msg):
            for i in range(2):
                await process_event(_event(segments=message, user_id=i + 10), spawn_handlers=False)

    asyncio.run(_run())

    assert sent == [(_TestConfig.current_group, message)]
