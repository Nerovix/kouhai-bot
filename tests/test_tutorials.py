"""Tests for official tutorial extraction."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from unittest.mock import AsyncMock, patch

from kouhai_bot.tutorials import (
    MIN_EDITORIAL_LEN,
    OfficialEditorial,
    extract_editorial,
    get_editorial_zh_for_group,
    get_official_editorial,
    has_cached_editorial_zh,
    is_no_official_editorial,
    mark_no_official_editorial,
    prefetch_editorial_zh,
)


def _long_text(prefix: str = "x") -> str:
    return prefix * (MIN_EDITORIAL_LEN + 10)


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

    async def _run():
        with patch(
            "kouhai_bot.tutorials.translate_editorial_to_zh",
            AsyncMock(return_value=("中文题解译文。" * 20, "", True)),
        ):
            return await get_editorial_zh_for_group(editorial, "542D")

    zh, _tag = asyncio.run(_run())
    assert zh is not None
    assert zh.startswith("中文题解")
    assert (tmp_path / "tutorial_translations" / "542D.txt").is_file()
    assert (tmp_path / "tutorial_translations" / "542D.verified").is_file()


def test_get_editorial_zh_for_group_marks_mismatched_editorial_missing(tmp_path, monkeypatch):
    import json
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)
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
            "kouhai_bot.tutorials.translate_editorial_to_zh",
            AsyncMock(return_value=(None, "", False)),
        ):
            return await get_editorial_zh_for_group(editorial, "542D")

    zh, tag = asyncio.run(_run())
    assert zh is None
    assert tag == ""
    assert is_no_official_editorial("542D")
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
    translate = AsyncMock(return_value=("新缓存译文。" * 20, "", True))

    async def _run():
        with patch("kouhai_bot.tutorials.translate_editorial_to_zh", translate):
            return await get_editorial_zh_for_group(editorial, "317C")

    zh, tag = asyncio.run(_run())
    assert zh is not None
    assert zh.startswith("新缓存译文")
    assert tag == ""
    translate.assert_awaited_once()
    assert has_cached_editorial_zh("317C")
    assert (cache_dir / "317C.verified").is_file()


def test_prefetch_editorial_zh_recovers_after_rescrape(tmp_path, monkeypatch):
    import json
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)
    mark_no_official_editorial("542D")
    tutorials_dir = tmp_path / "tutorials"
    tutorials_dir.mkdir()
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
            "kouhai_bot.tutorials.translate_editorial_to_zh",
            AsyncMock(return_value=("恢复后的译文。" * 20, "", True)),
        ):
            await prefetch_editorial_zh("542D")

    asyncio.run(_run())
    assert not is_no_official_editorial("542D")
    assert has_cached_editorial_zh("542D")


def test_prefetch_editorial_zh_marks_missing(tmp_path, monkeypatch):
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)

    async def _run():
        await prefetch_editorial_zh("999Z")

    asyncio.run(_run())
    assert is_no_official_editorial("999Z")


def test_prefetch_editorial_zh_caches_translation(tmp_path, monkeypatch):
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)
    tutorials_dir = tmp_path / "tutorials"
    tutorials_dir.mkdir()
    body = _long_text("prefetch-")
    import json
    (tutorials_dir / "542D.json").write_text(
        json.dumps({
            "tutorial_url": "https://example.com/e",
            "sections": [{"hint": "", "solution": body, "raw_text": body, "code_blocks": []}],
        }),
        encoding="utf-8",
    )

    async def _run():
        with patch(
            "kouhai_bot.tutorials.translate_editorial_to_zh",
            AsyncMock(return_value=("预取译文 \\(x \\le n\\)。" * 15, "", True)),
        ):
            await prefetch_editorial_zh("542D")

    asyncio.run(_run())
    assert has_cached_editorial_zh("542D")
    cached = (tmp_path / "tutorial_translations" / "542D.txt").read_text(encoding="utf-8")
    assert cached.startswith("预取译文")


def test_deliver_uses_prefetch_cache_without_translate(tmp_path, monkeypatch):
    from kouhai_bot.config import BotConfig
    from kouhai_bot.editorial_followup import deliver_official_tutorial_forward

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)
    monkeypatch.setattr("kouhai_bot.editorial_followup.get_config", lambda: cfg)
    cache_dir = tmp_path / "tutorial_translations"
    cache_dir.mkdir()
    (cache_dir / "542D.txt").write_text("预取译文。" * 20, encoding="utf-8")
    (cache_dir / "542D.verified").write_text("", encoding="utf-8")

    async def _fail_translate(*args, **kwargs):
        raise AssertionError("translate should not run when cache is warm")

    async def _run():
        with patch("kouhai_bot.tutorials.translate_editorial_to_zh", _fail_translate), \
                patch("kouhai_bot.editorial_followup.send_private_msg", AsyncMock(return_value=1)), \
                patch("kouhai_bot.editorial_followup.send_group_forward_msg", AsyncMock(return_value=99)):
            await deliver_official_tutorial_forward(
                1,
                "542D",
                OfficialEditorial(text="x", tutorial_url="", tutorial_title=""),
            )

    asyncio.run(_run())


def test_run_post_solve_skips_prefetch_wait_when_cache_warm(tmp_path, monkeypatch):
    from kouhai_bot.config import BotConfig

    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.tutorials.get_config", lambda: cfg)
    monkeypatch.setattr("kouhai_bot.editorial_followup.get_config", lambda: cfg)
    cache_dir = tmp_path / "tutorial_translations"
    cache_dir.mkdir()
    (cache_dir / "542D.txt").write_text("预取译文。" * 20, encoding="utf-8")
    (cache_dir / "542D.verified").write_text("", encoding="utf-8")

    async def _fail_await(pid):
        raise AssertionError("should not wait for prefetch when cache is warm")

    async def _run():
        with patch("kouhai_bot.editorial_followup._await_prefetch_if_running", _fail_await), \
                patch("kouhai_bot.editorial_followup.send_private_msg", AsyncMock(return_value=1)), \
                patch("kouhai_bot.editorial_followup.send_group_forward_msg", AsyncMock(return_value=99)):
            from kouhai_bot.editorial_followup import run_post_solve_editorial_followup
            await run_post_solve_editorial_followup(999, "542D")

    asyncio.run(_run())
