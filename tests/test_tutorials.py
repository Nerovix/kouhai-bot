"""Tests for official tutorial extraction."""

import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from unittest.mock import AsyncMock, patch

from kouhai_bot.tutorials import (
    MIN_EDITORIAL_LEN,
    OfficialEditorial,
    extract_editorial,
    get_editorial_zh_for_group,
    get_official_editorial,
    has_cached_editorial_zh,
    ensure_tutorial_json,
    is_no_official_editorial,
    mark_no_official_editorial,
    prefetch_editorial_zh,
)


class _FakeAgentNoMatch(Exception):
    pass


class _FakeAgentIncomplete(Exception):
    pass


class _FakeScrapeError(Exception):
    pass


def _long_text(prefix: str = "x") -> str:
    return prefix * (MIN_EDITORIAL_LEN + 10)


def _write_statement(tmp_path, pid: str) -> None:
    statements_dir = tmp_path / "statements"
    statements_dir.mkdir(exist_ok=True)
    statement_path = statements_dir / f"{pid}.json"
    if not statement_path.exists():
        statement_path.write_text(
            json.dumps({"description": f"statement for {pid}", "images": []}),
            encoding="utf-8",
        )


def _write_tutorial_for_editorial(tmp_path, pid: str, editorial: OfficialEditorial) -> None:
    _write_statement(tmp_path, pid)
    tutorials_dir = tmp_path / "tutorials"
    tutorials_dir.mkdir(exist_ok=True)
    (tutorials_dir / f"{pid}.json").write_text(
        json.dumps(
            {
                "tutorial_url": editorial.tutorial_url,
                "tutorial_title": editorial.tutorial_title,
                "sections": [
                    {
                        "hint": "",
                        "solution": editorial.text,
                        "raw_text": editorial.text,
                        "code_blocks": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _write_verified_cache(
    tmp_path,
    pid: str,
    editorial: OfficialEditorial,
    text: str = "已验证中文题解。" * 20,
) -> None:
    from kouhai_bot.tutorials import _save_cached_translation

    _write_statement(tmp_path, pid)
    _write_tutorial_for_editorial(tmp_path, pid, editorial)
    _save_cached_translation(pid, text, editorial)


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
    assert "Arpa's solution" not in text or text != "Arpa's solution"
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


def test_get_official_editorial_none_when_missing_file(tmp_path, monkeypatch):
    import json
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)
    assert get_official_editorial("999Z") is None

    tutorials_dir = tmp_path / "tutorials"
    tutorials_dir.mkdir()
    body = _long_text("loaded-")
    (tutorials_dir / "542D.json").write_text(
        json.dumps({
            "tutorial_url": "https://example.com/editorial",
            "tutorial_title": "T",
            "sections": [{
                "hint": "",
                "solution": body,
                "raw_text": body,
                "code_blocks": [],
            }],
        }),
        encoding="utf-8",
    )
    editorial = get_official_editorial("542D")
    assert editorial is not None
    assert editorial.text.startswith("loaded-")
    assert editorial.tutorial_url == "https://example.com/editorial"


def test_get_editorial_zh_for_group_uses_translation(tmp_path, monkeypatch):
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)

    body = _long_text("english-")
    editorial = OfficialEditorial(
        text=body,
        tutorial_url="https://example.com/e",
        tutorial_title="T",
    )
    _write_tutorial_for_editorial(tmp_path, "542D", editorial)

    async def _run():
        with patch(
            "kouhai_bot.editorial_preparation.translate_editorial_to_zh",
            AsyncMock(return_value=("中文题解译文。" * 20, "", True)),
        ):
            return await get_editorial_zh_for_group(editorial, "542D")

    zh, _tag = asyncio.run(_run())
    assert zh is not None
    assert zh.startswith("中文题解")
    assert (tmp_path / "tutorial_translations" / "542D.txt").is_file()
    assert (tmp_path / "tutorial_translations" / "542D.verified").is_file()


def test_get_editorial_zh_for_group_keeps_single_mismatch_non_terminal(tmp_path, monkeypatch):
    import json
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)
    _write_statement(tmp_path, "542D")
    tutorials_dir = tmp_path / "tutorials"
    tutorials_dir.mkdir()
    body = _long_text("wrong-editorial-")
    (tutorials_dir / "542D.json").write_text(
        json.dumps({
            "tutorial_url": "https://example.com/e",
            "tutorial_title": "Wrong",
            "sections": [{"hint": "", "solution": body, "raw_text": body, "code_blocks": []}],
        }),
        encoding="utf-8",
    )
    editorial = OfficialEditorial(text=body, tutorial_url="https://example.com/e", tutorial_title="Wrong")

    async def _run():
        with patch(
            "kouhai_bot.editorial_preparation.translate_editorial_to_zh",
            AsyncMock(return_value=(None, "", False)),
        ):
            return await get_editorial_zh_for_group(editorial, "542D")

    zh, tag = asyncio.run(_run())
    assert zh is None
    assert tag == ""
    assert not is_no_official_editorial("542D")
    editorial_after_marker = get_official_editorial("542D")
    assert editorial_after_marker is not None
    assert editorial_after_marker.text.startswith("wrong-editorial-")
    assert not (tmp_path / "tutorial_translations" / "542D.txt").exists()


def test_unverified_editorial_translation_cache_is_not_warm(tmp_path, monkeypatch):
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)
    cache_dir = tmp_path / "tutorial_translations"
    cache_dir.mkdir()
    (cache_dir / "317C.txt").write_text("旧缓存译文。" * 20, encoding="utf-8")

    assert not has_cached_editorial_zh("317C")


def test_verified_cache_is_bound_to_the_persisted_editorial_source(
    tmp_path,
    monkeypatch,
):
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)
    original = OfficialEditorial(
        text=_long_text("original-"),
        tutorial_url="https://example.com/e",
        tutorial_title="T",
    )
    _write_verified_cache(tmp_path, "542D", original)
    assert has_cached_editorial_zh("542D")

    replacement = OfficialEditorial(
        text=_long_text("replacement-"),
        tutorial_url=original.tutorial_url,
        tutorial_title=original.tutorial_title,
    )
    _write_tutorial_for_editorial(tmp_path, "542D", replacement)

    assert not has_cached_editorial_zh("542D")


