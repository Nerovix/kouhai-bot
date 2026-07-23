import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot import problem_prefetch
from kouhai_bot.problem_prefetch import NextProblemPrefetcher
from kouhai_bot.problem_preparation import PreparedProblem


GROUP_ID = 123


def _prepared(pid: str = "542D", *, min_rating: int = 2000, max_rating: int = 3000):
    return PreparedProblem(
        state={
            "today": pid,
            "contestId": 542,
            "index": "D",
            "name": "Prepared",
            "rating": 2600,
            "tags": [],
            "date": "2026-01-01",
        },
        summary="中文题意",
        model_tag="『M』",
        sample_messages=("样例",),
        notes_message="解释",
        min_rating=min_rating,
        max_rating=max_rating,
        prepared_at=1,
    )


def _configure(tmp_path, monkeypatch, rating=(2000, 3000)):
    cfg = SimpleNamespace(
        data_dir=str(tmp_path),
        llm_multimodal_providers=[],
    )
    statement_dir = tmp_path / "statements"
    statement_dir.mkdir(parents=True, exist_ok=True)
    (statement_dir / "542D.json").write_text(json.dumps({
        "description": "statement",
        "images": [],
    }))
    monkeypatch.setattr(problem_prefetch, "get_config", lambda: cfg)
    monkeypatch.setattr(
        problem_prefetch,
        "effective_rating_range",
        lambda _group_id: rating,
    )
    monkeypatch.setattr(problem_prefetch, "get_today_problem", lambda _group_id: None)
    monkeypatch.setattr(
        problem_prefetch,
        "load_scoreboard",
        lambda _group_id: {"solves": []},
    )
    monkeypatch.setattr(
        problem_prefetch,
        "load_statement_json",
        lambda _group_id, _pid: {"description": "statement", "images": []},
    )
    monkeypatch.setattr(
        problem_prefetch,
        "schedule_prefetch_editorial",
        lambda *_args, **_kwargs: None,
    )
    return cfg


