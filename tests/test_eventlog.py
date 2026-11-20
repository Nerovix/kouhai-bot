"""Tests for official-native group event dispatch (process_event/dispatch).

The new dispatcher is group-only and multi-group: it accepts normalized
official events (group_openid/user_openid/message_id/content/author), ignores
C2C/private/notice events, requires an @bot (``at_bot`` or ``mentions`` is_you)
before a command triggers, canonicalizes alias/case/leading-junk command text,
enforces per-(group, user, command) cooldowns with silent drops, and either
spawns the handler as a task or awaits it inline.
"""

import asyncio
import itertools

import pytest

from kouhai_bot.db import Database
from kouhai_bot.handlers import (
    _normalize_leading_command_junk,
    dispatch,
    process_event,
)
from kouhai_bot.handlers import registry
from kouhai_bot.handlers.context import Ctx
from kouhai_bot.handlers.registry import CommandDef
from kouhai_bot.handlers.runtime import Runtime, set_runtime
from kouhai_bot.official import OfficialSender, ReplyBudget

GROUP = "GROUP_A"
USER = "USER_1"

# Each call to _event() represents a distinct gateway message, so default to a
# fresh unique message_id (the dispatcher dedups repeats by id).
_msg_seq = itertools.count(1)


class FakeRest:
    def __init__(self):
        self.sent = []
        self.uploads = []
        self.acks = []

    async def send_group_message(self, group_openid, payload):
        self.sent.append((group_openid, payload))
        return {"id": f"M{len(self.sent)}"}

    async def upload_group_media(self, group_openid, payload):
        self.uploads.append((group_openid, payload))
        return {"file_info": "FI", "file_uuid": "u", "ttl": 300}

    async def respond_interaction(self, interaction_id, *, code=0):
        self.acks.append((interaction_id, code))
        return True


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    monkeypatch.setattr(registry, "_registry", {})


@pytest.fixture
def rest():
    return FakeRest()


@pytest.fixture(autouse=True)
async def runtime(db: Database, rest: FakeRest):
    sender = OfficialSender(rest, ReplyBudget(db))
    rt = Runtime(db=db, sender=sender, browser=None)
    set_runtime(rt)
    return rt


def _event(
    content: str = "/dispatchtest hello",
    *,
    group: str = GROUP,
    user: str = USER,
    mid: str | None = None,
    nickname: str = "Alice",
    message_type: str = "group",
    type_: str = "message",
    mentions: list | None = None,
    at_bot: bool = False,
) -> dict:
    # Commands require an @bot. Default to the full-message/chip form: a
    # structured ``mentions`` entry marking us (``is_you``), ``at_bot`` false.
    # Placeholder openid only — never real ids. Override ``mentions``/``at_bot``
    # to exercise un-addressed or @-message (GROUP_AT_MESSAGE_CREATE) forms.
    if mentions is None:
        mentions = [{"member_openid": "BOT_PLACEHOLDER", "bot": True, "is_you": True}]
    return {
        "type": type_,
        "message_type": message_type,
        "group_openid": group,
        "user_openid": user,
        "message_id": mid if mid is not None else f"MSG_{next(_msg_seq)}",
        "content": content,
        "author": {
            "member_openid": user,
            "username": nickname,
            "member_nickname": nickname,
        },
        "mentions": mentions,
        "at_bot": at_bot,
    }


def _register(name, handler, *, aliases=(), cooldown=0):
    registry.register(CommandDef(
        name=name, aliases=list(aliases), description=name,
        handler=handler, cooldown=cooldown,
    ))


def _interaction(
    button_data: str = "/dispatchtest hello",
    *,
    group: str = GROUP,
    user: str = USER,
    iid: str | None = None,
    interaction_type: int = 11,
    scene: str = "group",
) -> dict:
    # Normalized INTERACTION_CREATE (inline-keyboard callback). Placeholder
    # openids only — never real ids. button_data is the command the button carries.
    return {
        "type": "interaction",
        "group_openid": group,
        "user_openid": user,
        "interaction_id": iid if iid is not None else f"INT_{next(_msg_seq)}",
        "interaction_type": interaction_type,
        "scene": scene,
        "button_data": button_data,
        "button_id": "btn",
        "data": {"type": interaction_type,
                 "resolved": {"button_data": button_data, "button_id": "btn"}},
        "raw": {},
    }


