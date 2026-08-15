"""Tests for editorial prefetch exponential backoff and private delivery."""

import asyncio
import os
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import kouhai_bot.editorial_followup as ef

PID = "1000A"


def _reset():
    ef._prefetch_attempts.pop(PID, None)
    ef._prefetch_last_attempt_at.pop(PID, None)
    ef._prefetch_tasks.pop(PID, None)


def test_backoff_starts_allowed():
    _reset()
    assert ef._prefetch_backoff_remaining(PID) == 0.0


def test_backoff_after_failure_and_reset_on_success():
    _reset()
    ef._mark_prefetch_failed(PID)
    remaining = ef._prefetch_backoff_remaining(PID)
    assert 0.0 < remaining <= ef._PREFETCH_BACKOFF_BASE_SEC

    ef._mark_prefetch_succeeded(PID)
    assert ef._prefetch_backoff_remaining(PID) == 0.0
    assert ef._prefetch_attempts.get(PID) is None


def test_backoff_grows_exponentially():
    _reset()
    expected = [60.0, 120.0, 240.0, 480.0]
    for idx, want in enumerate(expected):
        ef._mark_prefetch_failed(PID)
        remaining = ef._prefetch_backoff_remaining(PID)
        assert want - 1.0 < remaining <= want, f"attempt {idx}: {remaining}"


def test_backoff_caps_at_max():
    _reset()
    ef._prefetch_attempts[PID] = 100
    ef._prefetch_last_attempt_at[PID] = time.monotonic()
    remaining = ef._prefetch_backoff_remaining(PID)
    assert ef._PREFETCH_BACKOFF_MAX_SEC - 1.0 < remaining <= ef._PREFETCH_BACKOFF_MAX_SEC


def test_schedule_skips_during_backoff(monkeypatch):
    _reset()
    monkeypatch.setattr(ef, "_prefetch_needed", lambda pid: True)

    def boom(*_args, **_kwargs):
        raise AssertionError("create_task must not run during backoff")

    monkeypatch.setattr(ef.asyncio, "create_task", boom)
    ef._mark_prefetch_failed(PID)
    ef.schedule_prefetch_editorial(PID)  # must not raise


