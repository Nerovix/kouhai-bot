"""Blocking group-problem preparation pipeline (DB-backed, openid-native)."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from kouhai_bot import problem_preparation as pp
from kouhai_bot.config import BotConfig
from kouhai_bot.db import set_database
from kouhai_bot.db.repos import posts, problems, users
from kouhai_bot.problem_content import statement_fingerprint
from kouhai_bot.problem_summary import SummaryPreparationStatus

GROUP = "G1"
PID = "9990A"

STATEMENT = {
    "description": "desc",
    "input": "input spec",
    "time_limit": "2 seconds",
    "memory_limit": "256 megabytes",
    "samples": [{"input": "1 2\n", "output": "3\n"}],
    "notes": "First sample sums the numbers.",
    "images": [],
}

PICKED_STATE = {
    "today": PID,
    "contestId": 9990,
    "index": "A",
    "name": "Test Problem",
    "rating": 2600,
    "tags": ["dp", "math"],
}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.config._config", cfg)
    return cfg


@pytest.fixture
async def bound_db(db):
    set_database(db)
    yield db


@pytest.fixture
def statement_file(cfg):
    path = Path(cfg.data_dir) / "statements" / f"{PID}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(STATEMENT, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def mock_llm(monkeypatch):
    summarize = AsyncMock(return_value=("中文摘要", "『M』"))
    explain = AsyncMock(return_value=("第一个样例把两个数相加。", "『M』"))
    monkeypatch.setattr(pp, "summarize_problem", summarize)
    monkeypatch.setattr(pp, "generate_sample_explanation", explain)
    return summarize, explain


def _mock_picker(monkeypatch, result=None, *, side_effect=None):
    picker = AsyncMock(return_value=result, side_effect=side_effect)
    monkeypatch.setattr(pp, "_run_picker", picker)
    return picker


async def test_prepare_writes_catalog_and_marks_used(bound_db, cfg, statement_file, mock_llm, monkeypatch):
    picker = _mock_picker(monkeypatch, dict(PICKED_STATE))

    prepared = await pp.prepare_problem(GROUP)

    assert prepared.pid == PID
    assert prepared.summary == "中文摘要"
    assert prepared.summary_status is SummaryPreparationStatus.READY
    assert prepared.model_tag == "『M』"
    assert prepared.statement_sha256 == statement_fingerprint(STATEMENT)
    assert prepared.min_rating == cfg.min_rating
    assert prepared.max_rating == cfg.max_rating
    assert prepared.sample_messages == ("样例 1\nInput:\n1 2\n\nOutput:\n3",)
    assert prepared.notes_message == "样例解释：\n第一个样例把两个数相加。"

    # Picker received the DB-driven exclusion set and effective rating range.
    kwargs = picker.await_args.kwargs
    assert kwargs["rating_range"] == (cfg.min_rating, cfg.max_rating)
    assert kwargs["excluded"] == set()

    row = await problems.get_problem(bound_db, PID)
    assert row is not None
    assert row["name"] == "Test Problem"
    assert row["rating"] == 2600
    assert row["tags"] == ["dp", "math"]
    assert row["statement_sha256"] == prepared.statement_sha256
    assert row["statement"]["description"] == "desc"

    assert PID in await posts.get_excluded_pids(bound_db, GROUP)


async def test_group_rating_override_reaches_picker_and_result(bound_db, cfg, statement_file, mock_llm, monkeypatch):
    await users.set_group_rating(bound_db, GROUP, 2500, 2700)
    picker = _mock_picker(monkeypatch, dict(PICKED_STATE))

    prepared = await pp.prepare_problem(GROUP)

    assert picker.await_args.kwargs["rating_range"] == (2500, 2700)
    assert (prepared.min_rating, prepared.max_rating) == (2500, 2700)


async def test_summary_gate_two_failed_attempts_become_incomplete(bound_db, cfg, statement_file, mock_llm, monkeypatch):
    summarize, _ = mock_llm
    summarize.return_value = (None, "")
    _mock_picker(monkeypatch, dict(PICKED_STATE))

    prepared = await pp.prepare_problem(GROUP)

    assert summarize.await_count == 2
    assert prepared.summary == ""
    assert prepared.model_tag == ""
    assert prepared.summary_status is SummaryPreparationStatus.INCOMPLETE
    # The rest of the card content is still complete.
    assert prepared.sample_messages
    assert prepared.notes_message


async def test_summary_exception_then_success(bound_db, cfg, statement_file, mock_llm, monkeypatch):
    summarize, _ = mock_llm
    summarize.side_effect = [RuntimeError("llm down"), ("补上的摘要", "")]
    _mock_picker(monkeypatch, dict(PICKED_STATE))

    prepared = await pp.prepare_problem(GROUP)

    assert summarize.await_count == 2
    assert prepared.summary == "补上的摘要"
    assert prepared.summary_status is SummaryPreparationStatus.READY


async def test_pool_exhausted_relaxes_used_but_keeps_posted_and_solved(bound_db, cfg, statement_file, mock_llm, monkeypatch):
    await posts.mark_used(bound_db, GROUP, "USED1")
    await posts.post_problem(bound_db, GROUP, "POSTED1")
    await posts.record_solve(bound_db, GROUP, "U1", "SOLVED1", "Alice")
    picker = _mock_picker(
        monkeypatch,
        side_effect=[pp.ProblemPoolExhausted(), dict(PICKED_STATE)],
    )

    prepared = await pp.prepare_problem(GROUP)

    assert prepared.pid == PID
    assert picker.await_count == 2
    first, second = picker.await_args_list
    assert first.kwargs["excluded"] == {"USED1", "POSTED1", "SOLVED1"}
    assert second.kwargs["excluded"] == {"POSTED1", "SOLVED1"}


async def test_pool_exhausted_twice_raises_user_message(bound_db, cfg, mock_llm, monkeypatch):
    _mock_picker(monkeypatch, side_effect=pp.ProblemPoolExhausted())
    with pytest.raises(pp.ProblemPoolExhausted) as exc:
        await pp.prepare_problem(GROUP)
    assert exc.value.user_message == "题库里暂时没有合适的未解题了，过段时间再来试试吧😢"


async def test_missing_statement_cache_is_terminal(bound_db, cfg, mock_llm, monkeypatch):
    _mock_picker(monkeypatch, dict(PICKED_STATE))
    with pytest.raises(pp.ProblemPreparationError) as exc:
        await pp.prepare_problem(GROUP)
    assert exc.value.user_message == "题面缓存读取失败"
    # Nothing was cataloged or marked used for the failed preparation.
    assert await problems.get_problem(bound_db, PID) is None
    assert await posts.get_excluded_pids(bound_db, GROUP) == set()


async def test_notes_explanation_failure_skips_notes_node(bound_db, cfg, statement_file, mock_llm, monkeypatch):
    _, explain = mock_llm
    explain.side_effect = RuntimeError("llm down")
    _mock_picker(monkeypatch, dict(PICKED_STATE))

    prepared = await pp.prepare_problem(GROUP)

    assert explain.await_count == pp.NOTES_ATTEMPTS
    assert prepared.notes_message == ""
    assert prepared.summary_status is SummaryPreparationStatus.READY


async def test_notes_explanation_receives_source_material(bound_db, cfg, statement_file, mock_llm, monkeypatch):
    _, explain = mock_llm
    statement = dict(
        STATEMENT,
        description="Full statement [[IMAGE_1: graphic]]",
        images=[
            {"src": "https://x/g1.png", "kind": "graphic", "caption": "Figure 1"},
            {"src": "https://x/f1.png", "kind": "formula"},
        ],
        notes="The tree in the first test:\n\n[[FIG_1]]",
    )
    statement_file.write_text(json.dumps(statement), encoding="utf-8")
    _mock_picker(monkeypatch, dict(PICKED_STATE))

    prepared = await pp.prepare_problem(GROUP)

    kwargs = explain.await_args.kwargs
    assert kwargs["statement_text"] == "Full statement [[IMAGE_1: graphic]]"
    assert kwargs["samples"] == statement["samples"]
    assert kwargs["notes"] == "The tree in the first test:"  # legacy marker stripped
    assert kwargs["summary"] == "中文摘要"
    assert [img["src"] for img in kwargs["images"]] == [
        "https://x/g1.png", "https://x/f1.png",
    ]
    assert prepared.notes_message == "样例解释：\n第一个样例把两个数相加。"


async def test_no_original_notes_skips_generation(bound_db, cfg, statement_file, mock_llm, monkeypatch):
    _, explain = mock_llm
    statement = dict(STATEMENT, notes="")
    statement_file.write_text(json.dumps(statement), encoding="utf-8")
    _mock_picker(monkeypatch, dict(PICKED_STATE))

    prepared = await pp.prepare_problem(GROUP)

    explain.assert_not_awaited()
    assert prepared.notes_message == ""


# --- picker subprocess boundary -------------------------------------------


class _FakeProcess:
    def __init__(self, *, returncode: int, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.killed = False

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


@pytest.fixture
def fast_sleep(monkeypatch):
    monkeypatch.setattr(pp.asyncio, "sleep", AsyncMock())


async def test_run_picker_writes_exclude_file_and_passes_args(bound_db, cfg, monkeypatch, fast_sleep):
    calls: list[list[str]] = []

    async def fake_exec(_python, *args, **_kwargs):
        calls.append(list(args))
        return _FakeProcess(returncode=0, stdout=json.dumps(PICKED_STATE).encode())

    monkeypatch.setattr(pp.asyncio, "create_subprocess_exec", fake_exec)
    state = await pp._run_picker(GROUP, rating_range=(2100, 2400), excluded={"B1", "A1"})

    assert state["today"] == PID
    exclude_path = pp._exclude_file_path(GROUP)
    assert json.loads(exclude_path.read_text(encoding="utf-8")) == ["A1", "B1"]
    args = calls[0]
    assert args[1] == "pick-json"
    assert "--group" in args and GROUP in args
    assert "--exclude-file" in args and str(exclude_path) in args
    assert "--with-statement" in args
    assert args[args.index("--min-rating") + 1] == "2100"
    assert args[args.index("--max-rating") + 1] == "2400"


async def test_run_picker_retries_three_times_then_raises(bound_db, cfg, monkeypatch, fast_sleep):
    exec_mock = AsyncMock(return_value=_FakeProcess(returncode=1, stderr=b"boom"))
    monkeypatch.setattr(pp.asyncio, "create_subprocess_exec", exec_mock)
    with pytest.raises(pp.ProblemPreparationError) as exc:
        await pp._run_picker(GROUP, rating_range=(2000, 3000), excluded=set())
    assert exec_mock.await_count == pp.PICK_ATTEMPTS
    assert exc.value.user_message == "题目选取失败，稍后再试"


async def test_run_picker_invalid_payload_message(bound_db, cfg, monkeypatch, fast_sleep):
    exec_mock = AsyncMock(return_value=_FakeProcess(returncode=0, stdout=b'{"today": ""}'))
    monkeypatch.setattr(pp.asyncio, "create_subprocess_exec", exec_mock)
    with pytest.raises(pp.ProblemPreparationError) as exc:
        await pp._run_picker(GROUP, rating_range=(2000, 3000), excluded=set())
    assert exc.value.user_message == "题目选取结果异常"


async def test_run_picker_exhausted_stderr_stops_immediately(bound_db, cfg, monkeypatch, fast_sleep):
    exec_mock = AsyncMock(return_value=_FakeProcess(
        returncode=1, stderr=b"no unsolved problems found in range",
    ))
    monkeypatch.setattr(pp.asyncio, "create_subprocess_exec", exec_mock)
    with pytest.raises(pp.ProblemPoolExhausted):
        await pp._run_picker(GROUP, rating_range=(2000, 3000), excluded=set())
    assert exec_mock.await_count == 1


async def test_run_picker_timeout_kills_process_and_retries(bound_db, cfg, monkeypatch):
    class _HangingProcess(_FakeProcess):
        async def communicate(self):
            await asyncio.Event().wait()
            return b"", b""

    process = _HangingProcess(returncode=None)
    monkeypatch.setattr(pp.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    monkeypatch.setattr(pp, "PICK_TIMEOUT_SEC", 0.01)

    async def sleep_no_delay(_delay):
        pass

    monkeypatch.setattr(pp.asyncio, "sleep", sleep_no_delay)
    with pytest.raises(pp.ProblemPreparationError):
        await pp._run_picker(GROUP, rating_range=(2000, 3000), excluded=set())
    assert process.killed


async def test_run_picker_cancellation_kills_and_propagates(bound_db, cfg, monkeypatch):
    started = asyncio.Event()

    class _HangingProcess(_FakeProcess):
        async def communicate(self):
            started.set()
            await asyncio.Event().wait()
            return b"", b""

    process = _HangingProcess(returncode=None)
    monkeypatch.setattr(pp.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    task = asyncio.create_task(
        pp._run_picker(GROUP, rating_range=(2000, 3000), excluded=set())
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed


def test_classify_pick_error_mapping():
    assert pp.classify_pick_error("No unsolved problems found") == "EXHAUSTED"
    assert pp.classify_pick_error("HTTPSConnectionPool(host='codeforces.com')") == "Codeforces 连接失败"
    assert pp.classify_pick_error("SSLEOFError occurred") == "Codeforces 连接失败"
    assert pp.classify_pick_error("Read timed out") == "Codeforces 请求超时"
    assert pp.classify_pick_error("Permission denied") == "本地数据读取异常"
    assert pp.classify_pick_error("FileNotFound somewhere") == "本地数据读取异常"
    assert pp.classify_pick_error("mystery failure") == "题目选取失败，稍后再试"


async def test_effective_rating_range_falls_back_on_db_error(bound_db, cfg, monkeypatch):
    monkeypatch.setattr(
        pp.users, "get_group_rating", AsyncMock(side_effect=RuntimeError("db down")),
    )
    assert await pp.effective_rating_range(GROUP) == (cfg.min_rating, cfg.max_rating)


# --- PreparedProblem serialization gate ------------------------------------


def _prepared(**overrides) -> pp.PreparedProblem:
    base = dict(
        state={"today": PID, "name": "Test Problem"},
        summary="摘要",
        summary_status=SummaryPreparationStatus.READY,
        model_tag="『M』",
        statement_sha256="a" * 64,
        sample_messages=("样例 1",),
        notes_message="解释",
        min_rating=2000,
        max_rating=3000,
        prepared_at=1,
    )
    base.update(overrides)
    return pp.PreparedProblem(**base)


def test_prepared_problem_json_roundtrip():
    prepared = _prepared()
    assert prepared.pid == PID
    assert pp.PreparedProblem.from_json(prepared.to_json()) == prepared


def test_prepared_problem_from_json_rejections():
    assert pp.PreparedProblem.from_json(None) is None
    assert pp.PreparedProblem.from_json([]) is None
    assert pp.PreparedProblem.from_json({}) is None

    bad_sha = _prepared().to_json()
    bad_sha["statement_sha256"] = "abc"
    assert pp.PreparedProblem.from_json(bad_sha) is None

    no_pid = _prepared().to_json()
    no_pid["state"] = {"today": ""}
    assert pp.PreparedProblem.from_json(no_pid) is None

    ready_no_summary = _prepared().to_json()
    ready_no_summary["summary"] = ""
    assert pp.PreparedProblem.from_json(ready_no_summary) is None

    incomplete = _prepared(summary_status=SummaryPreparationStatus.INCOMPLETE).to_json()
    assert pp.PreparedProblem.from_json(incomplete) is None  # summary/tag must be empty
    incomplete["summary"] = ""
    incomplete["model_tag"] = ""
    restored = pp.PreparedProblem.from_json(incomplete)
    assert restored is not None
    assert restored.summary_status is SummaryPreparationStatus.INCOMPLETE

    bad_samples = _prepared().to_json()
    bad_samples["sample_messages"] = [1]
    assert pp.PreparedProblem.from_json(bad_samples) is None