async def test_process_event_spawns_handler_task():
    seen: list[str] = []

    async def _handler(ctx: Ctx):
        await asyncio.sleep(0)
        seen.append(ctx.raw_text)

    _register("dispatchtest", _handler)
    task = await process_event(_event())
    assert isinstance(task, asyncio.Task)
    assert seen == []  # receive loop did not wait for the handler
    await task
    assert seen == ["/dispatchtest hello"]


async def test_dispatch_gateway_path_does_not_block():
    seen: list[str] = []

    async def _handler(ctx: Ctx):
        await asyncio.sleep(0.02)
        seen.append(ctx.raw_text)

    _register("dispatchtest", _handler)
    await dispatch(_event())
    assert seen == []  # handler still running after dispatch returned
    await asyncio.sleep(0.05)
    assert seen == ["/dispatchtest hello"]


async def test_spawn_handlers_false_awaits_completion():
    seen: list[str] = []

    async def _handler(ctx: Ctx):
        await asyncio.sleep(0)
        seen.append(ctx.raw_text)

    _register("dispatchsync", _handler)
    result = await process_event(_event("/dispatchsync hello"), spawn_handlers=False)
    assert result is None
    assert seen == ["/dispatchsync hello"]


async def test_alias_canonicalized_before_handler():
    seen: list[tuple[str, str]] = []

    async def _handler(ctx: Ctx):
        seen.append((ctx.raw_text, ctx.args))

    _register("dispatchalias", _handler, aliases=["da"])
    await process_event(_event("/da hello world"), spawn_handlers=False)
    assert seen == [("/dispatchalias hello world", "hello world")]


async def test_command_case_canonicalized_before_handler():
    seen: list[str] = []

    async def _handler(ctx: Ctx):
        seen.append(ctx.raw_text)

    _register("dispatchcase", _handler, aliases=["dc"])
    await process_event(_event("/DC hello"), spawn_handlers=False)
    await process_event(_event("/DC hi", user="USER_2"), spawn_handlers=False)
    assert seen == ["/dispatchcase hello", "/dispatchcase hi"]


async def test_bare_alias_without_args():
    seen: list[tuple[str, str]] = []

    async def _handler(ctx: Ctx):
        seen.append((ctx.raw_text, ctx.args))

    _register("dispatchalias", _handler, aliases=["da"])
    await process_event(_event("/da"), spawn_handlers=False)
    assert seen == [("/dispatchalias", "")]


async def test_leading_variation_selector_junk_stripped_before_command():
    seen: list[str] = []

    async def _handler(ctx: Ctx):
        seen.append(ctx.raw_text)

    _register("dispatchvs", _handler)
    await process_event(_event("\ufe0f/dispatchvs hello"), spawn_handlers=False)
    await process_event(
        _event("\u200b\ufe0f/dispatchvs again", user="USER_2"),
        spawn_handlers=False,
    )
    assert seen == ["/dispatchvs hello", "/dispatchvs again"]


async def test_leading_junk_normalizer_only_touches_slash_commands():
    assert _normalize_leading_command_junk("\ufe0f/review x") == "/review x"
    assert _normalize_leading_command_junk("\u200b\ufe0f/np") == "/np"
    # Junk before ordinary text is preserved.
    assert _normalize_leading_command_junk("\ufe0fhello") == "\ufe0fhello"
    assert _normalize_leading_command_junk("plain text") == "plain text"
    # Junk-only text has no visible slash → unchanged.
    assert _normalize_leading_command_junk("\ufe0f") == "\ufe0f"


