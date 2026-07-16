"""Tests for QQ notice-event handling."""

import asyncio
import os
import sys
from types import SimpleNamespace

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

    newproblem._cooldowns.clear()
    newproblem._newproblem_active.clear()
    newproblem._newproblem_locks.clear()
    return newproblem


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

    monkeypatch.setattr("kouhai_bot.handlers.notice.get_config", _cfg)
    monkeypatch.setattr("kouhai_bot.handlers.notice.send_group_poke", fake_poke)
    monkeypatch.setattr(newproblem, "_has_unsolved_problem", lambda _gid: False)
    monkeypatch.setattr(newproblem, "_post_new_problem_locked", fake_post)

    asyncio.run(process_event(_poke_event(), spawn_handlers=False))

    assert pokes == [(GROUP_ID, USER_ID)]
    assert posts == [(GROUP_ID, "戳一戳刷新🌟", True)]
    assert GROUP_ID in newproblem._cooldowns


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

    monkeypatch.setattr("kouhai_bot.handlers.notice.get_config", _cfg)
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

    monkeypatch.setattr("kouhai_bot.handlers.notice.get_config", _cfg)
    monkeypatch.setattr("kouhai_bot.handlers.notice.send_group_poke", fake_poke)
    monkeypatch.setattr(newproblem, "_has_unsolved_problem", lambda _gid: False)
    monkeypatch.setattr(newproblem, "_post_new_problem_locked", fake_post)
    monkeypatch.setattr("kouhai_bot.handlers.notice.time.monotonic", lambda: 200.0)

    asyncio.run(process_event(_poke_event(), spawn_handlers=False))

    assert pokes == [(GROUP_ID, USER_ID)]
    assert posts == []


def test_pokes_outside_service_group_or_not_targeting_bot_are_ignored(monkeypatch):
    _reset_newproblem_runtime()
    pokes = []

    async def fake_poke(group_id, user_id):
        pokes.append((group_id, user_id))
        return True

    monkeypatch.setattr("kouhai_bot.handlers.notice.get_config", _cfg)
    monkeypatch.setattr("kouhai_bot.handlers.notice.send_group_poke", fake_poke)

    asyncio.run(process_event(_poke_event(group_id=999), spawn_handlers=False))
    asyncio.run(process_event(_poke_event(target_id=999), spawn_handlers=False))

    assert pokes == []


def test_non_poke_notice_is_ignored(monkeypatch):
    _reset_newproblem_runtime()
    pokes = []

    async def fake_poke(group_id, user_id):
        pokes.append((group_id, user_id))
        return True

    monkeypatch.setattr("kouhai_bot.handlers.notice.get_config", _cfg)
    monkeypatch.setattr("kouhai_bot.handlers.notice.send_group_poke", fake_poke)

    asyncio.run(process_event(_poke_event(sub_type="lucky_king"), spawn_handlers=False))

    assert pokes == []
