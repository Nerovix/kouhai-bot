"""Tests for official-native multi-group echo/repeat detection.

Official group content is plain text (no OneBot segments), so the repeat key
is the NFC-normalized, mention-stripped text. Each group has an independent
bounded buffer; the bot joins a streak probabilistically via the runtime
sender.
"""

import pytest

from kouhai_bot import echo
from kouhai_bot.db import Database
from kouhai_bot.echo import (
    GroupEcho,
    canonical_echo_key,
    check_and_echo,
    reset_echo_for_tests,
)
from kouhai_bot.handlers import process_event
from kouhai_bot.handlers import registry
from kouhai_bot.handlers.runtime import Runtime, set_runtime
from kouhai_bot.official import OfficialSender, ReplyBudget

GROUP = "GROUP_A"
BOT = "__bot__"


class FakeRest:
    def __init__(self):
        self.sent = []
        self.uploads = []

    async def send_group_message(self, group_openid, payload):
        self.sent.append((group_openid, payload))
        return {"id": f"M{len(self.sent)}"}

    async def upload_group_media(self, group_openid, payload):
        self.uploads.append((group_openid, payload))
        return {"file_info": "FI", "file_uuid": "u", "ttl": 300}


@pytest.fixture
def rest():
    return FakeRest()


@pytest.fixture(autouse=True)
async def runtime(db: Database, rest: FakeRest):
    sender = OfficialSender(rest, ReplyBudget(db))
    rt = Runtime(db=db, sender=sender, browser=None)
    set_runtime(rt)
    return rt


class _Rng:
    """Deterministic rng: pops preset values, then always misses (1.0)."""

    def __init__(self, values: list[float]):
        self.values = list(values)

    def __call__(self) -> float:
        if not self.values:
            return 1.0
        return self.values.pop(0)


async def _feed(
    echo_obj: GroupEcho,
    messages: list[str],
    *,
    users: list[str] | None = None,
    group: str = GROUP,
) -> list[bool]:
    results = []
    for i, message in enumerate(messages):
        user = users[i] if users is not None else f"USER_{i}"
        results.append(await echo_obj.check_and_echo(
            group_openid=group,
            user_openid=user,
            content=message,
            raw_text=message,
            message_id=f"m{i}",
        ))
    return results


def _sent_texts(rest: FakeRest) -> list[tuple[str, str]]:
    return [(group, payload["content"]) for group, payload in rest.sent]


async def test_two_identical_messages_make_bot_third_repeater(rest):
    echo_obj = GroupEcho(rng=lambda: 0.0)

    results = await _feed(echo_obj, ["复读", "复读"])

    assert results == [False, True]
    assert _sent_texts(rest) == [(GROUP, "复读")]
    snapshot = echo_obj.buffer_snapshot()
    assert [entry.echo_key for entry in snapshot] == ["复读", "复读", "复读"]
    assert snapshot[-1].user_openid == BOT
    assert snapshot[-1].content == "复读"
    assert snapshot[-1].message_id == "echo:m1"


async def test_missed_echo_can_trigger_on_later_repeater(rest):
    echo_obj = GroupEcho(rng=_Rng([0.9, 0.0]))

    results = await _feed(echo_obj, ["复读", "复读", "复读"])

    assert results == [False, False, True]
    assert _sent_texts(rest) == [(GROUP, "复读")]
    snapshot = echo_obj.buffer_snapshot()
    assert [entry.echo_key for entry in snapshot] == ["复读"] * 4
    assert snapshot[-1].user_openid == BOT


async def test_probability_miss_leaves_streak_without_bot_entry(rest):
    echo_obj = GroupEcho(rng=lambda: 0.9)

    results = await _feed(echo_obj, ["复读", "复读"])

    assert results == [False, False]
    assert rest.sent == []
    snapshot = echo_obj.buffer_snapshot()
    assert [entry.echo_key for entry in snapshot] == ["复读", "复读"]
    assert all(entry.user_openid != BOT for entry in snapshot)


