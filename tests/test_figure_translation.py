"""Marker-preserving translation for editorial figures."""

from types import SimpleNamespace

import pytest

from kouhai_bot.config import BotConfig
from kouhai_bot.handlers import shared


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    config = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.config._config", config)
    return config


def _reply(text: str, tag: str = "『M』") -> SimpleNamespace:
    return SimpleNamespace(text=text, model_tag=tag)


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