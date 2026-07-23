"""Tests for QQ notice-event handling."""

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.handlers import process_event


GROUP_ID = 123
BOT_QQ = 789
USER_ID = 456


def _cfg(cooldown: int = 300) -> SimpleNamespace:
    return SimpleNamespace(
        bot_qq=BOT_QQ,
        current_group=GROUP_ID,
        newproblem_cooldown=cooldown,
    )


def _poke_event(**overrides) -> dict:
    event = {
        "type": "notice",
        "notice_type": "notify",
        "sub_type": "poke",
        "group_id": GROUP_ID,
        "user_id": USER_ID,
        "target_id": BOT_QQ,
        "raw": {},
    }
    event.update(overrides)
    return event


def _reset_newproblem_runtime():
    from kouhai_bot.handlers.cmd import newproblem
    from kouhai_bot.problem_prefetch import reset_prefetchers_for_tests

    newproblem._cooldowns.clear()
    newproblem._newproblem_active.clear()
    newproblem._newproblem_locks.clear()
    reset_prefetchers_for_tests()
    return newproblem


@pytest.fixture(autouse=True)
def _patch_config(monkeypatch):
    monkeypatch.setattr("kouhai_bot.handlers.notice.get_config", _cfg)
    monkeypatch.setattr("kouhai_bot.handlers.cmd.newproblem.get_config", _cfg)


def test_bot_poke_is_returned_and_posts_when_eligible(monkeypatch):
    newproblem = _reset_newproblem_runtime()
    pokes = []
    posts = []

    async def fake_poke(group_id, user_id):
        pokes.append((group_id, user_id))
        return True

    async def fake_post(group_id, *, prefix="", notify_group=False):
        posts.append((group_id, prefix, notify_group))
        return True

    monkeypatch.setattr("kouhai_bot.handlers.notice.send_group_poke", fake_poke)
    monkeypatch.setattr(newproblem, "_has_unsolved_problem", lambda _gid: False)
    monkeypatch.setattr(newproblem, "_post_new_problem_locked", fake_post)

    asyncio.run(process_event(_poke_event(), spawn_handlers=False))

    assert pokes == [(GROUP_ID, USER_ID)]
    assert posts == [(GROUP_ID, "戳一戳刷新🌟", False)]
    assert GROUP_ID in newproblem._cooldowns


def test_quiet_poke_picker_failure_sends_no_group_message(monkeypatch, tmp_path):
    newproblem = _reset_newproblem_runtime()
    group_messages = []

    cfg = SimpleNamespace(
        bot_qq=BOT_QQ,
        current_group=GROUP_ID,
        newproblem_cooldown=300,
        data_dir=str(tmp_path),
        min_rating=2000,
        max_rating=3000,
    )

    async def fake_poke(_group_id, _user_id):
        return True

    async def fake_send_group_msg(group_id, message):
        group_messages.append((group_id, message))
        return True

    from kouhai_bot.problem_preparation import ProblemPreparationError

    prefetcher = SimpleNamespace(
        claim=AsyncMock(
            side_effect=ProblemPreparationError("Codeforces 连接失败")
        ),
        release=AsyncMock(),
    )

    monkeypatch.setattr("kouhai_bot.handlers.notice.get_config", lambda: cfg)
    monkeypatch.setattr(newproblem, "get_config", lambda: cfg)
    monkeypatch.setattr("kouhai_bot.handlers.notice.send_group_poke", fake_poke)
    monkeypatch.setattr(newproblem, "_has_unsolved_problem", lambda _gid: False)
    monkeypatch.setattr(newproblem, "send_group_msg", fake_send_group_msg)
    monkeypatch.setattr(
        newproblem,
        "get_next_problem_prefetcher",
        lambda _group_id: prefetcher,
    )

    asyncio.run(process_event(_poke_event(), spawn_handlers=False))

    prefetcher.claim.assert_awaited_once()
    assert group_messages == []
    assert GROUP_ID not in newproblem._cooldowns


def test_command_post_exception_is_not_swallowed(monkeypatch):
    newproblem = _reset_newproblem_runtime()

    async def failing_post(*_args, **_kwargs):
        raise RuntimeError("posting failed")

    monkeypatch.setattr(newproblem, "_has_unsolved_problem", lambda _gid: False)
    monkeypatch.setattr(newproblem, "_post_new_problem_locked", failing_post)

    with pytest.raises(RuntimeError, match="posting failed"):
        asyncio.run(newproblem.enqueue_new_problem(
            GROUP_ID,
            USER_ID,
            {"nickname": "tester"},
            "message-id",
            command="newproblem --force",
        ))

    assert GROUP_ID not in newproblem._newproblem_active
    assert not newproblem._newproblem_lock(GROUP_ID).locked()
    assert GROUP_ID not in newproblem._cooldowns


def test_bot_poke_only_pokes_back_when_problem_is_unsolved(monkeypatch):
    newproblem = _reset_newproblem_runtime()
    pokes = []
    posts = []

    async def fake_poke(group_id, user_id):
        pokes.append((group_id, user_id))
        return True

    async def fake_post(*args, **kwargs):
        posts.append((args, kwargs))
        return True

    monkeypatch.setattr("kouhai_bot.handlers.notice.send_group_poke", fake_poke)
    monkeypatch.setattr(newproblem, "_has_unsolved_problem", lambda _gid: True)
    monkeypatch.setattr(newproblem, "_post_new_problem_locked", fake_post)

    asyncio.run(process_event(_poke_event(), spawn_handlers=False))

    assert pokes == [(GROUP_ID, USER_ID)]
    assert posts == []