async def test_leading_bot_mention_stripped_before_command():
    seen: list[tuple[str, str]] = []

    async def _handler(ctx: Ctx):
        seen.append((ctx.raw_text, ctx.args))

    _register("dispatchat", _handler, aliases=["dat"])
    # Quoting a bot card auto-inserts @bot (official form has no '!'); the
    # command must still dispatch. Placeholder openid only — never real ids.
    await process_event(
        _event("<@ABCDEF0123456789> /dat hello"), spawn_handlers=False
    )
    await process_event(
        _event("<@ABCDEF0123456789> /dat hi", user="USER_2"), spawn_handlers=False
    )
    # Legacy '!' form is tolerated too.
    await process_event(
        _event("<@!ABCDEF0123456789> /dat x", user="USER_3"),
        spawn_handlers=False,
    )
    assert seen == [
        ("/dispatchat hello", "hello"),
        ("/dispatchat hi", "hi"),
        ("/dispatchat x", "x"),
    ]


async def test_command_addressed_to_other_bot_ignored():
    seen: list[str] = []

    async def _handler(ctx: Ctx):
        seen.append(ctx.raw_text)

    _register("dispatchat", _handler, aliases=["dat"])
    # Placeholder hex openids only — never real ids.
    other_bot = {"member_openid": "BBBBBBBBBBBBBBBB", "bot": True, "is_you": False}
    us = {"member_openid": "AAAAAAAAAAAAAAAA", "bot": True, "is_you": True}
    human = {"member_openid": "CCCCCCCCCCCCCCCC", "bot": False}

    # @ another bot only → not addressed to us; ignore entirely.
    event = _event("<@BBBBBBBBBBBBBBBB> /dat hello", mentions=["junk", other_bot])
    assert await process_event(event, spawn_handlers=False) is None
    assert seen == []

    # @ us → dispatch.
    await process_event(
        _event("<@AAAAAAAAAAAAAAAA> /dat hi", user="USER_2", mentions=[us]),
        spawn_handlers=False,
    )

    # @ us AND another bot → we were addressed too; dispatch.
    await process_event(
        _event(
            "<@BBBBBBBBBBBBBBBB> <@AAAAAAAAAAAAAAAA> /dat yo",
            user="USER_3", mentions=[other_bot, us],
        ),
        spawn_handlers=False,
    )

    # @ a human AND us → the human mention does not block us; dispatch.
    await process_event(
        _event(
            "<@CCCCCCCCCCCCCCCC> <@AAAAAAAAAAAAAAAA> /dat hey",
            user="USER_4", mentions=[human, us],
        ),
        spawn_handlers=False,
    )

    # @ a human only (not us) → require-@ gate drops it.
    event = _event("<@CCCCCCCCCCCCCCCC> /dat nope", user="USER_5", mentions=[human])
    assert await process_event(event, spawn_handlers=False) is None

    assert seen == ["/dispatchat hi", "/dispatchat yo", "/dispatchat hey"]


async def test_commands_require_at_bot():
    seen: list[str] = []

    async def _handler(ctx: Ctx):
        seen.append(ctx.raw_text)

    _register("dispatchgate", _handler, aliases=["dg"])
    us = {"member_openid": "AAAAAAAAAAAAAAAA", "bot": True, "is_you": True}

    # Bare command, no @bot → ignored.
    assert await process_event(
        _event("/dg bare", mentions=[]), spawn_handlers=False
    ) is None

    # @-message form (GROUP_AT_MESSAGE_CREATE → at_bot), no structured mentions.
    await process_event(
        _event("/dg atmsg", user="USER_2", mentions=[], at_bot=True),
        spawn_handlers=False,
    )

    # Full-message/chip form: mentions marks us, at_bot false.
    await process_event(
        _event("/dg chip", user="USER_3", mentions=[us]),
        spawn_handlers=False,
    )

    # Quoting a bot card auto-inserts @bot markup + is_you mention → dispatch.
    await process_event(
        _event("<@AAAAAAAAAAAAAAAA> /dg quoted", user="USER_4", mentions=[us]),
        spawn_handlers=False,
    )

    # mentions present but none is us → ignored.
    assert await process_event(
        _event("/dg other", user="USER_5",
               mentions=[{"member_openid": "BBBBBBBBBBBBBBBB", "bot": True, "is_you": False}]),
        spawn_handlers=False,
    ) is None

    assert seen == ["/dispatchgate atmsg", "/dispatchgate chip", "/dispatchgate quoted"]