def test_schedule_creates_task_when_allowed(monkeypatch):
    _reset()
    monkeypatch.setattr(ef, "_prefetch_needed", lambda pid: True)
    calls = []

    def fake_create_task(coro, *, name=None):
        calls.append(name)
        return asyncio.Future()

    monkeypatch.setattr(ef.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(ef, "_track_task", lambda task: None)

    async def _run():
        ef.schedule_prefetch_editorial(PID)
        await asyncio.sleep(0)

    asyncio.run(_run())
    assert len(calls) == 1
    assert PID in calls[0]


# ---------------------------------------------------------------------------
# Private post-solve editorial delivery
# ---------------------------------------------------------------------------

_FAKE_EDITORIAL = SimpleNamespace(tutorial_url="https://codeforces.com/blog/entry/1")
_LONG_ZH = "这是官方题解的中文翻译。" * 100


def _mock_deliver_prereqs(monkeypatch):
    """Common mocks for deliver tests: verified editorial + self-send ids."""
    monkeypatch.setattr(ef, "get_verified_official_editorial", lambda pid: _FAKE_EDITORIAL)
    monkeypatch.setattr(ef, "load_cached_editorial_zh", lambda pid: _LONG_ZH)
    monkeypatch.setattr(ef, "get_config", lambda: SimpleNamespace(bot_qq=999))
    counter = {"n": 100}

    async def fake_self_send(_user_id, _message):
        counter["n"] += 1
        return counter["n"]

    monkeypatch.setattr(ef, "send_private_msg", fake_self_send)
    return counter


@pytest.mark.asyncio
async def test_private_deliver_forwards_to_user(monkeypatch):
    _mock_deliver_prereqs(monkeypatch)
    forwarded = []

    async def fake_fwd(user_id, messages):
        forwarded.append((user_id, messages))
        return 1

    monkeypatch.setattr(ef, "send_private_forward_msg", fake_fwd)

    await ef.deliver_official_tutorial_forward_private(12345, PID, _FAKE_EDITORIAL)
    assert len(forwarded) == 1
    user_id, messages = forwarded[0]
    assert user_id == 12345
    assert len(messages) >= 1
    assert messages[0]["type"] == "node"
    assert messages[0]["data"]["id"]


@pytest.mark.asyncio
async def test_private_deliver_skips_when_unverified(monkeypatch):
    monkeypatch.setattr(ef, "get_verified_official_editorial", lambda pid: None)
    monkeypatch.setattr(ef, "send_private_forward_msg", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not forward")))
    # must not raise: unverified editorial is silently skipped
    await ef.deliver_official_tutorial_forward_private(12345, PID, _FAKE_EDITORIAL)


@pytest.mark.asyncio
async def test_group_deliver_still_uses_group_forward(monkeypatch):
    _mock_deliver_prereqs(monkeypatch)
    forwarded = []

    async def fake_group_fwd(group_id, messages):
        forwarded.append((group_id, messages))
        return 1

    monkeypatch.setattr(ef, "send_group_forward_msg", fake_group_fwd)

    await ef.deliver_official_tutorial_forward(777, PID, _FAKE_EDITORIAL)
    assert len(forwarded) == 1
    assert forwarded[0][0] == 777


def test_private_schedule_creates_task(monkeypatch):
    calls = []

    def fake_create_task(coro, *, name=None):
        calls.append(name)
        return asyncio.Future()

    monkeypatch.setattr(ef.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(ef, "_track_task", lambda task: None)

    async def _run():
        ef.schedule_private_post_solve_editorial_followup(12345, PID)
        await asyncio.sleep(0)

    asyncio.run(_run())
    assert len(calls) == 1
    assert "private_editorial_deliver_12345_" in calls[0]


@pytest.mark.asyncio
async def test_private_run_skips_when_no_editorial(monkeypatch):
    monkeypatch.setattr(ef, "get_verified_official_editorial", lambda pid: None)
    monkeypatch.setattr(ef, "is_no_official_editorial", lambda pid: True)

    def boom(*_args, **_kwargs):
        raise AssertionError("must not forward when no editorial")

    monkeypatch.setattr(ef, "send_private_forward_msg", boom)
    await ef.run_private_post_solve_editorial_followup(12345, PID)


def test_private_success_schedules_editorial_only_on_first_solve(monkeypatch):
    """_send_private_success must deliver the editorial once per problem."""
    import kouhai_bot.handlers.cmd.submit as submit_mod
    from kouhai_bot.private_judge import PRIVATE_SCOPE

    calls: list[tuple[int, str]] = []
    monkeypatch.setattr(
        submit_mod,
        "schedule_private_post_solve_editorial_followup",
        lambda uid, pid: calls.append((uid, pid)),
    )
    solved: set[str] = set()

    def fake_is_solved(uid, pid):
        return str(pid) in solved

    monkeypatch.setattr(submit_mod, "is_private_solved", fake_is_solved)
    monkeypatch.setattr(
        submit_mod, "mark_private_solved", lambda uid, pid, **kw: solved.add(str(pid))
    )
    monkeypatch.setattr(submit_mod, "get_today_problem", lambda gid: None)

    async def fake_plain(req, text):
        return None

    monkeypatch.setattr(submit_mod, "_send_req_plain", fake_plain)

    handler = submit_mod.GroupCoordinator.__new__(submit_mod.GroupCoordinator)
    monkeypatch.setattr(
        handler, "_problem_label_from_snapshot", lambda req, pid: "CF1000A"
    )
    monkeypatch.setattr(handler, "_log_finished", lambda *a, **k: None)

    req = submit_mod.PendingRequest(
        kind="submit",
        group_id=1,
        user_id=12345,
        sender={},
        message_id="m1",
        command="/submit",
        nickname="tester",
        scope=PRIVATE_SCOPE,
    )

    async def _run():
        await handler._send_private_success(req, PID)
        await handler._send_private_success(req, PID)

    asyncio.run(_run())
    assert len(calls) == 1, f"editorial scheduled {len(calls)} times, want 1"
    assert calls[0] == (12345, PID)