async def test_bot_in_streak_prevents_duplicate_echo(rest):
    echo_obj = GroupEcho(rng=lambda: 0.0)

    await _feed(echo_obj, ["复读"] * 5)

    assert _sent_texts(rest) == [(GROUP, "复读")]
    snapshot = echo_obj.buffer_snapshot()
    assert [entry.user_openid for entry in snapshot].count(BOT) == 1


async def test_existing_bot_entry_in_user_streak_prevents_echo(rest):
    echo_obj = GroupEcho(rng=lambda: 0.0)

    results = await _feed(
        echo_obj,
        ["复读", "复读", "复读"],
        users=["USER_1", BOT, "USER_2"],
    )

    assert results == [False, False, False]
    assert rest.sent == []
    assert [e.user_openid for e in echo_obj.buffer_snapshot()] == ["USER_1", BOT, "USER_2"]


async def test_command_messages_break_streak_and_stay_in_buffer(rest):
    echo_obj = GroupEcho(rng=lambda: 0.0)

    await _feed(echo_obj, ["复读", "/submit 复读", "复读", "复读"])

    assert _sent_texts(rest) == [(GROUP, "复读")]
    snapshot = echo_obj.buffer_snapshot()
    assert [entry.echo_key for entry in snapshot] == [
        "复读", "/submit 复读", "复读", "复读", "复读",
    ]
    assert [entry.is_command for entry in snapshot] == [False, True, False, False, False]


async def test_messages_after_command_can_trigger_echo_again(rest):
    echo_obj = GroupEcho(rng=lambda: 0.0)

    await _feed(echo_obj, ["msg", "msg", "/cmd", "msg", "msg"])

    assert _sent_texts(rest) == [(GROUP, "msg"), (GROUP, "msg")]
    snapshot = echo_obj.buffer_snapshot()
    assert [entry.echo_key for entry in snapshot] == [
        "msg", "msg", "msg", "/cmd", "msg", "msg", "msg",
    ]
    assert [e.user_openid for e in snapshot].count(BOT) == 2


async def test_non_identical_messages_do_not_trigger(rest):
    echo_obj = GroupEcho(rng=lambda: 0.0)

    results = await _feed(echo_obj, ["a", "b", "a", "b", "c"])

    assert results == [False] * 5
    assert rest.sent == []


async def test_buffer_is_bounded(rest):
    echo_obj = GroupEcho(max_entries=5, trigger_count=99, rng=lambda: 0.0)

    await _feed(echo_obj, [f"msg {i}" for i in range(8)])

    assert rest.sent == []
    assert [entry.echo_key for entry in echo_obj.buffer_snapshot()] == [
        "msg 3", "msg 4", "msg 5", "msg 6", "msg 7",
    ]


async def test_trigger_count_three_requires_three_in_a_row(rest):
    echo_obj = GroupEcho(trigger_count=3, rng=lambda: 0.0)

    results = await _feed(echo_obj, ["复读", "复读", "复读"])

    assert results == [False, False, True]
    assert _sent_texts(rest) == [(GROUP, "复读")]


async def test_probability_comparison_is_strictly_less(rest):
    # rng() == echo_probability must NOT echo (condition is rng() < p).
    miss = GroupEcho(echo_probability=0.25, rng=lambda: 0.25)
    assert await _feed(miss, ["x", "x"]) == [False, False]
    assert rest.sent == []

    hit = GroupEcho(echo_probability=0.25, rng=lambda: 0.2499)
    assert await _feed(hit, ["x", "x"]) == [False, True]
    assert _sent_texts(rest) == [(GROUP, "x")]


async def test_empty_content_is_ignored_without_buffer_entry(rest):
    echo_obj = GroupEcho(rng=lambda: 0.0)

    assert await _feed(echo_obj, ["", "   "]) == [False, False]
    assert echo_obj.buffer_snapshot() == []
    assert rest.sent == []


def test_canonical_echo_key_nfc_and_strip():
    assert canonical_echo_key("  复读  ") == "复读"
    # NFC: decomposed e+acute equals composed é.
    assert canonical_echo_key("e\u0301") == canonical_echo_key("\u00e9") == "\u00e9"
    assert canonical_echo_key("") == ""
    assert canonical_echo_key(None) == ""


