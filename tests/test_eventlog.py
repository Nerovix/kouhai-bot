"""Tests for official-native group event dispatch (process_event/dispatch).

The new dispatcher is group-only and multi-group: it accepts normalized
official events (group_openid/user_openid/message_id/content/author), ignores
C2C/private/notice events, canonicalizes alias/case/leading-junk command text,
enforces per-(group, user, command) cooldowns with silent drops, and either
spawns the handler as a task or awaits it inline.
"""

import asyncio

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
    mid: str = "MSG_1",
    nickname: str = "Alice",
    message_type: str = "group",
    type_: str = "message",
) -> dict:
    return {
        "type": type_,
        "message_type": message_type,
        "group_openid": group,
        "user_openid": user,
        "message_id": mid,
        "content": content,
        "author": {"nickname": nickname},
    }


def _register(name, handler, *, aliases=(), cooldown=0):
    registry.register(CommandDef(
        name=name, aliases=list(aliases), description=name,
        handler=handler, cooldown=cooldown,
    ))


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
    await process_event(_event("/DISPATCHCASE hello"), spawn_handlers=False)
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