def test_verified_cache_is_bound_to_the_problem_statement(
    tmp_path,
    monkeypatch,
):
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)
    editorial = OfficialEditorial(
        text=_long_text("source-"),
        tutorial_url="https://example.com/e",
        tutorial_title="T",
    )
    _write_verified_cache(tmp_path, "542D", editorial)
    assert has_cached_editorial_zh("542D")

    (tmp_path / "statements" / "542D.json").write_text(
        json.dumps({"description": "corrected statement", "images": []}),
        encoding="utf-8",
    )
    assert not has_cached_editorial_zh("542D")


def test_get_editorial_zh_for_group_revalidates_unverified_cache(tmp_path, monkeypatch):
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)
    cache_dir = tmp_path / "tutorial_translations"
    cache_dir.mkdir()
    (cache_dir / "317C.txt").write_text("旧缓存译文。" * 20, encoding="utf-8")
    editorial = OfficialEditorial(
        text=_long_text("english-"),
        tutorial_url="https://example.com/e",
        tutorial_title="T",
    )
    _write_tutorial_for_editorial(tmp_path, "317C", editorial)
    translate = AsyncMock(return_value=("新缓存译文。" * 20, "", True))

    async def _run():
        with patch(
            "kouhai_bot.editorial_preparation.translate_editorial_to_zh",
            translate,
        ):
            return await get_editorial_zh_for_group(editorial, "317C")

    zh, tag = asyncio.run(_run())
    assert zh is not None
    assert zh.startswith("新缓存译文")
    assert tag == ""
    translate.assert_awaited_once()
    assert has_cached_editorial_zh("317C")
    assert (cache_dir / "317C.verified").is_file()


def test_get_editorial_zh_for_group_strips_verified_cache_thinking_tags(tmp_path, monkeypatch):
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)
    editorial = OfficialEditorial(
        text=_long_text("english-"),
        tutorial_url="https://example.com/e",
        tutorial_title="T",
    )
    _write_verified_cache(
        tmp_path,
        "542D",
        editorial,
        "<thinking>hidden cached editorial</thinking>" + ("中文题解。" * 20),
    )
    translate = AsyncMock(return_value=("不应重新翻译。" * 20, "", True))

    async def _run():
        with patch(
            "kouhai_bot.editorial_preparation.translate_editorial_to_zh",
            translate,
        ):
            return await get_editorial_zh_for_group(editorial, "542D")

    zh, tag = asyncio.run(_run())
    assert zh is not None
    assert "中文题解" in zh
    assert "hidden cached editorial" not in zh
    assert "thinking" not in zh
    assert tag == ""
    translate.assert_not_awaited()