def test_canonical_echo_key_strips_leading_official_mentions():
    assert canonical_echo_key("<@!ABC123> 复读") == "复读"
    assert canonical_echo_key("<@!ABC123><@!DEF456> hi") == "hi"
    # Only leading hex-openid mentions are stripped; mid-text stays.
    assert canonical_echo_key("hi <@!ABC123>") == "hi <@!ABC123>"
    assert canonical_echo_key("<@!nothex> x") == "<@!nothex> x"


async def test_nfc_variants_join_one_streak(rest):
    echo_obj = GroupEcho(rng=lambda: 0.0)

    results = await _feed(echo_obj, ["e\u0301", "\u00e9"])

    assert results == [False, True]
    assert _sent_texts(rest) == [(GROUP, "\u00e9")]


async def test_mention_variant_joins_streak_and_echoes_original_content(rest):
    echo_obj = GroupEcho(rng=lambda: 0.0)

    results = await _feed(echo_obj, ["复读", "<@!AB12> 复读"])

    assert results == [False, True]
    # The bot repeats the triggering message's original content, not the key.
    assert _sent_texts(rest) == [(GROUP, "<@!AB12> 复读")]
    assert [e.echo_key for e in echo_obj.buffer_snapshot()] == ["复读"] * 3


async def test_module_level_check_and_echo_is_per_group(rest):
    echo._groups["GA"] = GroupEcho(rng=lambda: 0.0)
    echo._groups["GB"] = GroupEcho(rng=lambda: 0.0)

    async def _msg(group: str, i: int) -> bool:
        return await check_and_echo(
            group_openid=group, user_openid=f"U{i}", content="复读",
            raw_text="复读", message_id=f"{group}_m{i}",
        )

    assert await _msg("GA", 0) is False
    assert await _msg("GB", 0) is False  # independent buffer: streak only 1
    assert await _msg("GA", 1) is True
    assert await _msg("GB", 1) is True
    assert _sent_texts(rest) == [("GA", "复读"), ("GB", "复读")]


async def test_reset_echo_for_tests_clears_group_buffers():
    echo._groups["GA"] = GroupEcho(rng=lambda: 0.0)
    await check_and_echo(
        group_openid="GA", user_openid="U0", content="x",
        raw_text="x", message_id="m0",
    )
    assert echo._groups["GA"].buffer_snapshot()

    reset_echo_for_tests()
    assert echo._groups == {}
    # A fresh group gets a default-configured buffer.
    fresh = echo._group_echo("GA")
    assert fresh.buffer_snapshot() == []
    assert fresh.trigger_count == 2
    assert fresh.echo_probability == 0.25


async def test_process_event_hooks_echo_for_non_command_group_messages(rest):
    echo._groups[GROUP] = GroupEcho(rng=lambda: 0.0)

    def _event(i: int) -> dict:
        return {
            "type": "message",
            "message_type": "group",
            "group_openid": GROUP,
            "user_openid": f"USER_{i}",
            "message_id": f"MSG_{i}",
            "content": "复读",
            "author": {"nickname": "Alice"},
        }

    for i in range(2):
        assert await process_event(_event(i), spawn_handlers=False) is None

    assert _sent_texts(rest) == [(GROUP, "复读")]


async def test_process_event_command_messages_never_echo(rest, monkeypatch):
    monkeypatch.setattr(registry, "_registry", {})  # /whatever is unknown
    echo._groups[GROUP] = GroupEcho(rng=lambda: 0.0)

    def _event(i: int) -> dict:
        return {
            "type": "message",
            "message_type": "group",
            "group_openid": GROUP,
            "user_openid": f"USER_{i}",
            "message_id": f"MSG_{i}",
            "content": "/whatever 复读",
            "author": {"nickname": "Alice"},
        }

    for i in range(3):
        await process_event(_event(i), spawn_handlers=False)

    assert rest.sent == []
    snapshot = echo._groups[GROUP].buffer_snapshot()
    assert len(snapshot) == 3
    assert all(entry.is_command for entry in snapshot)