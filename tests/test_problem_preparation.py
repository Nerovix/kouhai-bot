import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot import problem_preparation
from kouhai_bot.problem_preparation import (
    ProblemPreparationError,
    format_previous_problem_reveal,
)


def _state(pid: str = "542D") -> dict:
    return {
        "today": pid,
        "contestId": 542,
        "index": "D",
        "name": "Prepared",
        "rating": 2600,
        "tags": ["dp"],
        "date": "2026-01-01",
    }


def test_prepare_problem_starts_editorial_before_summary(tmp_path, monkeypatch):
    cfg = SimpleNamespace(data_dir=str(tmp_path))
    statement_dir = tmp_path / "statements"
    statement_dir.mkdir()
    (statement_dir / "542D.json").write_text(json.dumps({
        "description": "statement",
        "input": "n",
        "time_limit": "2s",
        "memory_limit": "256MB",
        "samples": [{"input": "1", "output": "2"}],
        "images": [],
    }))
    scheduled = []

    monkeypatch.setattr(problem_preparation, "get_config", lambda: cfg)
    monkeypatch.setattr(
        problem_preparation,
        "effective_rating_range",
        lambda _group_id: (2500, 2700),
    )
    monkeypatch.setattr(
        problem_preparation,
        "_run_picker",
        AsyncMock(return_value=_state()),
    )
    monkeypatch.setattr(
        problem_preparation,
        "schedule_prefetch_editorial",
        scheduled.append,
    )
    monkeypatch.setattr(
        problem_preparation,
        "build_notes_message",
        AsyncMock(return_value=""),
    )

    async def summarize(*_args, **_kwargs):
        assert scheduled == ["542D"]
        return "中文题意", "『M』"

    monkeypatch.setattr(problem_preparation, "summarize_problem", summarize)

    prepared = asyncio.run(problem_preparation.prepare_problem(1))

    assert prepared.pid == "542D"
    assert prepared.summary == "中文题意"
    assert prepared.model_tag == "『M』"
    assert prepared.sample_messages == ("样例 1\nInput:\n1\n\nOutput:\n2",)
    assert (prepared.min_rating, prepared.max_rating) == (2500, 2700)


def test_prepare_problem_keeps_empty_summary_after_existing_retry(tmp_path, monkeypatch):
    cfg = SimpleNamespace(data_dir=str(tmp_path))
    statement_dir = tmp_path / "statements"
    statement_dir.mkdir()
    (statement_dir / "542D.json").write_text(json.dumps({
        "description": "statement",
        "input": "n",
        "images": [],
    }))
    summarize = AsyncMock(return_value=(None, ""))

    monkeypatch.setattr(problem_preparation, "get_config", lambda: cfg)
    monkeypatch.setattr(
        problem_preparation,
        "effective_rating_range",
        lambda _group_id: (2000, 3000),
    )
    monkeypatch.setattr(
        problem_preparation,
        "_run_picker",
        AsyncMock(return_value=_state()),
    )
    monkeypatch.setattr(problem_preparation, "schedule_prefetch_editorial", lambda _pid: None)
    monkeypatch.setattr(problem_preparation, "summarize_problem", summarize)

    prepared = asyncio.run(problem_preparation.prepare_problem(1))

    assert prepared.summary == ""
    assert summarize.await_count == 2


def test_run_picker_preserves_three_attempt_error(monkeypatch):
    attempts = 0

    class FailedProcess:
        returncode = 1

        async def communicate(self):
            return b"", b"SSL EOF"

    async def create_process(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        return FailedProcess()

    monkeypatch.setattr(
        problem_preparation.asyncio,
        "create_subprocess_exec",
        create_process,
    )
    monkeypatch.setattr(problem_preparation.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(
        problem_preparation,
        "picker_args",
        lambda *_args, **_kwargs: ["picker"],
    )

    with pytest.raises(ProblemPreparationError) as exc_info:
        asyncio.run(problem_preparation._run_picker(1))

    assert attempts == 3
    assert exc_info.value.user_message == "Codeforces 连接失败"


def test_run_picker_cancellation_kills_child_process(monkeypatch):
    communicate_started = asyncio.Event()

    class HangingProcess:
        returncode = None
        killed = False

        async def communicate(self):
            communicate_started.set()
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            return self.returncode

    process = HangingProcess()

    monkeypatch.setattr(
        problem_preparation.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    monkeypatch.setattr(
        problem_preparation,
        "picker_args",
        lambda *_args, **_kwargs: ["picker"],
    )

    async def run():
        task = asyncio.create_task(problem_preparation._run_picker(1))
        await communicate_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert process.killed is True


def test_format_previous_problem_reveal_is_pure_and_compatible():
    assert format_previous_problem_reveal(None) == "还没有发过题哦"
    assert format_previous_problem_reveal([]) == "还没有发过题哦"
    assert format_previous_problem_reveal(_state()) == (
        "上一道题来自 CF542D Prepared 2600✨"
    )