async def test_ctx_carries_event_identity():
    captured: list[Ctx] = []

    async def _handler(ctx: Ctx):
        captured.append(ctx)

    _register("dispatchctx", _handler)
    event = _event("/dispatchctx  arg text  ", mid="MSG_42", nickname="Bob")
    await process_event(event, spawn_handlers=False)
    assert len(captured) == 1
    ctx = captured[0]
    assert ctx.group_openid == GROUP
    assert ctx.user_openid == USER
    assert ctx.message_id == "MSG_42"
    assert ctx.nickname == "Bob"
    assert ctx.args == "arg text"
    assert ctx.event is event


async def test_cooldown_silently_drops_same_group_user_command():
    seen: list[str] = []

    async def _handler(ctx: Ctx):
        seen.append(ctx.user_openid)

    _register("dispatchcd", _handler, cooldown=60)
    first = await process_event(_event("/dispatchcd x"), spawn_handlers=False)
    second = await process_event(_event("/dispatchcd y"), spawn_handlers=False)
    assert first is None and second is None  # silent drop, no reply
    assert seen == [USER]

    # Cooldown is scoped per (group, user, command): other users, other
    # groups, and other commands are unaffected.
    await process_event(_event("/dispatchcd x", user="USER_2"), spawn_handlers=False)
    await process_event(_event("/dispatchcd x", group="GROUP_B"), spawn_handlers=False)
    _register("dispatchother", _handler, cooldown=60)
    await process_event(_event("/dispatchother x"), spawn_handlers=False)
    assert seen == [USER, "USER_2", USER, USER]


async def test_zero_cooldown_never_drops():
    seen: list[str] = []

    async def _handler(ctx: Ctx):
        seen.append(ctx.raw_text)

    _register("dispatchfree", _handler, cooldown=0)
    for _ in range(3):
        await process_event(_event("/dispatchfree x"), spawn_handlers=False)
    assert len(seen) == 3


