"""Editorial prefetch backoff/scheduling and group-only long-image delivery.

Private-judge delivery tests were dropped: private judge no longer exists in
the official-API architecture. Delivery now renders a long image via
``send_forward_card`` with a plain-text fallback.
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import kouhai_bot.editorial_followup as ef
from kouhai_bot.config import BotConfig
from kouhai_bot.db import set_database
from kouhai_bot.db.repos import posts, problems
from kouhai_bot.handlers.runtime import Runtime, set_runtime
from kouhai_bot.official.budget import ReplyBudget
from kouhai_bot.official.sender import OfficialSender
from kouhai_bot.problem_content import statement_fingerprint

GROUP = "G1"
PID = "1000A"
STATEMENT = {"description": "statement", "images": []}
LONG_EN = "Use dynamic programming over subsets. " * 10
LONG_ZH = "这是官方题解的中文翻译，长度足够通过校验。" * 10
URL = "https://codeforces.com/blog/entry/1"


class FakeRest:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []
        self.uploads: list[tuple[str, dict]] = []

    async def send_group_message(self, group_openid, payload):
        self.sent.append((group_openid, payload))
        return {"id": f"m{len(self.sent)}"}

    async def upload_group_media(self, group_openid, payload):
        self.uploads.append((group_openid, payload))
        return {"file_info": "FI", "ttl": 300}


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
def rest(bound_db):
    rest = FakeRest()
    sender = OfficialSender(rest, ReplyBudget(bound_db), bot_name="测试Bot")
    set_runtime(Runtime(db=bound_db, sender=sender, browser=None))
    return rest


@pytest.fixture(autouse=True)
def _clear_ef_state():
    for mapping in (ef._prefetch_tasks, ef._prefetch_attempts, ef._prefetch_last_attempt_at):
        mapping.clear()
    ef._background_tasks.clear()
    yield
    for mapping in (ef._prefetch_tasks, ef._prefetch_attempts, ef._prefetch_last_attempt_at):
        mapping.clear()
    ef._background_tasks.clear()


async def _seed_problem(db, pid=PID, statement=None):
    source = statement if statement is not None else STATEMENT
    sha = statement_fingerprint(source)
    await problems.upsert_problem(
        db, pid,
        statement_sha256=sha,
        statement_json=json.dumps(source, ensure_ascii=False),
    )
    return sha


async def _seed_verified(db, pid=PID, zh=LONG_ZH, url=URL):
    sha = await _seed_problem(db, pid)
    await problems.upsert_editorial_source(
        db, pid,
        tutorial_url=url,
        tutorial_title="Editorial",
        sections_json=json.dumps([{"solution": LONG_EN}]),
        source_sha256="e" * 64,
        statement_sha256=sha,
    )
    await problems.set_editorial_translation(db, pid, zh, "verified")
    return sha


# --- backoff ----------------------------------------------------------------


def test_backoff_starts_allowed():
    assert ef._prefetch_backoff_remaining(PID) == 0.0


def test_backoff_after_failure_and_reset_on_success():
    ef._mark_prefetch_failed(PID)
    remaining = ef._prefetch_backoff_remaining(PID)
    assert 0.0 < remaining <= ef._PREFETCH_BACKOFF_BASE_SEC

    ef._mark_prefetch_succeeded(PID)
    assert ef._prefetch_backoff_remaining(PID) == 0.0
    assert ef._prefetch_attempts.get(PID) is None


def test_backoff_grows_exponentially():
    expected = [60.0, 120.0, 240.0, 480.0]
    for idx, want in enumerate(expected):
        ef._mark_prefetch_failed(PID)
        remaining = ef._prefetch_backoff_remaining(PID)
        assert want - 1.0 < remaining <= want, f"attempt {idx}: {remaining}"


def test_backoff_caps_at_max():
    ef._prefetch_attempts[PID] = 100
    ef._prefetch_last_attempt_at[PID] = time.monotonic()
    remaining = ef._prefetch_backoff_remaining(PID)
    assert ef._PREFETCH_BACKOFF_MAX_SEC - 1.0 < remaining <= ef._PREFETCH_BACKOFF_MAX_SEC


def test_schedule_skips_during_backoff(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("create_task must not run during backoff")

    monkeypatch.setattr(ef.asyncio, "create_task", boom)
    ef._mark_prefetch_failed(PID)
    ef.schedule_prefetch_editorial(PID)  # must not raise


async def test_schedule_creates_task_when_allowed(monkeypatch):
    calls = []

    def fake_create_task(coro, *, name=None):
        calls.append(name)
        coro.close()
        return asyncio.get_running_loop().create_future()

    monkeypatch.setattr(ef.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(ef, "_track_task", lambda task: None)
    ef.schedule_prefetch_editorial(PID)
    assert len(calls) == 1
    assert PID in calls[0]


async def test_schedule_dedups_inflight_task():
    gate = asyncio.Event()

    async def slow():
        await gate.wait()

    task = asyncio.create_task(slow())
    ef._prefetch_tasks[PID] = task
    try:
        ef.schedule_prefetch_editorial(PID)
        assert ef._prefetch_tasks[PID] is task, "in-flight prefetch must be reused"
    finally:
        gate.set()
        await task


async def test_ensure_schedules_even_for_terminal_state(bound_db, cfg, monkeypatch):
    """ensure_* always schedules; the terminal check lives inside the task."""
    await _seed_verified(bound_db)
    prefetch = AsyncMock(side_effect=AssertionError("terminal state must not re-prefetch"))
    monkeypatch.setattr(ef, "prefetch_editorial_zh", prefetch)

    ef.ensure_editorial_prefetch(PID)
    task = ef._prefetch_tasks[PID]
    await task
    prefetch.assert_not_awaited()
    assert ef._prefetch_attempts.get(PID) is None


async def test_run_prefetch_failure_marks_backoff(bound_db, cfg, monkeypatch):
    await _seed_problem(bound_db)
    monkeypatch.setattr(
        ef, "prefetch_editorial_zh", AsyncMock(side_effect=RuntimeError("crawler down")),
    )
    await ef._run_prefetch_editorial(PID, run_agent=True)
    assert ef._prefetch_attempts[PID] == 1
    assert ef._prefetch_backoff_remaining(PID) > 0


async def test_run_prefetch_incomplete_result_marks_backoff(bound_db, cfg, monkeypatch):
    await _seed_problem(bound_db)
    monkeypatch.setattr(ef, "prefetch_editorial_zh", AsyncMock(return_value=None))
    await ef._run_prefetch_editorial(PID, run_agent=True)
    assert ef._prefetch_attempts[PID] == 1


async def test_run_prefetch_success_resets_backoff(bound_db, cfg, monkeypatch):
    async def fake_prefetch(pid, *, run_agent=True):
        await _seed_verified(bound_db, pid)

    monkeypatch.setattr(ef, "prefetch_editorial_zh", fake_prefetch)
    ef._mark_prefetch_failed(PID)
    ef._mark_prefetch_failed(PID)
    await ef._run_prefetch_editorial(PID, run_agent=True)
    assert ef._prefetch_attempts.get(PID) is None
    assert ef._prefetch_backoff_remaining(PID) == 0.0


# --- post-solve delivery ------------------------------------------------------


async def test_warm_cache_delivers_immediately_without_prefetch_wait(bound_db, cfg, rest, monkeypatch):
    await _seed_verified(bound_db)
    monkeypatch.setattr(
        ef, "_await_prefetch_if_running",
        AsyncMock(side_effect=AssertionError("warm cache must not wait for prefetch")),
    )
    card = AsyncMock(return_value="mid")
    monkeypatch.setattr(ef, "send_forward_card", card)

    await ef.run_post_solve_editorial_followup(GROUP, PID)

    card.assert_awaited_once()
    args, kwargs = card.await_args
    assert args[1] == GROUP
    nodes = args[2]
    assert nodes[0]["nickname"] == "测试Bot"
    assert kwargs["title"] == f"📖 {PID} 官方题解"
    payload = kwargs["text_fallback"]
    assert payload.startswith(f"📖 {PID} 官方题解\n\n")
    assert "来源" not in payload and URL not in payload
    assert LONG_ZH in payload


async def test_no_editorial_marker_is_silent(bound_db, cfg, rest, monkeypatch):
    await _seed_problem(bound_db)
    from kouhai_bot import tutorials

    await tutorials.mark_no_official_editorial(PID, reason="exhaustive")
    card = AsyncMock(side_effect=AssertionError("must not deliver"))
    monkeypatch.setattr(ef, "send_forward_card", card)
    await ef.run_post_solve_editorial_followup(GROUP, PID)
    card.assert_not_awaited()
    assert rest.sent == []


async def test_incomplete_editorial_skips_without_terminal_marker(bound_db, cfg, rest, monkeypatch):
    await _seed_problem(bound_db)
    card = AsyncMock(side_effect=AssertionError("must not deliver"))
    monkeypatch.setattr(ef, "send_forward_card", card)
    await ef.run_post_solve_editorial_followup(GROUP, PID)
    card.assert_not_awaited()
    from kouhai_bot.tutorials import get_editorial_state

    assert await get_editorial_state(PID) == "incomplete"


async def test_delivery_awaits_inflight_prefetch_then_delivers(bound_db, cfg, rest, monkeypatch):
    await _seed_problem(bound_db)

    async def slow_prefetch():
        await asyncio.sleep(0.02)
        await _seed_verified(bound_db)

    task = asyncio.create_task(slow_prefetch())
    ef._prefetch_tasks[PID] = task
    card = AsyncMock(return_value="mid")
    monkeypatch.setattr(ef, "send_forward_card", card)

    await ef.run_post_solve_editorial_followup(GROUP, PID)

    assert task.done()
    card.assert_awaited_once()


async def test_deliver_returns_false_for_short_translation(bound_db, cfg, rest):
    await _seed_verified(bound_db, zh="太短")
    delivered = await ef.deliver_official_tutorial(GROUP, PID)
    assert delivered is False
    assert rest.sent == []


async def test_deliver_chunks_long_payload(bound_db, cfg, rest, monkeypatch):
    zh = "长" * 12000
    await _seed_verified(bound_db, zh=zh)
    card = AsyncMock(return_value="mid")
    monkeypatch.setattr(ef, "send_forward_card", card)

    assert await ef.deliver_official_tutorial(GROUP, PID) is True
    nodes = card.await_args.args[2]
    payload = card.await_args.kwargs["text_fallback"]
    assert len(nodes) == (len(payload) + 4999) // 5000
    assert all(len(node["content_text"]) <= 5000 for node in nodes)
    assert "".join(node["content_text"] for node in nodes) == payload


async def test_render_failure_falls_back_to_plain_text(bound_db, cfg, rest, monkeypatch):
    await _seed_verified(bound_db)
    monkeypatch.setattr(
        "kouhai_bot.render.forward_card.render_card_png",
        AsyncMock(side_effect=RuntimeError("no chromium")),
    )

    assert await ef.deliver_official_tutorial(GROUP, PID) is True
    assert rest.uploads == []
    assert len(rest.sent) == 1
    gid, payload = rest.sent[0]
    assert gid == GROUP
    assert payload["msg_type"] == 0
    assert payload["content"].startswith(f"📖 {PID} 官方题解")
    assert LONG_ZH in payload["content"]


async def test_render_success_sends_media_message(bound_db, cfg, rest, monkeypatch):
    await _seed_verified(bound_db)
    monkeypatch.setattr(
        "kouhai_bot.render.forward_card.render_card_png",
        AsyncMock(return_value=b"png-bytes"),
    )

    assert await ef.deliver_official_tutorial(GROUP, PID) is True
    assert len(rest.uploads) == 1
    assert rest.uploads[0][1]["file_type"] == 1
    assert len(rest.sent) == 1
    gid, payload = rest.sent[0]
    assert gid == GROUP
    assert payload["msg_type"] == 7
    assert payload["media"]["file_info"] == "FI"


async def test_schedule_post_solve_creates_tracked_task(bound_db, cfg, monkeypatch):
    run = AsyncMock()
    monkeypatch.setattr(ef, "run_post_solve_editorial_followup", run)
    ef.schedule_post_solve_editorial_followup(GROUP, PID)
    assert ef._background_tasks
    await asyncio.gather(*list(ef._background_tasks))
    run.assert_awaited_once_with(GROUP, PID)


# --- maintenance loop ---------------------------------------------------------


async def test_maintenance_loop_covers_current_and_next(bound_db, cfg, monkeypatch):
    await posts.post_problem(bound_db, GROUP, "CUR1")
    seen: list[str] = []
    monkeypatch.setattr(ef, "ensure_editorial_prefetch", seen.append)

    stop = asyncio.Event()

    async def next_pid():
        return "NEXT1"

    loop_task = asyncio.create_task(
        ef.editorial_prefetch_maintenance_loop(
            GROUP, get_next_problem_pid=next_pid, stop_event=stop, interval_seconds=0.01,
        )
    )
    for _ in range(200):
        if "CUR1" in seen and "NEXT1" in seen:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await loop_task
    assert "CUR1" in seen and "NEXT1" in seen


async def test_maintenance_loop_dedups_same_pid(bound_db, cfg, monkeypatch):
    await posts.post_problem(bound_db, GROUP, "SAME1")
    calls = MagicMock()
    monkeypatch.setattr(ef, "ensure_editorial_prefetch", calls)

    stop = asyncio.Event()

    async def next_pid():
        return "SAME1"

    loop_task = asyncio.create_task(
        ef.editorial_prefetch_maintenance_loop(
            GROUP, get_next_problem_pid=next_pid, stop_event=stop, interval_seconds=60,
        )
    )
    for _ in range(200):
        if calls.call_count >= 1:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await loop_task
    assert calls.call_count == 1
    calls.assert_called_with("SAME1")


def test_chunk_text_edges():
    assert ef._chunk_text("", 10) == [""]
    assert ef._chunk_text("abc", 0) == ["abc"]
    assert ef._chunk_text("abcde", 2) == ["ab", "cd", "e"]

async def test_deliver_appends_figure_gallery(bound_db, cfg, rest, monkeypatch):
    sha = await _seed_problem(bound_db)
    await problems.upsert_editorial_source(
        bound_db, PID,
        tutorial_url=URL,
        tutorial_title="Editorial",
        sections_json=json.dumps(
            [{"solution": LONG_EN, "images": ["https://codeforces.com/predownloaded/aa.png"]}]
        ),
        source_sha256="e" * 64,
        statement_sha256=sha,
    )
    await problems.set_editorial_translation(bound_db, PID, LONG_ZH, "verified")

    async def _figs(srcs):
        assert list(srcs) == ["https://codeforces.com/predownloaded/aa.png"]
        return [(1, "data:image/png;base64,AAA")]

    monkeypatch.setattr(ef, "figure_data_uris", _figs)
    card = AsyncMock(return_value="mid")
    monkeypatch.setattr(ef, "send_forward_card", card)

    assert await ef.deliver_official_tutorial(GROUP, PID) is True
    nodes = card.await_args.args[2]
    gallery = nodes[-1]
    assert gallery["content_html"] == (
        '<h3>题解配图</h3><div class="figgrid single">'
        '<figure><img src="data:image/png;base64,AAA" alt="图 1">'
        "<figcaption>图 1</figcaption></figure></div>"
    )
    assert gallery["content_text"] == "题解配图（1 张，见题解长图）"
    payload = card.await_args.kwargs["text_fallback"]
    assert payload.endswith("（本题解含 1 张配图，请在题解长图中查看）")
    assert LONG_ZH in payload


async def test_deliver_inlines_marked_figures(bound_db, cfg, rest, monkeypatch):
    sha = await _seed_problem(bound_db)
    await problems.upsert_editorial_source(
        bound_db, PID,
        tutorial_url=URL,
        tutorial_title="Editorial",
        sections_json=json.dumps(
            [{"solution": LONG_EN, "images": ["https://codeforces.com/predownloaded/aa.png"]}]
        ),
        source_sha256="e" * 64,
        statement_sha256=sha,
    )
    marked_zh = "先看图 1：\n\n[[FIG_1]]\n\n然后继续讨论做法。" + LONG_ZH
    await problems.set_editorial_translation(bound_db, PID, marked_zh, "verified")

    async def _figs(srcs):
        return [(1, "data:image/png;base64,AAA")]

    monkeypatch.setattr(ef, "figure_data_uris", _figs)
    card = AsyncMock(return_value="mid")
    monkeypatch.setattr(ef, "send_forward_card", card)

    assert await ef.deliver_official_tutorial(GROUP, PID) is True
    nodes = card.await_args.args[2]
    texts = [node["content_text"] for node in nodes]
    assert any(text.endswith("先看图 1：") for text in texts)
    figure_index = next(
        i for i, node in enumerate(nodes) if "figgrid" in node["content_html"]
    )
    assert nodes[figure_index]["content_text"] == "图 1"
    assert "然后继续讨论做法" in texts[figure_index + 1]
    assert all("题解配图" not in text for text in texts)
    payload = card.await_args.kwargs["text_fallback"]
    assert "[[FIG_1]]" not in payload
    assert "（图 1）" in payload


async def test_deliver_without_images_has_no_gallery(bound_db, cfg, rest, monkeypatch):
    await _seed_verified(bound_db)
    card = AsyncMock(return_value="mid")
    monkeypatch.setattr(ef, "send_forward_card", card)

    assert await ef.deliver_official_tutorial(GROUP, PID) is True
    nodes = card.await_args.args[2]
    assert all("题解配图" not in node["content_text"] for node in nodes)
    assert "配图" not in card.await_args.kwargs["text_fallback"]