def test_prefetch_editorial_zh_recovers_after_rescrape(tmp_path, monkeypatch):
    import json
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)
    marker_dir = tmp_path / "tutorial_translations"
    marker_dir.mkdir()
    (marker_dir / "542D.no_editorial").write_text(
        json.dumps(
            {
                "format_version": 1,
                "status": "no_editorial",
                "reason": "old_translation_mismatch",
            }
        ),
        encoding="utf-8",
    )
    tutorials_dir = tmp_path / "tutorials"
    tutorials_dir.mkdir()
    _write_statement(tmp_path, "542D")
    body = _long_text("rescued-")
    (tutorials_dir / "542D.json").write_text(
        json.dumps({
            "tutorial_url": "https://example.com/e",
            "sections": [{"hint": "", "solution": body, "raw_text": body, "code_blocks": []}],
        }),
        encoding="utf-8",
    )

    async def _run():
        with patch(
            "kouhai_bot.editorial_preparation.translate_editorial_to_zh",
            AsyncMock(return_value=("恢复后的译文。" * 20, "", True)),
        ):
            await prefetch_editorial_zh("542D")

    asyncio.run(_run())
    assert not is_no_official_editorial("542D")
    assert has_cached_editorial_zh("542D")


def test_legacy_no_editorial_marker_is_not_a_trusted_terminal_state(
    tmp_path,
    monkeypatch,
):
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)
    marker_dir = tmp_path / "tutorial_translations"
    marker_dir.mkdir()
    statement_dir = tmp_path / "statements"
    statement_dir.mkdir()
    (statement_dir / "542D.json").write_text(
        json.dumps({"description": "statement", "images": []}),
        encoding="utf-8",
    )
    (marker_dir / "542D.no_editorial").write_text("", encoding="utf-8")

    assert not is_no_official_editorial("542D")

    mark_no_official_editorial("542D", reason="agent_no_match:test")
    assert is_no_official_editorial("542D")


def test_no_editorial_marker_is_bound_to_the_problem_statement(
    tmp_path,
    monkeypatch,
):
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)
    _write_statement(tmp_path, "542D")

    mark_no_official_editorial("542D", reason="agent_exhaustive_no_match:test")
    assert is_no_official_editorial("542D")

    (tmp_path / "statements" / "542D.json").write_text(
        json.dumps({"description": "corrected statement", "images": []}),
        encoding="utf-8",
    )
    assert not is_no_official_editorial("542D")


def test_no_editorial_marker_requires_completed_failure_reason(
    tmp_path,
    monkeypatch,
):
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)
    _write_statement(tmp_path, "542D")

    with pytest.raises(ValueError, match="reason missing"):
        mark_no_official_editorial("542D", reason="")
    assert not is_no_official_editorial("542D")


def test_short_translation_cannot_create_verified_marker(tmp_path, monkeypatch):
    from kouhai_bot.config import BotConfig
    from kouhai_bot.tutorials import _save_cached_translation

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)
    editorial = OfficialEditorial(
        text=_long_text("official-"),
        tutorial_url="https://example.com/editorial",
        tutorial_title="Editorial",
    )
    _write_statement(tmp_path, "542D")
    _write_tutorial_for_editorial(tmp_path, "542D", editorial)

    assert not _save_cached_translation("542D", "too short", editorial)
    assert not has_cached_editorial_zh("542D")
    assert not (
        tmp_path / "tutorial_translations" / "542D.verified"
    ).exists()


def test_prefetch_editorial_zh_without_statement_stays_incomplete(tmp_path, monkeypatch):
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)

    async def _run():
        await prefetch_editorial_zh("999Z")

    asyncio.run(_run())
    assert not is_no_official_editorial("999Z")