def test_bot_poke_only_pokes_back_during_cooldown(monkeypatch):
    newproblem = _reset_newproblem_runtime()
    pokes = []
    posts = []
    newproblem._cooldowns[GROUP_ID] = 100.0

    async def fake_poke(group_id, user_id):
        pokes.append((group_id, user_id))
        return True

    async def fake_post(*args, **kwargs):
        posts.append((args, kwargs))
        return True

    monkeypatch.setattr("kouhai_bot.handlers.notice.send_group_poke", fake_poke)
    monkeypatch.setattr(newproblem, "_has_unsolved_problem", lambda _gid: False)
    monkeypatch.setattr(newproblem, "_post_new_problem_locked", fake_post)
    monkeypatch.setattr("kouhai_bot.handlers.cmd.newproblem.time.monotonic", lambda: 200.0)

    asyncio.run(process_event(_poke_event(), spawn_handlers=False))

    assert pokes == [(GROUP_ID, USER_ID)]
    assert posts == []


def test_pokes_outside_service_group_or_not_targeting_bot_are_ignored(monkeypatch):
    _reset_newproblem_runtime()
    pokes = []

    async def fake_poke(group_id, user_id):
        pokes.append((group_id, user_id))
        return True

    monkeypatch.setattr("kouhai_bot.handlers.notice.send_group_poke", fake_poke)

    asyncio.run(process_event(_poke_event(group_id=999), spawn_handlers=False))
    asyncio.run(process_event(_poke_event(target_id=999), spawn_handlers=False))

    assert pokes == []


def test_invalid_and_self_pokes_are_ignored(monkeypatch):
    _reset_newproblem_runtime()
    pokes = []

    async def fake_poke(group_id, user_id):
        pokes.append((group_id, user_id))
        return True

    monkeypatch.setattr("kouhai_bot.handlers.notice.send_group_poke", fake_poke)

    missing_user = _poke_event()
    missing_user.pop("user_id")
    asyncio.run(process_event(missing_user, spawn_handlers=False))
    asyncio.run(process_event(_poke_event(user_id=None), spawn_handlers=False))
    asyncio.run(process_event(_poke_event(user_id="invalid"), spawn_handlers=False))
    asyncio.run(process_event(_poke_event(user_id=True), spawn_handlers=False))
    asyncio.run(process_event(_poke_event(user_id=1.5), spawn_handlers=False))
    asyncio.run(process_event(_poke_event(user_id=0), spawn_handlers=False))
    asyncio.run(process_event(_poke_event(user_id=-1), spawn_handlers=False))
    asyncio.run(process_event(_poke_event(user_id=BOT_QQ), spawn_handlers=False))

    assert pokes == []


def test_poke_post_failure_does_not_escape_detached_task(monkeypatch, caplog):
    newproblem = _reset_newproblem_runtime()

    async def fake_poke(_group_id, _user_id):
        return True

    async def failing_post(*_args, **_kwargs):
        raise RuntimeError("posting failed")

    async def run():
        task = await process_event(_poke_event(), spawn_handlers=True)
        assert task is not None
        await task

    monkeypatch.setattr("kouhai_bot.handlers.notice.send_group_poke", fake_poke)
    monkeypatch.setattr(newproblem, "_has_unsolved_problem", lambda _gid: False)
    monkeypatch.setattr(newproblem, "_post_new_problem_locked", failing_post)

    asyncio.run(run())

    assert "poke post failed" in caplog.text
    assert GROUP_ID not in newproblem._newproblem_active
    assert not newproblem._newproblem_lock(GROUP_ID).locked()
    assert GROUP_ID not in newproblem._cooldowns


def test_concurrent_pokes_start_only_one_refresh(monkeypatch):
    newproblem = _reset_newproblem_runtime()
    pokes = []
    posts = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_poke(group_id, user_id):
        pokes.append((group_id, user_id))
        return True

    async def fake_post(group_id, **_kwargs):
        posts.append(group_id)
        started.set()
        await release.wait()
        return True

    async def run():
        first = asyncio.create_task(process_event(_poke_event(), spawn_handlers=False))
        await asyncio.wait_for(started.wait(), timeout=1.0)
        await process_event(_poke_event(user_id=USER_ID + 1), spawn_handlers=False)
        release.set()
        await asyncio.wait_for(first, timeout=1.0)

    monkeypatch.setattr("kouhai_bot.handlers.notice.send_group_poke", fake_poke)
    monkeypatch.setattr(newproblem, "_has_unsolved_problem", lambda _gid: False)
    monkeypatch.setattr(newproblem, "_post_new_problem_locked", fake_post)

    asyncio.run(run())

    assert pokes == [(GROUP_ID, USER_ID), (GROUP_ID, USER_ID + 1)]
    assert posts == [GROUP_ID]


def test_non_poke_notice_is_ignored(monkeypatch):
    _reset_newproblem_runtime()
    pokes = []

    async def fake_poke(group_id, user_id):
        pokes.append((group_id, user_id))
        return True

    monkeypatch.setattr("kouhai_bot.handlers.notice.send_group_poke", fake_poke)

    asyncio.run(process_event(_poke_event(sub_type="lucky_king"), spawn_handlers=False))

    assert pokes == []
