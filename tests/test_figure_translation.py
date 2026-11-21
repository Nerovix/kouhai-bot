"""Marker-preserving translation for statement/editorial figures."""

from types import SimpleNamespace

import pytest

from kouhai_bot.config import BotConfig
from kouhai_bot.handlers import shared

PASSAGE = "前文。\n\n[[FIG_1]]\n\n后文。"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    config = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.config._config", config)
    return config


def _reply(text: str, tag: str = "『M』") -> SimpleNamespace:
    return SimpleNamespace(text=text, model_tag=tag)


async def test_translate_figure_passage_keeps_markers(cfg, monkeypatch):
    async def fake(_messages, **_kwargs):
        return _reply("前文译文。\n\n[[FIG_1]]\n\n后文译文。")

    monkeypatch.setattr(shared, "call_chat_completion_result", fake)
    text, tag = await shared.translate_figure_passage(PASSAGE)
    assert text == "前文译文。\n\n[[FIG_1]]\n\n后文译文。"
    assert tag == "『M』"


async def test_translate_figure_passage_falls_back_to_segments(cfg, monkeypatch):
    calls = []

    async def fake(messages, **_kwargs):
        user = messages[-1]["content"]
        calls.append(user)
        if "[[FIG_1]]" in user:
            return _reply("丢了占位符")
        if "前文。" in user:
            return _reply("前文译文。")
        return _reply("后文译文。")

    monkeypatch.setattr(shared, "call_chat_completion_result", fake)
    text, _tag = await shared.translate_figure_passage(PASSAGE)
    assert text == "前文译文。\n\n[[FIG_1]]\n\n后文译文。"
    assert len(calls) == 3


async def test_translate_sample_notes_keeps_figure_markers(cfg, monkeypatch):
    async def fake(_messages, **_kwargs):
        return _reply("第一个测试的树：\n\n[[FIG_1]]\n\n接着说明。")

    monkeypatch.setattr(shared, "call_chat_completion_result", fake)
    text, tag = await shared.translate_sample_notes(
        "The tree in the first test:\n\n[[FIG_1]]\n\nThen explain."
    )
    assert text == "第一个测试的树：\n\n[[FIG_1]]\n\n接着说明。"
    assert tag == "『M』"


async def test_translate_sample_notes_repairs_lost_markers(cfg, monkeypatch):
    calls = []

    async def fake(messages, **_kwargs):
        user = messages[-1]["content"]
        calls.append(user)
        if "[[FIG_1]]" in user:
            return _reply("丢了占位符")
        if "第一段。" in user:
            return _reply("第一段译文。")
        return _reply("第二段译文。")

    monkeypatch.setattr(shared, "call_chat_completion_result", fake)
    text, _tag = await shared.translate_sample_notes("第一段。\n\n[[FIG_1]]\n\n第二段。")
    assert text == "第一段译文。\n\n[[FIG_1]]\n\n第二段译文。"
    assert len(calls) == 3


async def test_translate_sample_notes_without_markers_unchanged(cfg, monkeypatch):
    async def fake(_messages, **_kwargs):
        return _reply("普通样例解释。")

    monkeypatch.setattr(shared, "call_chat_completion_result", fake)
    text, tag = await shared.translate_sample_notes("Plain note.")
    assert text == "普通样例解释。"
    assert tag == "『M』"


async def test_translate_editorial_repairs_lost_markers(cfg, monkeypatch):
    body = "思路如下。\n\n[[FIG_1]]\n\n复杂度 O(n)。"

    async def fake_completion(messages, **_kwargs):
        user = messages[-1]["content"]
        if user.strip().startswith("{"):
            return _reply('{"matched":"yes","result":"思路如下。复杂度 O(n)。"}')
        if "思路如下" in user:
            return _reply("思路译文。")
        return _reply("复杂度译文。")

    async def fake_parse(*_args, **_kwargs):
        return {"matched": "yes", "result": "思路如下。复杂度 O(n)。"}, ""

    monkeypatch.setattr(shared, "call_chat_completion_result", fake_completion)
    monkeypatch.setattr(shared, "parse_json_with_llm_repair", fake_parse)

    text, tag, matched = await shared.translate_editorial_to_zh(body, pid="1A")
    assert matched is True
    assert tag == "『M』"
    assert "[[FIG_1]]" in text
    assert "复杂度译文。" in text