def test_prefetch_editorial_zh_no_agent_leaves_missing_unknown(tmp_path, monkeypatch):
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)

    async def _run():
        with patch(
            "kouhai_bot.tutorials.ensure_tutorial_json",
            AsyncMock(side_effect=AssertionError("agent should not run")),
        ), patch(
            "kouhai_bot.editorial_preparation.translate_editorial_to_zh",
            AsyncMock(side_effect=AssertionError("translate should not run")),
        ):
            await prefetch_editorial_zh("999Z", run_agent=False)

    asyncio.run(_run())
    assert not is_no_official_editorial("999Z")
    assert not (tmp_path / "tutorial_translations" / "999Z.no_editorial").exists()


def test_tutorial_agent_importable_from_runtime():
    from kouhai_bot.tutorials import _load_tutorial_agent

    AgentNoMatch, AgentIncomplete, ScrapeError, run_agent_for_pid = (
        _load_tutorial_agent()
    )
    assert issubclass(AgentNoMatch, Exception)
    assert issubclass(AgentIncomplete, Exception)
    assert issubclass(ScrapeError, Exception)
    assert run_agent_for_pid.__name__ == "run_agent_for_pid"


def test_prefetch_editorial_zh_runs_agent_before_translate(tmp_path, monkeypatch):
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)
    statements_dir = tmp_path / "statements"
    statements_dir.mkdir()
    (statements_dir / "542D.json").write_text(
        json.dumps({"name": "P", "description": "D", "input": "I", "output": "O"}),
        encoding="utf-8",
    )
    async def _fake_run_agent_for_pid(
        *,
        pid,
        statements_dir,
        blog_limit,
        excluded_tutorial_urls,
    ):
        assert pid == "542D"
        assert (statements_dir / "542D.json").is_file()
        assert blog_limit == 0
        assert excluded_tutorial_urls == frozenset()
        body = _long_text("agent editorial ")
        return SimpleNamespace(
            bundle={
                "tutorial_url": "https://codeforces.com/blog/entry/1",
                "tutorial_title": "Editorial",
                "sections": [
                    {"hint": "", "solution": body, "raw_text": body, "code_blocks": []}
                ],
            },
            selected_candidate_id="c1",
            confidence=0.91,
            elapsed_sec=1.2,
        )

    async def _run():
        with patch(
            "kouhai_bot.editorial_preparation._load_tutorial_agent",
            return_value=(
                _FakeAgentNoMatch,
                _FakeAgentIncomplete,
                _FakeScrapeError,
                _fake_run_agent_for_pid,
            ),
        ), patch(
            "kouhai_bot.editorial_preparation.translate_editorial_to_zh",
            AsyncMock(return_value=("自动抓取后的译文。" * 12, "", True)),
        ):
            await prefetch_editorial_zh("542D")

    asyncio.run(_run())
    assert not is_no_official_editorial("542D")
    assert get_official_editorial("542D") is not None
    assert has_cached_editorial_zh("542D")


def test_prefetch_rejects_wrong_candidate_and_uses_next_blog(tmp_path, monkeypatch):
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)
    statements_dir = tmp_path / "statements"
    statements_dir.mkdir()
    (statements_dir / "542D.json").write_text(
        json.dumps({"description": "statement", "images": []}),
        encoding="utf-8",
    )
    wrong = OfficialEditorial(
        text=_long_text("wrong-"),
        tutorial_url="https://codeforces.com/blog/entry/1",
        tutorial_title="Wrong",
    )
    _write_tutorial_for_editorial(tmp_path, "542D", wrong)
    correct_body = _long_text("correct-")
    agent_calls: list[frozenset[str]] = []

    async def find_alternative(**kwargs):
        excluded = kwargs["excluded_tutorial_urls"]
        agent_calls.append(excluded)
        assert excluded == frozenset({wrong.tutorial_url})
        return SimpleNamespace(
            bundle={
                "tutorial_url": "https://codeforces.com/blog/entry/2",
                "tutorial_title": "Correct",
                "sections": [{
                    "hint": "",
                    "solution": correct_body,
                    "raw_text": correct_body,
                    "code_blocks": [],
                }],
            },
            selected_candidate_id="b2",
            confidence=0.95,
            elapsed_sec=1.0,
        )

    translate = AsyncMock(side_effect=[
        (None, "", False),
        ("正确候选的中文题解。" * 12, "", True),
    ])
    with patch(
        "kouhai_bot.editorial_preparation._load_tutorial_agent",
        return_value=(
            _FakeAgentNoMatch,
            _FakeAgentIncomplete,
            _FakeScrapeError,
            find_alternative,
        ),
    ), patch(
        "kouhai_bot.editorial_preparation.translate_editorial_to_zh",
        translate,
    ):
        asyncio.run(prefetch_editorial_zh("542D"))

    assert agent_calls == [frozenset({wrong.tutorial_url})]
    assert translate.await_count == 2
    assert not is_no_official_editorial("542D")
    editorial = get_official_editorial("542D")
    assert editorial is not None
    assert editorial.tutorial_url.endswith("/2")
    assert has_cached_editorial_zh("542D")


