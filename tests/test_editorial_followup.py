"""Tests for editorial prefetch exponential backoff."""

import asyncio
import os
import sys
import time

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
