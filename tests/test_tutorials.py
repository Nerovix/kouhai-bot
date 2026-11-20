"""Official editorial extraction, DB-backed verified cache, and prefetch flow.

The persistence boundary moved from disk JSON markers to the SQLite
``editorials`` table (``kouhai_bot.tutorials``); the side-effect-free
search/validate/translate action lives in ``editorial_preparation`` and still
reads the mirrored disk statement cache. ``extract_editorial`` now lives in
``editorial_content``.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kouhai_bot import editorial_preparation as ep
from kouhai_bot import tutorials
from kouhai_bot.config import BotConfig
from kouhai_bot.db import set_database
from kouhai_bot.db.repos import problems
from kouhai_bot.editorial_content import (
    MIN_EDITORIAL_LEN,
    OfficialEditorial,
    extract_editorial,
)
from kouhai_bot.problem_content import statement_fingerprint
from kouhai_bot.tutorials import (
    format_editorial_for_review,
    get_editorial_state,
    get_verified_official_editorial,
    has_cached_editorial_zh,
    is_no_official_editorial,
    load_cached_editorial_zh,
    mark_no_official_editorial,
    prefetch_editorial_zh,
)

PID = "542D"
URL1 = "https://codeforces.com/blog/entry/1"
URL2 = "https://codeforces.com/blog/entry/2"


class _FakeAgentNoMatch(Exception):
    pass


class _FakeAgentIncomplete(Exception):
    pass


class _FakeScrapeError(Exception):
    pass


def _long_text(prefix: str = "x") -> str:
    return prefix * (MIN_EDITORIAL_LEN + 10)


def _statement(pid: str = PID) -> dict:
    return {"description": f"statement for {pid}", "images": []}


def _bundle(url: str, body: str, title: str = "Editorial") -> dict:
    return {
        "tutorial_url": url,
        "tutorial_title": title,
        "sections": [
            {"hint": "", "solution": body, "raw_text": body, "code_blocks": []}
        ],
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


async def _seed_problem(db, cfg, pid: str = PID, statement: dict | None = None) -> str:
    """Mirror one statement to disk (crawler input) and DB (fingerprint gate)."""
    source = statement if statement is not None else _statement(pid)
    sha = statement_fingerprint(source)
    statements_dir = Path(cfg.data_dir) / "statements"
    statements_dir.mkdir(parents=True, exist_ok=True)
    (statements_dir / f"{pid}.json").write_text(
        json.dumps(source, ensure_ascii=False), encoding="utf-8",
    )
    await problems.upsert_problem(
        db, pid,
        statement_sha256=sha,
        statement_json=json.dumps(source, ensure_ascii=False),
    )
    return sha


async def _seed_source(db, pid: str, sha: str, url: str = URL1, body: str | None = None):
    body = body if body is not None else _long_text("english-")
    await problems.upsert_editorial_source(
        db, pid,
        tutorial_url=url,
        tutorial_title="Editorial",
        sections_json=json.dumps(_bundle(url, body)["sections"], ensure_ascii=False),
        source_sha256="e" * 64,
        statement_sha256=sha,
    )


async def _seed_verified(db, cfg, pid: str = PID, zh: str | None = None) -> str:
    sha = await _seed_problem(db, cfg, pid)
    await _seed_source(db, pid, sha)
    await problems.set_editorial_translation(
        db, pid, zh if zh is not None else "已验证中文题解。" * 20, "verified",
    )
    return sha


def _patch_agent(monkeypatch, run_agent):
    monkeypatch.setattr(
        ep, "_load_tutorial_agent",
        lambda: (_FakeAgentNoMatch, _FakeAgentIncomplete, _FakeScrapeError, run_agent),
    )


def _agent_result(url: str, body: str, title: str = "Editorial"):
    return SimpleNamespace(
        bundle=_bundle(url, body, title),
        selected_candidate_id="c1",
        confidence=0.9,
        elapsed_sec=1.0,
    )


# --- extraction (editorial_content) ------------------------------------------


def test_extract_editorial_from_hint():
    section = {
        "hint": _long_text("hint-"),
        "solution": "",
        "raw_text": "Authors & preparation: foo\nEditorial\n" + _long_text("raw-"),
        "code_blocks": [],
    }
    text = extract_editorial(section)
    assert text.startswith("hint-")
    assert len(text) >= MIN_EDITORIAL_LEN


def test_extract_editorial_skips_placeholder_solution_uses_raw():
    body = _long_text("real editorial ")
    section = {
        "hint": "",
        "solution": "Arpa's solution",
        "raw_text": (
            "Authors & preparation: Arpa\n"
            "Editorial\n"
            "Solution\n"
            f"{body}"
        ),
        "code_blocks": [],
    }
    text = extract_editorial(section)
    assert text != "Arpa's solution"
    assert body.strip() in text


def test_extract_editorial_loading_placeholder_returns_short():
    section = {
        "hint": "",
        "solution": "Tutorial is loading...",
        "raw_text": "",
        "code_blocks": [],
    }
    assert len(extract_editorial(section)) < MIN_EDITORIAL_LEN


def test_extract_editorial_appends_code_blocks():
    section = {
        "hint": "",
        "solution": _long_text("sol-"),
        "raw_text": "",
        "code_blocks": ["int main() { return 0; }"],
    }
    text = extract_editorial(section)
    assert "int main()" in text
    assert "```" in text


# --- DB verified-cache gating --------------------------------------------------


async def test_missing_problem_row_is_incomplete(bound_db, cfg):
    assert await get_verified_official_editorial(PID) is None
    assert await get_editorial_state(PID) == "incomplete"
    assert not await has_cached_editorial_zh(PID)
    assert not await is_no_official_editorial(PID)


async def test_verified_editorial_roundtrip(bound_db, cfg):
    await _seed_verified(bound_db, cfg)
    assert await has_cached_editorial_zh(PID)
    assert await get_editorial_state(PID) == "verified"
    editorial = await get_verified_official_editorial(PID)
    assert editorial is not None
    assert editorial.text.startswith("english-")
    assert editorial.tutorial_url == URL1
    assert (await load_cached_editorial_zh(PID)).startswith("已验证中文题解")


async def test_verified_cache_is_bound_to_the_problem_statement(bound_db, cfg):
    await _seed_verified(bound_db, cfg)
    assert await has_cached_editorial_zh(PID)

    # Statement re-fetched with different content → new fingerprint.
    await _seed_problem(bound_db, cfg, PID, {"description": "corrected", "images": []})
    assert not await has_cached_editorial_zh(PID)
    assert await get_verified_official_editorial(PID) is None
    assert await get_editorial_state(PID) == "incomplete"


async def test_source_reupsert_resets_translation(bound_db, cfg):
    sha = await _seed_verified(bound_db, cfg)
    assert await has_cached_editorial_zh(PID)

    # A re-scraped editorial source invalidates the old translation.
    await _seed_source(bound_db, PID, sha, body=_long_text("replacement-"))
    assert not await has_cached_editorial_zh(PID)
    assert await get_editorial_state(PID) == "incomplete"
    assert await get_verified_official_editorial(PID) is None


async def test_cached_translation_strips_leaked_thinking(bound_db, cfg):
    await _seed_verified(
        bound_db, cfg,
        zh="<thinking>hidden cached editorial</thinking>" + ("中文题解。" * 20),
    )
    zh = await load_cached_editorial_zh(PID)
    assert "中文题解" in zh
    assert "hidden cached editorial" not in zh
    assert "thinking" not in zh


async def test_cache_is_per_pid(bound_db, cfg):
    await _seed_verified(bound_db, cfg, PID)
    await _seed_problem(bound_db, cfg, "317C")
    assert await has_cached_editorial_zh(PID)
    assert not await has_cached_editorial_zh("317C")
    assert await get_verified_official_editorial("317C") is None


# --- no-editorial terminal marker ----------------------------------------------


async def test_no_editorial_marker_requires_reason(bound_db, cfg):
    await _seed_problem(bound_db, cfg)
    with pytest.raises(ValueError, match="reason missing"):
        await mark_no_official_editorial(PID, reason="")
    assert not await is_no_official_editorial(PID)


async def test_no_editorial_marker_requires_statement(bound_db, cfg):
    with pytest.raises(ValueError, match="statement missing"):
        await mark_no_official_editorial(PID, reason="agent_exhaustive_no_match:test")


async def test_no_editorial_marker_is_bound_to_the_problem_statement(bound_db, cfg):
    await _seed_problem(bound_db, cfg)
    await mark_no_official_editorial(PID, reason="agent_exhaustive_no_match:test")
    assert await is_no_official_editorial(PID)
    assert await get_editorial_state(PID) == "no_editorial"

    await _seed_problem(bound_db, cfg, PID, {"description": "corrected", "images": []})
    assert not await is_no_official_editorial(PID)


# --- prefetch: terminal short-circuit ------------------------------------------


async def test_prefetch_short_circuits_verified_state(bound_db, cfg, monkeypatch):
    await _seed_verified(bound_db, cfg)
    prepare = AsyncMock(side_effect=AssertionError("verified state must not re-prepare"))
    monkeypatch.setattr(tutorials, "prepare_editorial", prepare)
    await prefetch_editorial_zh(PID)
    prepare.assert_not_awaited()


async def test_prefetch_short_circuits_no_editorial_state(bound_db, cfg, monkeypatch):
    await _seed_problem(bound_db, cfg)
    await mark_no_official_editorial(PID, reason="agent_exhaustive_no_match:test")
    prepare = AsyncMock(side_effect=AssertionError("terminal state must not re-prepare"))
    monkeypatch.setattr(tutorials, "prepare_editorial", prepare)
    await prefetch_editorial_zh(PID)
    prepare.assert_not_awaited()


async def test_prefetch_without_statement_stays_incomplete(bound_db, cfg, monkeypatch):
    _patch_agent(monkeypatch, AsyncMock(side_effect=AssertionError("agent must not run")))
    await prefetch_editorial_zh("999Z")
    assert not await is_no_official_editorial("999Z")
    assert await get_editorial_state("999Z") == "incomplete"


async def test_prefetch_no_agent_leaves_missing_unknown(bound_db, cfg, monkeypatch):
    await _seed_problem(bound_db, cfg)
    _patch_agent(monkeypatch, AsyncMock(side_effect=AssertionError("agent must not run")))
    monkeypatch.setattr(
        ep, "translate_editorial_to_zh",
        AsyncMock(side_effect=AssertionError("translate must not run")),
    )
    await prefetch_editorial_zh(PID, run_agent=False)
    assert not await is_no_official_editorial(PID)
    assert not await has_cached_editorial_zh(PID)


# --- prefetch: persisted source as initial candidate ---------------------------


async def test_prefetch_revalidates_persisted_source_without_agent(bound_db, cfg, monkeypatch):
    sha = await _seed_problem(bound_db, cfg)
    await _seed_source(bound_db, PID, sha)  # source only, translation not verified
    _patch_agent(monkeypatch, AsyncMock(side_effect=AssertionError("agent must not run")))
    translate = AsyncMock(return_value=("重新验证后的译文。" * 20, "", True))
    monkeypatch.setattr(ep, "translate_editorial_to_zh", translate)

    await prefetch_editorial_zh(PID)

    translate.assert_awaited_once()
    assert await has_cached_editorial_zh(PID)
    editorial = await get_verified_official_editorial(PID)
    assert editorial is not None
    assert editorial.tutorial_url == URL1


async def test_prefetch_rejects_wrong_source_and_uses_next_blog(bound_db, cfg, monkeypatch):
    sha = await _seed_problem(bound_db, cfg)
    await _seed_source(bound_db, PID, sha, url=URL1, body=_long_text("wrong-"))
    agent_calls: list[frozenset[str]] = []

    async def find_alternative(**kwargs):
        excluded = kwargs["excluded_tutorial_urls"]
        agent_calls.append(excluded)
        assert kwargs["pid"] == PID
        assert kwargs["blog_limit"] == 0
        return _agent_result(URL2, _long_text("correct-"), "Correct")

    _patch_agent(monkeypatch, find_alternative)
    translate = AsyncMock(side_effect=[
        (None, "", False),
        ("正确候选的中文题解。" * 12, "", True),
    ])
    monkeypatch.setattr(ep, "translate_editorial_to_zh", translate)

    await prefetch_editorial_zh(PID)

    assert agent_calls == [frozenset({URL1})]
    assert translate.await_count == 2
    assert not await is_no_official_editorial(PID)
    editorial = await get_verified_official_editorial(PID)
    assert editorial is not None
    assert editorial.tutorial_url == URL2
    assert await has_cached_editorial_zh(PID)


async def test_prefetch_mismatch_then_incomplete_search_stays_retryable(bound_db, cfg, monkeypatch):
    sha = await _seed_problem(bound_db, cfg)
    await _seed_source(bound_db, PID, sha, url=URL1, body=_long_text("wrong-"))

    async def incomplete(**kwargs):
        assert kwargs["excluded_tutorial_urls"] == frozenset({URL1})
        raise _FakeAgentIncomplete("second blog timed out")

    _patch_agent(monkeypatch, incomplete)
    monkeypatch.setattr(
        ep, "translate_editorial_to_zh", AsyncMock(return_value=(None, "", False)),
    )

    await prefetch_editorial_zh(PID)

    assert not await is_no_official_editorial(PID)
    assert not await has_cached_editorial_zh(PID)
    assert await get_editorial_state(PID) == "incomplete"


async def test_prefetch_marks_terminal_only_after_mismatch_and_exhaustive_search(bound_db, cfg, monkeypatch):
    sha = await _seed_problem(bound_db, cfg)
    await _seed_source(bound_db, PID, sha, url=URL1, body=_long_text("wrong-"))

    async def no_remaining_match(**kwargs):
        assert kwargs["excluded_tutorial_urls"] == frozenset({URL1})
        raise _FakeAgentNoMatch("all remaining blogs explicitly rejected")

    _patch_agent(monkeypatch, no_remaining_match)
    monkeypatch.setattr(
        ep, "translate_editorial_to_zh", AsyncMock(return_value=(None, "", False)),
    )

    await prefetch_editorial_zh(PID)

    assert await is_no_official_editorial(PID)
    assert not await has_cached_editorial_zh(PID)
    row = await bound_db.fetch_one(
        "SELECT no_editorial_reason FROM editorials WHERE pid=?", (PID,),
    )
    assert row["no_editorial_reason"].startswith("agent_exhaustive_no_match:")


# --- prefetch: fresh agent search ----------------------------------------------


async def test_prefetch_runs_agent_before_translate(bound_db, cfg, monkeypatch):
    await _seed_problem(bound_db, cfg)

    async def fake_agent(*, pid, statements_dir, blog_limit, excluded_tutorial_urls):
        assert pid == PID
        assert (statements_dir / f"{PID}.json").is_file()
        assert blog_limit == 0
        assert excluded_tutorial_urls == frozenset()
        return _agent_result(URL1, _long_text("agent editorial "))

    _patch_agent(monkeypatch, fake_agent)
    monkeypatch.setattr(
        ep, "translate_editorial_to_zh",
        AsyncMock(return_value=("自动抓取后的译文。" * 12, "", True)),
    )

    await prefetch_editorial_zh(PID)

    assert not await is_no_official_editorial(PID)
    assert await has_cached_editorial_zh(PID)
    editorial = await get_verified_official_editorial(PID)
    assert editorial is not None
    assert editorial.text.startswith("agent editorial ")


async def test_prefetch_marks_only_completed_no_match(bound_db, cfg, monkeypatch):
    await _seed_problem(bound_db, cfg)

    async def no_match(**_kwargs):
        raise _FakeAgentNoMatch("complete search found no matching editorial")

    _patch_agent(monkeypatch, no_match)
    await prefetch_editorial_zh(PID)
    assert await is_no_official_editorial(PID)


async def test_prefetch_incomplete_attempt_remains_retryable(bound_db, cfg, monkeypatch):
    await _seed_problem(bound_db, cfg)

    async def incomplete(**_kwargs):
        raise _FakeAgentIncomplete("deadline exceeded")

    _patch_agent(monkeypatch, incomplete)
    await prefetch_editorial_zh(PID)
    assert not await is_no_official_editorial(PID)
    assert not await has_cached_editorial_zh(PID)


async def test_prefetch_scrape_error_remains_retryable(bound_db, cfg, monkeypatch):
    await _seed_problem(bound_db, cfg)

    async def scrape_failed(**_kwargs):
        raise _FakeScrapeError("challenge page")

    _patch_agent(monkeypatch, scrape_failed)
    await prefetch_editorial_zh(PID)
    assert not await is_no_official_editorial(PID)
    assert not await has_cached_editorial_zh(PID)


async def test_prefetch_cancellation_remains_incomplete(bound_db, cfg, monkeypatch):
    await _seed_problem(bound_db, cfg)
    started = asyncio.Event()

    async def blocked(**_kwargs):
        started.set()
        await asyncio.Event().wait()

    _patch_agent(monkeypatch, blocked)
    task = asyncio.create_task(prefetch_editorial_zh(PID))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not await is_no_official_editorial(PID)
    assert not await has_cached_editorial_zh(PID)


async def test_short_translation_cannot_become_verified(bound_db, cfg, monkeypatch):
    await _seed_problem(bound_db, cfg)

    async def fake_agent(**_kwargs):
        return _agent_result(URL1, _long_text("official-"))

    _patch_agent(monkeypatch, fake_agent)
    monkeypatch.setattr(
        ep, "translate_editorial_to_zh", AsyncMock(return_value=("太短", "", True)),
    )
    await prefetch_editorial_zh(PID)
    assert not await has_cached_editorial_zh(PID)
    assert not await is_no_official_editorial(PID)


async def test_commit_refuses_stale_statement_fingerprint(bound_db, cfg, monkeypatch):
    await _seed_problem(bound_db, cfg)
    body = _long_text("stale-")
    prepared = ep.PreparedEditorial(
        bundle=_bundle(URL1, body),
        editorial=OfficialEditorial(text=body, tutorial_url=URL1, tutorial_title="Editorial"),
        translated_text=(" stale 译文。" * 20),
        statement_sha256="0" * 64,  # does not match the current statement
    )
    monkeypatch.setattr(
        tutorials, "prepare_editorial",
        AsyncMock(return_value=ep.EditorialPreparationResult(
            ep.EditorialPreparationStatus.READY, prepared=prepared,
        )),
    )
    await prefetch_editorial_zh(PID)
    assert not await has_cached_editorial_zh(PID)
    assert await get_editorial_state(PID) == "incomplete"


async def test_commit_strips_thinking_and_appends_model_tag(bound_db, cfg, monkeypatch):
    await _seed_problem(bound_db, cfg)

    async def fake_agent(**_kwargs):
        return _agent_result(URL1, _long_text("tagged-"))

    _patch_agent(monkeypatch, fake_agent)
    monkeypatch.setattr(
        ep, "translate_editorial_to_zh",
        AsyncMock(return_value=(
            "<thinking>secret</thinking>" + ("带标签译文。" * 20), "『M』", True,
        )),
    )
    await prefetch_editorial_zh(PID)
    assert await has_cached_editorial_zh(PID)
    zh = await load_cached_editorial_zh(PID)
    assert zh.endswith("『M』")
    assert "secret" not in zh


def test_tutorial_agent_importable_from_runtime():
    AgentNoMatch, AgentIncomplete, ScrapeError, run_agent_for_pid = (
        ep._load_tutorial_agent()
    )
    assert issubclass(AgentNoMatch, Exception)
    assert issubclass(AgentIncomplete, Exception)
    assert issubclass(ScrapeError, Exception)
    assert run_agent_for_pid.__name__ == "run_agent_for_pid"


# --- review formatting -----------------------------------------------------------


def test_format_editorial_for_review():
    editorial = OfficialEditorial(
        text="Use greedy sorting.", tutorial_url=URL1, tutorial_title="T",
    )
    text = format_editorial_for_review(editorial)
    assert text.startswith("官方题解（仅你可见，群友不知道）：")
    assert "Use greedy sorting." in text
    assert text.endswith(f"来源：{URL1}")


def test_format_editorial_for_review_truncates_long_body():
    editorial = OfficialEditorial(text="x" * 20000, tutorial_url="", tutorial_title="")
    text = format_editorial_for_review(editorial)
    assert "x" * 12000 in text
    assert "...(题解已截断)" in text
    assert "x" * 12001 not in text
    assert "来源" not in text