def test_prefetch_mismatch_then_incomplete_search_stays_retryable(
    tmp_path,
    monkeypatch,
):
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)
    statements_dir = tmp_path / "statements"
    statements_dir.mkdir()
    (statements_dir / "542D.json").write_text(
        json.dumps({"description": "statement", "images": []}),
        encoding="utf-8",
    )
    wrong = OfficialEditorial(
        text=_long_text("wrong-"),
        tutorial_url="https://codeforces.com/blog/entry/1",
        tutorial_title="Wrong",
    )
    _write_tutorial_for_editorial(tmp_path, "542D", wrong)

    async def incomplete(**kwargs):
        assert kwargs["excluded_tutorial_urls"] == frozenset({wrong.tutorial_url})
        raise _FakeAgentIncomplete("second blog timed out")

    with patch(
        "kouhai_bot.editorial_preparation._load_tutorial_agent",
        return_value=(
            _FakeAgentNoMatch,
            _FakeAgentIncomplete,
            _FakeScrapeError,
            incomplete,
        ),
    ), patch(
        "kouhai_bot.editorial_preparation.translate_editorial_to_zh",
        AsyncMock(return_value=(None, "", False)),
    ):
        asyncio.run(prefetch_editorial_zh("542D"))

    assert not is_no_official_editorial("542D")
    assert get_official_editorial("542D") is None
    assert not has_cached_editorial_zh("542D")


def test_prefetch_marks_terminal_only_after_mismatch_and_exhaustive_search(
    tmp_path,
    monkeypatch,
):
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)
    statements_dir = tmp_path / "statements"
    statements_dir.mkdir()
    (statements_dir / "542D.json").write_text(
        json.dumps({"description": "statement", "images": []}),
        encoding="utf-8",
    )
    wrong = OfficialEditorial(
        text=_long_text("wrong-"),
        tutorial_url="https://codeforces.com/blog/entry/1",
        tutorial_title="Wrong",
    )
    _write_tutorial_for_editorial(tmp_path, "542D", wrong)

    async def no_remaining_match(**kwargs):
        assert kwargs["excluded_tutorial_urls"] == frozenset({wrong.tutorial_url})
        raise _FakeAgentNoMatch("all remaining blogs explicitly rejected")

    with patch(
        "kouhai_bot.editorial_preparation._load_tutorial_agent",
        return_value=(
            _FakeAgentNoMatch,
            _FakeAgentIncomplete,
            _FakeScrapeError,
            no_remaining_match,
        ),
    ), patch(
        "kouhai_bot.editorial_preparation.translate_editorial_to_zh",
        AsyncMock(return_value=(None, "", False)),
    ):
        asyncio.run(prefetch_editorial_zh("542D"))

    assert is_no_official_editorial("542D")
    assert get_official_editorial("542D") is None
    assert not has_cached_editorial_zh("542D")


def test_prefetch_editorial_zh_marks_only_completed_no_match(tmp_path, monkeypatch):
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)
    statements_dir = tmp_path / "statements"
    statements_dir.mkdir()
    (statements_dir / "542D.json").write_text(
        json.dumps({"description": "statement", "images": []}),
        encoding="utf-8",
    )

    async def no_match(**_kwargs):
        raise _FakeAgentNoMatch("complete search found no matching editorial")

    async def _run():
        with patch(
            "kouhai_bot.editorial_preparation._load_tutorial_agent",
            return_value=(
                _FakeAgentNoMatch,
                _FakeAgentIncomplete,
                _FakeScrapeError,
                no_match,
            ),
        ):
            await prefetch_editorial_zh("542D")

    asyncio.run(_run())
    assert is_no_official_editorial("542D")