async def test_cooldown_expires(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("kouhai_bot.handlers.time.monotonic", lambda: clock[0])
    seen: list[str] = []

    async def _handler(ctx: Ctx):
        seen.append(ctx.raw_text)

    _register("dispatchexp", _handler, cooldown=60)
    await process_event(_event("/dispatchexp a"), spawn_handlers=False)
    clock[0] = 1030.0
    await process_event(_event("/dispatchexp b"), spawn_handlers=False)
    assert seen == ["/dispatchexp a"]  # still within cooldown
    clock[0] = 1060.0
    await process_event(_event("/dispatchexp c"), spawn_handlers=False)
    assert seen == ["/dispatchexp a", "/dispatchexp c"]


async def test_private_c2c_and_notice_events_ignored():
    seen: list[str] = []

    async def _handler(ctx: Ctx):
        seen.append(ctx.raw_text)

    _register("dispatchtest", _handler)
    for kwargs in (
        {"message_type": "private"},
        {"message_type": "c2c"},
        {"type_": "notice"},
        {"type_": "request"},
    ):
        assert await process_event(_event(**kwargs), spawn_handlers=False) is None
    assert seen == []


async def test_missing_identity_or_empty_content_ignored():
    seen: list[str] = []

    async def _handler(ctx: Ctx):
        seen.append(ctx.raw_text)

    _register("dispatchtest", _handler)
    assert await process_event(_event(group=""), spawn_handlers=False) is None
    assert await process_event(_event(user=""), spawn_handlers=False) is None
    assert await process_event(_event("   "), spawn_handlers=False) is None
    assert seen == []


async def test_unknown_command_and_plain_text_not_dispatched():
    seen: list[str] = []

    async def _handler(ctx: Ctx):
        seen.append(ctx.raw_text)

    _register("dispatchtest", _handler)
    assert await process_event(_event("/nosuchcmd x"), spawn_handlers=False) is None
    assert await process_event(_event("hello everyone"), spawn_handlers=False) is None
    assert seen == []


async def test_any_group_is_served():
    seen: list[tuple[str, str]] = []

    async def _handler(ctx: Ctx):
        seen.append((ctx.group_openid, ctx.raw_text))

    _register("dispatchmulti", _handler)
    await process_event(_event("/dispatchmulti a", group="GROUP_X"), spawn_handlers=False)
    await process_event(_event("/dispatchmulti b", group="GROUP_Y"), spawn_handlers=False)
    assert seen == [("GROUP_X", "/dispatchmulti a"), ("GROUP_Y", "/dispatchmulti b")]


async def test_handler_exceptions_are_swallowed():
    async def _boom(ctx: Ctx):
        raise ValueError("boom")

    _register("dispatchboom", _boom)
    # Inline path must not propagate.
    await process_event(_event("/dispatchboom x"), spawn_handlers=False)
    # Spawned path must not surface as a task exception either.
    task = await process_event(_event("/dispatchboom y", user="USER_2"))
    await task
    assert task.exception() is None


async def test_duplicate_message_id_dispatched_once():
    seen: list[str] = []

    async def _handler(ctx: Ctx):
        seen.append(ctx.raw_text)

    _register("dispatchdup", _handler)
    # The gateway may redeliver the same event; the same message_id must only
    # run the handler once even across separate process_event calls.
    await process_event(_event("/dispatchdup x", mid="DUP_1"), spawn_handlers=False)
    await process_event(_event("/dispatchdup x", mid="DUP_1"), spawn_handlers=False)
    assert seen == ["/dispatchdup x"]
    # A different message_id is a distinct message and dispatches normally.
    await process_event(_event("/dispatchdup y", mid="DUP_2"), spawn_handlers=False)
    assert seen == ["/dispatchdup x", "/dispatchdup y"]


async def test_duplicate_drop_does_not_consume_cooldown():
    seen: list[str] = []

    async def _handler(ctx: Ctx):
        seen.append(ctx.raw_text)

    _register("dispatchdupcd", _handler, cooldown=60)
    await process_event(_event("/dispatchdupcd a", mid="DUP_3"), spawn_handlers=False)
    # Redelivery of the same id is dropped before cooldown is even consulted.
    await process_event(_event("/dispatchdupcd a", mid="DUP_3"), spawn_handlers=False)
    assert seen == ["/dispatchdupcd a"]


# ── inline-keyboard callback (INTERACTION_CREATE) ≡ typed command ─────────


async def test_interaction_callback_dispatches_and_acks(rest):
    seen: list[tuple[str, str]] = []

    async def _handler(ctx: Ctx):
        seen.append((ctx.raw_text, ctx.args))

    _register("dispatchint", _handler)
    ev = _interaction("/dispatchint hello world", iid="INT_A")
    await process_event(ev, spawn_handlers=False)
    # The callback runs the command exactly like a typed @bot message.
    assert seen == [("/dispatchint hello world", "hello world")]
    # And the interaction is acked (PUT code=0) so the clicker stops loading.
    assert rest.acks == [("INT_A", 0)]


async def test_interaction_callback_needs_no_at_mention():
    seen: list[str] = []

    async def _handler(ctx: Ctx):
        seen.append(ctx.raw_text)

    _register("dispatchintat", _handler)
    # Synthesized callback carries at_bot=True and no mentions; the require-@
    # gate must still let it through (a button click is inherently addressed).
    await process_event(_interaction("/dispatchintat x"), spawn_handlers=False)
    assert seen == ["/dispatchintat x"]


async def test_interaction_callback_alias_canonicalized():
    seen: list[tuple[str, str]] = []

    async def _handler(ctx: Ctx):
        seen.append((ctx.raw_text, ctx.args))

    _register("dispatchintalias", _handler, aliases=["dia"])
    await process_event(_interaction("/dia hello"), spawn_handlers=False)
    assert seen == [("/dispatchintalias hello", "hello")]


async def test_interaction_callback_shares_typed_cooldown(rest):
    seen: list[str] = []

    async def _handler(ctx: Ctx):
        seen.append(ctx.raw_text)

    _register("dispatchintcd", _handler, cooldown=60)
    # Typed command first, then a button callback for the same command: the
    # callback must be dropped by the SAME per-(group,user,command) cooldown.
    await process_event(_event("/dispatchintcd a"), spawn_handlers=False)
    await process_event(_interaction("/dispatchintcd b"), spawn_handlers=False)
    assert seen == ["/dispatchintcd a"]
    # The callback is still acked even though the command was cooldown-dropped.
    assert len(rest.acks) == 1


async def test_interaction_callback_cooldown_between_clicks():
    seen: list[str] = []

    async def _handler(ctx: Ctx):
        seen.append(ctx.raw_text)

    _register("dispatchintcd2", _handler, cooldown=60)
    await process_event(_interaction("/dispatchintcd2 a"), spawn_handlers=False)
    await process_event(_interaction("/dispatchintcd2 b"), spawn_handlers=False)
    assert seen == ["/dispatchintcd2 a"]


async def test_interaction_callback_zero_cooldown_repeats():
    seen: list[str] = []

    async def _handler(ctx: Ctx):
        seen.append(ctx.raw_text)

    _register("dispatchintfree", _handler, cooldown=0)
    for _ in range(3):
        await process_event(_interaction("/dispatchintfree x"), spawn_handlers=False)
    assert len(seen) == 3


async def test_interaction_dedup_by_interaction_id(rest):
    seen: list[str] = []

    async def _handler(ctx: Ctx):
        seen.append(ctx.raw_text)

    _register("dispatchintdup", _handler)
    await process_event(_interaction("/dispatchintdup x", iid="INT_DUP"), spawn_handlers=False)
    await process_event(_interaction("/dispatchintdup x", iid="INT_DUP"), spawn_handlers=False)
    assert seen == ["/dispatchintdup x"]
    assert rest.acks == [("INT_DUP", 0)]  # redelivery neither runs nor re-acks


async def test_interaction_non_keyboard_type_ignored(rest):
    seen: list[str] = []

    async def _handler(ctx: Ctx):
        seen.append(ctx.raw_text)

    _register("dispatchint", _handler)
    # type=12 (menu) and other interaction types are not inline-keyboard clicks.
    assert await process_event(
        _interaction("/dispatchint x", interaction_type=12), spawn_handlers=False
    ) is None
    assert seen == []
    assert rest.acks == []


async def test_interaction_non_group_scene_ignored(rest):
    seen: list[str] = []

    async def _handler(ctx: Ctx):
        seen.append(ctx.raw_text)

    _register("dispatchint", _handler)
    assert await process_event(
        _interaction("/dispatchint x", scene="c2c"), spawn_handlers=False
    ) is None
    assert seen == []
    assert rest.acks == []


async def test_interaction_non_slash_button_data_ignored(rest):
    seen: list[str] = []

    async def _handler(ctx: Ctx):
        seen.append(ctx.raw_text)

    _register("dispatchint", _handler)
    assert await process_event(
        _interaction("just text"), spawn_handlers=False
    ) is None
    assert seen == []
    assert rest.acks == []


async def test_interaction_missing_identity_ignored(rest):
    seen: list[str] = []

    async def _handler(ctx: Ctx):
        seen.append(ctx.raw_text)

    _register("dispatchint", _handler)
    assert await process_event(_interaction("/dispatchint x", group=""), spawn_handlers=False) is None
    assert await process_event(_interaction("/dispatchint x", user=""), spawn_handlers=False) is None
    assert seen == []
    assert rest.acks == []