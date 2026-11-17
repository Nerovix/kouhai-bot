"""Durable single-slot next-problem prefetch coordinator (DB-validated).

The READY slot itself is persisted as ``groups/<gid>/next_problem.json``;
validation (rating range, current post, solves, statement fingerprint) reads
the SQLite state layer. These tests bind the global database to the tmp-path
``db`` fixture and seed real catalog/post/solve rows.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from kouhai_bot import problem_prefetch
from kouhai_bot.config import BotConfig
from kouhai_bot.db import set_database
from kouhai_bot.db.repos import posts, problems, users
from kouhai_bot.problem_content import statement_fingerprint
from kouhai_bot.problem_prefetch import NextProblemPrefetcher, PrefetchedProblem
from kouhai_bot.problem_preparation import PreparedProblem
from kouhai_bot.problem_summary import SummaryPreparationStatus

GROUP = "G1"
STATEMENT = {"description": "statement", "images": []}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.config._config", cfg)
    return cfg


@pytest.fixture
async def bound_db(db):
    set_database(db)
    yield db


@pytest.fixture(autouse=True)
def _reset_prefetchers():
    problem_prefetch.reset_prefetchers_for_tests()
    yield
    problem_prefetch.reset_prefetchers_for_tests()


def _prepared(
    pid: str = "542D",
    *,
    min_rating: int = 2000,
    max_rating: int = 3000,
    statement: dict | None = None,
) -> PreparedProblem:
    source = statement if statement is not None else STATEMENT
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
        summary_status=SummaryPreparationStatus.READY,
        model_tag="『M』",
        statement_sha256=statement_fingerprint(source),
        sample_messages=("样例",),
        notes_message="解释",
        min_rating=min_rating,
        max_rating=max_rating,
        prepared_at=1,
    )


async def _seed_problem(db, pid: str, statement: dict | None = None) -> str:
    source = statement if statement is not None else STATEMENT
    sha = statement_fingerprint(source)
    await problems.upsert_problem(
        db, pid,
        name="Prepared",
        rating=2600,
        statement_sha256=sha,
        statement_json=json.dumps(source, ensure_ascii=False),
    )
    return sha


async def test_maintenance_and_cold_claim_share_one_build(bound_db, cfg):
    await _seed_problem(bound_db, "542D")
    started = asyncio.Event()
    finish = asyncio.Event()
    calls = 0

    async def prepare(group_openid):
        nonlocal calls
        calls += 1
        assert group_openid == GROUP
        started.set()
        await finish.wait()
        return _prepared()

    prefetcher = NextProblemPrefetcher(GROUP, prepare=prepare, retry_interval_sec=10)
    stop = asyncio.Event()
    runner = asyncio.create_task(prefetcher.run(stop_event=stop))
    try:
        await started.wait()
        claim_task = asyncio.create_task(prefetcher.claim())
        finish.set()
        slot = await claim_task
        assert slot.problem.pid == "542D"
        assert calls == 1, "cold claim must reuse the maintenance build"
        await prefetcher.release(slot.slot_id)
    finally:
        stop.set()
        await runner


async def test_ready_observer_is_notified_after_complete_slot_is_stored(bound_db, cfg):
    await _seed_problem(bound_db, "542D")
    observed: list[tuple[str, bool]] = []

    prefetcher = NextProblemPrefetcher(
        GROUP, prepare=AsyncMock(return_value=_prepared()),
    )
    prefetcher.set_ready_observer(
        lambda pid: observed.append((pid, prefetcher.slot_path.is_file()))
    )
    slot = await prefetcher._ensure_ready()
    assert slot is not None
    # The observer fires only after the durable slot exists on disk.
    assert observed == [("542D", True)]


async def test_ready_observer_failure_does_not_break_build(bound_db, cfg):
    await _seed_problem(bound_db, "542D")

    def bad_observer(_pid):
        raise RuntimeError("observer crashed")

    prefetcher = NextProblemPrefetcher(
        GROUP, prepare=AsyncMock(return_value=_prepared()),
    )
    prefetcher.set_ready_observer(bad_observer)
    slot = await prefetcher._ensure_ready()
    assert slot is not None
    assert await prefetcher.peek_pid() == "542D"


async def test_ready_slot_survives_coordinator_recreation(bound_db, cfg):
    await _seed_problem(bound_db, "542D")
    prepare = AsyncMock(return_value=_prepared())

    first = NextProblemPrefetcher(GROUP, prepare=prepare)
    slot = await first._ensure_ready()
    assert slot is not None
    assert first.slot_path.is_file()

    second = NextProblemPrefetcher(
        GROUP,
        prepare=AsyncMock(side_effect=AssertionError("must reuse persisted slot")),
    )
    restored = await second.peek()
    assert restored is not None
    assert restored.slot_id == slot.slot_id
    assert restored.problem == slot.problem
    assert prepare.await_count == 1


async def test_claim_blocks_refill_until_release(bound_db, cfg):
    await _seed_problem(bound_db, "542D")
    await _seed_problem(bound_db, "100A")
    second_started = asyncio.Event()
    finish_second = asyncio.Event()
    calls = 0

    async def prepare(_group_openid):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _prepared()
        second_started.set()
        await finish_second.wait()
        return _prepared("100A")

    prefetcher = NextProblemPrefetcher(GROUP, prepare=prepare, retry_interval_sec=10)
    await prefetcher._ensure_ready()
    slot = await prefetcher.claim()
    stop = asyncio.Event()
    runner = asyncio.create_task(prefetcher.run(stop_event=stop))
    try:
        await asyncio.sleep(0.02)
        assert calls == 1, "claimed slot must block background refill"
        assert await prefetcher.peek() is None

        await prefetcher.release(slot.slot_id)
        await asyncio.wait_for(second_started.wait(), timeout=1)
        assert calls == 2
        finish_second.set()
        for _ in range(200):
            ready = await prefetcher.peek()
            if ready is not None and ready.problem.pid == "100A":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("replacement slot never became READY")
    finally:
        finish_second.set()
        stop.set()
        await runner


async def test_double_claim_rejected_and_stale_release_ignored(bound_db, cfg):
    await _seed_problem(bound_db, "542D")
    prefetcher = NextProblemPrefetcher(
        GROUP, prepare=AsyncMock(return_value=_prepared()),
    )
    slot = await prefetcher.claim()
    with pytest.raises(RuntimeError, match="already claimed"):
        await prefetcher.claim()

    # A release token that does not match the claimed slot is ignored.
    await prefetcher.release("not-the-slot-id")
    with pytest.raises(RuntimeError, match="already claimed"):
        await prefetcher.claim()

    await prefetcher.release(slot.slot_id)
    again = await prefetcher.claim()
    assert again.problem.pid == "542D"
    await prefetcher.release(again.slot_id)


async def test_maintenance_paces_retries_after_build_failure(bound_db, cfg):
    calls = 0
    first_attempt = asyncio.Event()

    async def prepare(_group_openid):
        nonlocal calls
        calls += 1
        first_attempt.set()
        raise RuntimeError("temporary failure")

    prefetcher = NextProblemPrefetcher(
        GROUP, prepare=prepare, retry_interval_sec=0.05,
    )
    stop = asyncio.Event()
    runner = asyncio.create_task(prefetcher.run(stop_event=stop))
    try:
        await first_attempt.wait()
        await asyncio.sleep(0.01)
        assert calls == 1, "retry must wait for the retry interval"
        await asyncio.sleep(0.08)
        assert calls >= 2, "maintenance must retry after the interval"
    finally:
        stop.set()
        await runner


async def test_rating_override_change_invalidates_persisted_slot(bound_db, cfg):
    await _seed_problem(bound_db, "542D")
    first = NextProblemPrefetcher(
        GROUP, prepare=AsyncMock(return_value=_prepared()),
    )
    assert await first._ensure_ready() is not None

    await users.set_group_rating(bound_db, GROUP, 2500, 2700)
    second = NextProblemPrefetcher(GROUP)
    assert await second.peek() is None
    assert not second.slot_path.exists()


async def test_statement_change_invalidates_persisted_slot(bound_db, cfg):
    await _seed_problem(bound_db, "542D")
    first = NextProblemPrefetcher(
        GROUP, prepare=AsyncMock(return_value=_prepared()),
    )
    assert await first._ensure_ready() is not None

    # The catalog fingerprint moves (e.g. the statement was re-fetched).
    await _seed_problem(bound_db, "542D", {"description": "changed", "images": []})
    second = NextProblemPrefetcher(GROUP)
    assert await second.peek() is None
    assert not second.slot_path.exists()


async def test_missing_catalog_statement_invalidates_slot(bound_db, cfg):
    await _seed_problem(bound_db, "542D")
    prefetcher = NextProblemPrefetcher(
        GROUP, prepare=AsyncMock(return_value=_prepared()),
    )
    assert await prefetcher._ensure_ready() is not None
    await bound_db.execute("DELETE FROM problems WHERE pid='542D'")
    assert await prefetcher.peek() is None


async def test_current_or_solved_problem_invalidates_slot(bound_db, cfg):
    await _seed_problem(bound_db, "542D")
    await _seed_problem(bound_db, "999X")
    prepare = AsyncMock(return_value=_prepared())
    prefetcher = NextProblemPrefetcher(GROUP, prepare=prepare)
    assert await prefetcher._ensure_ready() is not None

    # Publishing the prefetched pid as the group's current view invalidates it.
    await posts.post_problem(bound_db, GROUP, "542D")
    assert await prefetcher.peek() is None

    # A different current problem lets the slot rebuild.
    await posts.post_problem(bound_db, GROUP, "999X")
    assert await prefetcher._ensure_ready() is not None

    # Any group solve of the prefetched pid also invalidates it.
    await posts.record_solve(bound_db, GROUP, "U1", "542D", "Alice")
    assert await prefetcher.peek() is None


async def test_corrupt_or_legacy_slot_file_is_discarded(bound_db, cfg):
    await _seed_problem(bound_db, "542D")
    prefetcher = NextProblemPrefetcher(
        GROUP,
        prepare=AsyncMock(side_effect=AssertionError("not expected")),
    )
    prefetcher.slot_path.parent.mkdir(parents=True, exist_ok=True)
    prefetcher.slot_path.write_text("{not json", encoding="utf-8")
    assert await prefetcher.peek() is None
    assert not prefetcher.slot_path.exists()

    legacy = _prepared().to_json()
    legacy["format_version"] = 1
    prefetcher.slot_path.write_text(
        json.dumps({"format_version": 1, "slot_id": "s", "problem": legacy}),
        encoding="utf-8",
    )
    assert await prefetcher.peek() is None
    assert not prefetcher.slot_path.exists()


async def test_persistence_failure_keeps_foreground_slot_available(bound_db, cfg, monkeypatch):
    await _seed_problem(bound_db, "542D")
    prefetcher = NextProblemPrefetcher(
        GROUP, prepare=AsyncMock(return_value=_prepared()),
    )

    def fail_write(_slot):
        raise OSError("disk full")

    monkeypatch.setattr(prefetcher, "_write_slot_locked", fail_write)
    slot = await prefetcher.claim()
    assert slot.problem.pid == "542D"
    assert not prefetcher.slot_path.exists()
    await prefetcher.release(slot.slot_id)


def test_prefetched_problem_json_roundtrip_and_format_gate():
    slot = PrefetchedProblem(slot_id="s1", problem=_prepared())
    restored = PrefetchedProblem.from_json(slot.to_json())
    assert restored == slot

    assert PrefetchedProblem.from_json(None) is None
    assert PrefetchedProblem.from_json({"slot_id": "s"}) is None
    legacy = slot.to_json()
    legacy["format_version"] = 1
    assert PrefetchedProblem.from_json(legacy) is None
    missing_id = slot.to_json()
    missing_id["slot_id"] = ""
    assert PrefetchedProblem.from_json(missing_id) is None