def test_prefetch_editorial_zh_incomplete_attempt_remains_retryable(
    tmp_path,
    monkeypatch,
):
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)
    statements_dir = tmp_path / "statements"
    statements_dir.mkdir()
    (statements_dir / "542D.json").write_text(
        json.dumps({"description": "statement", "images": []}),
        encoding="utf-8",
    )

    async def incomplete(**_kwargs):
        raise _FakeAgentIncomplete("deadline exceeded")

    async def _run():
        with patch(
            "kouhai_bot.editorial_preparation._load_tutorial_agent",
            return_value=(
                _FakeAgentNoMatch,
                _FakeAgentIncomplete,
                _FakeScrapeError,
                incomplete,
            ),
        ):
            await prefetch_editorial_zh("542D")

    asyncio.run(_run())
    assert not is_no_official_editorial("542D")
    assert not has_cached_editorial_zh("542D")


def test_prefetch_editorial_zh_cancellation_remains_incomplete(
    tmp_path,
    monkeypatch,
):
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)
    statements_dir = tmp_path / "statements"
    statements_dir.mkdir()
    (statements_dir / "542D.json").write_text(
        json.dumps({"description": "statement", "images": []}),
        encoding="utf-8",
    )
    started = asyncio.Event()

    async def blocked(**_kwargs):
        started.set()
        await asyncio.Event().wait()

    async def _run():
        with patch(
            "kouhai_bot.editorial_preparation._load_tutorial_agent",
            return_value=(
                _FakeAgentNoMatch,
                _FakeAgentIncomplete,
                _FakeScrapeError,
                blocked,
            ),
        ):
            task = asyncio.create_task(prefetch_editorial_zh("542D"))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(_run())
    assert not is_no_official_editorial("542D")
    assert not has_cached_editorial_zh("542D")


def test_ensure_tutorial_json_returns_false_without_statement(tmp_path, monkeypatch):
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)

    async def _run():
        assert await ensure_tutorial_json("999Z") is False

    asyncio.run(_run())


def test_prefetch_editorial_zh_caches_translation(tmp_path, monkeypatch):
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)
    _write_statement(tmp_path, "542D")
    tutorials_dir = tmp_path / "tutorials"
    tutorials_dir.mkdir()
    body = _long_text("prefetch-")
    (tutorials_dir / "542D.json").write_text(
        json.dumps({
            "tutorial_url": "https://example.com/e",
            "sections": [{"hint": "", "solution": body, "raw_text": body, "code_blocks": []}],
        }),
        encoding="utf-8",
    )

    async def _run():
        with patch(
            "kouhai_bot.editorial_preparation.translate_editorial_to_zh",
            AsyncMock(return_value=("预取译文 \\(x \\le n\\)。" * 15, "", True)),
        ):
            await prefetch_editorial_zh("542D")

    asyncio.run(_run())
    assert has_cached_editorial_zh("542D")
    cached = (tmp_path / "tutorial_translations" / "542D.txt").read_text(encoding="utf-8")
    assert cached.startswith("预取译文")


def test_ensure_editorial_prefetch_runs_full_pipeline_and_respects_terminal_state(
    monkeypatch,
):
    from kouhai_bot import editorial_followup

    cached = False
    no_editorial = False
    scheduled: list[tuple[str, bool]] = []

    monkeypatch.setattr(
        editorial_followup,
        "has_cached_editorial_zh",
        lambda _pid: cached,
    )
    monkeypatch.setattr(
        editorial_followup,
        "is_no_official_editorial",
        lambda _pid: no_editorial,
    )
    monkeypatch.setattr(
        editorial_followup,
        "schedule_prefetch_editorial",
        lambda pid, *, run_agent=True: scheduled.append((pid, run_agent)),
    )

    editorial_followup.ensure_editorial_prefetch("542D")
    assert scheduled == [("542D", True)]

    cached = True
    editorial_followup.ensure_editorial_prefetch("542D")
    assert scheduled == [("542D", True)]

    cached = False
    no_editorial = True
    editorial_followup.ensure_editorial_prefetch("542D")
    assert scheduled == [("542D", True)]