def test_maintenance_and_cold_claim_share_one_build(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    started = asyncio.Event()
    finish = asyncio.Event()
    calls = 0

    async def prepare(_group_id):
        nonlocal calls
        calls += 1
        started.set()
        await finish.wait()
        return _prepared()

    async def run():
        prefetcher = NextProblemPrefetcher(
            GROUP_ID,
            prepare=prepare,
            retry_interval_sec=10,
        )
        stop = asyncio.Event()
        runner = asyncio.create_task(prefetcher.run(stop_event=stop))
        await started.wait()
        claim_task = asyncio.create_task(prefetcher.claim())
        finish.set()
        slot = await claim_task
        assert slot.problem.pid == "542D"
        assert calls == 1
        await prefetcher.release(slot.slot_id)
        stop.set()
        await runner

    asyncio.run(run())


def test_ready_slot_survives_coordinator_recreation(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    prepare = AsyncMock(return_value=_prepared())

    async def run():
        first = NextProblemPrefetcher(GROUP_ID, prepare=prepare)
        slot = await first._ensure_ready()
        assert slot is not None
        assert first.slot_path.is_file()

        second = NextProblemPrefetcher(
            GROUP_ID,
            prepare=AsyncMock(side_effect=AssertionError("must reuse disk slot")),
        )
        restored = await second.peek()
        assert restored is not None
        assert restored.slot_id == slot.slot_id

    asyncio.run(run())
    assert prepare.await_count == 1


def test_rehydrated_slot_only_resumes_cached_editorial_work(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    scheduled: list[tuple[str, bool]] = []
    resumed = asyncio.Event()

    def schedule(pid, *, run_agent=True):
        scheduled.append((pid, run_agent))
        resumed.set()

    monkeypatch.setattr(problem_prefetch, "schedule_prefetch_editorial", schedule)

    async def run():
        first = NextProblemPrefetcher(
            GROUP_ID,
            prepare=AsyncMock(return_value=_prepared()),
        )
        assert await first._ensure_ready() is not None

        second = NextProblemPrefetcher(
            GROUP_ID,
            prepare=AsyncMock(side_effect=AssertionError("must reuse disk slot")),
        )
        stop = asyncio.Event()
        runner = asyncio.create_task(second.run(stop_event=stop))
        await asyncio.wait_for(resumed.wait(), timeout=1)
        stop.set()
        await runner

    asyncio.run(run())
    assert scheduled == [("542D", False)]


def test_claim_blocks_refill_until_release(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    second_started = asyncio.Event()
    finish_second = asyncio.Event()
    calls = 0

    async def prepare(_group_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _prepared()
        second_started.set()
        await finish_second.wait()
        return _prepared("100A")

    async def run():
        prefetcher = NextProblemPrefetcher(
            GROUP_ID,
            prepare=prepare,
            retry_interval_sec=10,
        )
        await prefetcher._ensure_ready()
        slot = await prefetcher.claim()
        stop = asyncio.Event()
        runner = asyncio.create_task(prefetcher.run(stop_event=stop))
        await asyncio.sleep(0.02)
        assert calls == 1

        await prefetcher.release(slot.slot_id)
        await asyncio.wait_for(second_started.wait(), timeout=1)
        assert calls == 2
        finish_second.set()
        for _ in range(100):
            ready = await prefetcher.peek()
            if ready is not None and ready.problem.pid == "100A":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("replacement slot never became READY")
        stop.set()
        await runner

    asyncio.run(run())


def test_maintenance_paces_retries_after_build_failure(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    calls = 0
    first_attempt = asyncio.Event()

    async def prepare(_group_id):
        nonlocal calls
        calls += 1
        first_attempt.set()
        raise RuntimeError("temporary failure")

    async def run():
        prefetcher = NextProblemPrefetcher(
            GROUP_ID,
            prepare=prepare,
            retry_interval_sec=0.05,
        )
        stop = asyncio.Event()
        runner = asyncio.create_task(prefetcher.run(stop_event=stop))
        await first_attempt.wait()
        await asyncio.sleep(0.01)
        assert calls == 1
        await asyncio.sleep(0.06)
        assert calls >= 2
        stop.set()
        await runner

    asyncio.run(run())


def test_rating_change_invalidates_persisted_slot(tmp_path, monkeypatch):
    rating = [2000, 3000]
    _configure(tmp_path, monkeypatch)
    monkeypatch.setattr(
        problem_prefetch,
        "effective_rating_range",
        lambda _group_id: tuple(rating),
    )

    async def run():
        first = NextProblemPrefetcher(
            GROUP_ID,
            prepare=AsyncMock(return_value=_prepared()),
        )
        assert await first._ensure_ready() is not None
        rating[:] = [2500, 2700]

        second = NextProblemPrefetcher(GROUP_ID)
        assert await second.peek() is None
        assert not second.slot_path.exists()

    asyncio.run(run())


def test_multimodal_config_change_invalidates_image_slot(tmp_path, monkeypatch):
    cfg = _configure(tmp_path, monkeypatch)
    cfg.llm_multimodal_providers = [object()]
    monkeypatch.setattr(
        problem_prefetch,
        "load_statement_json",
        lambda _group_id, _pid: {
            "description": "statement",
            "images": [{"src": "https://example.invalid/diagram.png"}],
        },
    )

    async def run():
        first = NextProblemPrefetcher(
            GROUP_ID,
            prepare=AsyncMock(return_value=_prepared()),
        )
        assert await first._ensure_ready() is not None
        cfg.llm_multimodal_providers = []

        second = NextProblemPrefetcher(GROUP_ID)
        assert await second.peek() is None
        assert not second.slot_path.exists()

    asyncio.run(run())


def test_current_or_solved_problem_invalidates_slot(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    current = [None]
    scoreboard = {"solves": []}
    monkeypatch.setattr(
        problem_prefetch,
        "get_today_problem",
        lambda _group_id: current[0],
    )
    monkeypatch.setattr(
        problem_prefetch,
        "load_scoreboard",
        lambda _group_id: scoreboard,
    )

    async def run():
        prefetcher = NextProblemPrefetcher(
            GROUP_ID,
            prepare=AsyncMock(return_value=_prepared()),
        )
        assert await prefetcher._ensure_ready() is not None
        current[0] = {"today": "542D"}
        assert await prefetcher.peek() is None
        current[0] = None
        assert await prefetcher._ensure_ready() is not None
        scoreboard["solves"].append({"problem": "542D"})
        assert await prefetcher.peek() is None

    asyncio.run(run())


def test_persistence_failure_keeps_foreground_slot_available(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)

    async def run():
        prefetcher = NextProblemPrefetcher(
            GROUP_ID,
            prepare=AsyncMock(return_value=_prepared()),
        )
        monkeypatch.setattr(
            prefetcher,
            "_write_slot_locked",
            lambda _slot: (_ for _ in ()).throw(OSError("disk full")),
        )
        slot = await prefetcher.claim()
        assert slot.problem.pid == "542D"
        await prefetcher.release(slot.slot_id)

    asyncio.run(run())