def test_startup_resumes_full_prefetch_for_current_problem(tmp_path, monkeypatch):
    from kouhai_bot import editorial_followup
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    state_dir = tmp_path / "groups" / "1"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(
        json.dumps({"today": "542D"}),
        encoding="utf-8",
    )
    scheduled: list[str] = []

    monkeypatch.setattr(editorial_followup, "get_config", lambda: cfg)
    monkeypatch.setattr(
        editorial_followup,
        "ensure_editorial_prefetch",
        scheduled.append,
    )

    editorial_followup.schedule_prefetch_for_group_today(1)

    assert scheduled == ["542D"]


def test_editorial_maintenance_retries_both_current_and_ready_problem(
    tmp_path,
    monkeypatch,
):
    from kouhai_bot import editorial_followup
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    state_dir = tmp_path / "groups" / "1"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(
        json.dumps({"today": "CURRENT"}),
        encoding="utf-8",
    )
    calls: list[str] = []
    retried = asyncio.Event()

    def ensure(pid):
        calls.append(pid)
        if calls.count("CURRENT") >= 2 and calls.count("NEXT") >= 2:
            retried.set()

    async def next_pid():
        return "NEXT"

    monkeypatch.setattr(editorial_followup, "get_config", lambda: cfg)
    monkeypatch.setattr(editorial_followup, "ensure_editorial_prefetch", ensure)

    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(
            editorial_followup.editorial_prefetch_maintenance_loop(
                1,
                get_next_problem_pid=next_pid,
                stop_event=stop,
                interval_seconds=0.01,
            )
        )
        await asyncio.wait_for(retried.wait(), timeout=1)
        stop.set()
        await task

    asyncio.run(run())
    assert calls[:4] == ["CURRENT", "NEXT", "CURRENT", "NEXT"]


def test_post_solve_missing_editorial_does_not_create_terminal_marker(
    tmp_path,
    monkeypatch,
):
    from kouhai_bot import editorial_followup
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)
    monkeypatch.setattr(editorial_followup, "get_config", lambda: cfg)
    monkeypatch.setattr(
        editorial_followup,
        "_await_prefetch_if_running",
        AsyncMock(),
    )

    asyncio.run(editorial_followup.run_post_solve_editorial_followup(1, "542D"))

    assert not is_no_official_editorial("542D")
    assert not has_cached_editorial_zh("542D")


def test_deliver_uses_prefetch_cache_without_translate(tmp_path, monkeypatch):
    from kouhai_bot.config import BotConfig
    from kouhai_bot.editorial_followup import deliver_official_tutorial_forward

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)
    monkeypatch.setattr("kouhai_bot.editorial_followup.get_config", lambda: cfg)
    editorial = OfficialEditorial(
        text=_long_text("source-"),
        tutorial_url="https://example.com/e",
        tutorial_title="T",
    )
    _write_verified_cache(tmp_path, "542D", editorial, "预取译文。" * 20)

    async def _fail_translate(*args, **kwargs):
        raise AssertionError("translate should not run when cache is warm")

    async def _run():
        with patch(
                "kouhai_bot.editorial_preparation.translate_editorial_to_zh",
                _fail_translate,
        ), \
                patch("kouhai_bot.editorial_followup.send_private_msg", AsyncMock(return_value=1)), \
                patch("kouhai_bot.editorial_followup.send_group_forward_msg", AsyncMock(return_value=99)):
            await deliver_official_tutorial_forward(
                1,
                "542D",
                editorial,
            )

    asyncio.run(_run())


def test_run_post_solve_skips_prefetch_wait_when_cache_warm(tmp_path, monkeypatch):
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)
    monkeypatch.setattr("kouhai_bot.editorial_followup.get_config", lambda: cfg)
    editorial = OfficialEditorial(
        text=_long_text("source-"),
        tutorial_url="https://example.com/e",
        tutorial_title="T",
    )
    _write_verified_cache(tmp_path, "542D", editorial, "预取译文。" * 20)

    async def _fail_await(pid):
        raise AssertionError("should not wait for prefetch when cache is warm")

    async def _run():
        with patch("kouhai_bot.editorial_followup._await_prefetch_if_running", _fail_await), \
                patch("kouhai_bot.editorial_followup.send_private_msg", AsyncMock(return_value=1)), \
                patch("kouhai_bot.editorial_followup.send_group_forward_msg", AsyncMock(return_value=99)):
            from kouhai_bot.editorial_followup import run_post_solve_editorial_followup
            await run_post_solve_editorial_followup(999, "542D")

    asyncio.run(_run())
