import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.config import BotConfig
from kouhai_bot.llm import (
    ChatCompletionResult,
    _ChatCompletionAttempt,
    _apply_rating_gate,
    chat_completion,
)
from kouhai_bot.llm_config import LlmProviderConfig, build_provider_queues_from_yaml


def _provider(name: str, *, min_rating=None, max_rating=None) -> LlmProviderConfig:
    return LlmProviderConfig(
        name=name,
        api_key=f"key-{name}",
        base_url="http://localhost/v1",
        model=f"model-{name}",
        min_rating=min_rating,
        max_rating=max_rating,
    )


def _queues(smart_model):
    return {
        "smart_model": smart_model,
        "general_model": [{"name": "general", "api_key": "key", "model": "general"}],
    }


def test_provider_rating_bounds_parse_from_yaml():
    smart, _general, _multimodal = build_provider_queues_from_yaml(_queues([
        {
            "name": "bounded",
            "api_key": "key",
            "model": "smart",
            "min_rating": "2200",
            "max_rating": 2800,
        }
    ]))

    assert smart[0].min_rating == 2200
    assert smart[0].max_rating == 2800


@pytest.mark.parametrize("field", ["min_rating", "max_rating"])
def test_invalid_provider_rating_bound_raises(field):
    raw = {
        "name": "bounded",
        "api_key": "key",
        "model": "smart",
        field: "not-an-int",
    }

    with pytest.raises(RuntimeError, match=r"bounded.*smart_model.*invalid"):
        build_provider_queues_from_yaml(_queues([raw]))


def test_provider_rating_min_must_not_exceed_max():
    with pytest.raises(RuntimeError, match=r"bounded.*smart_model.*min_rating"):
        build_provider_queues_from_yaml(_queues([
            {
                "name": "bounded",
                "api_key": "key",
                "model": "smart",
                "min_rating": 2800,
                "max_rating": 2200,
            }
        ]))


@pytest.mark.parametrize(
    ("rating", "expected"),
    [
        (1900, ["unset", "upper"]),
        (2200, ["unset", "lower", "upper", "both"]),
        (2600, ["unset", "lower", "upper", "both"]),
        (2900, ["unset", "lower"]),
    ],
)
def test_rating_gate_filters_provider_bounds(rating, expected):
    providers = [
        _provider("unset"),
        _provider("lower", min_rating=2200),
        _provider("upper", max_rating=2800),
        _provider("both", min_rating=2200, max_rating=2800),
    ]

    result = _apply_rating_gate(
        providers,
        rating,
        task_name="judge",
        provider_name_pinned=False,
    )

    assert [provider.name for provider in result] == expected


@pytest.mark.asyncio
async def test_rating_gate_empty_result_falls_back_to_unfiltered_queue(caplog, monkeypatch):
    only = _provider("only", min_rating=2200)
    cfg = BotConfig(
        llm_smart_providers=[only],
        llm_general_providers=[_provider("general")],
        llm_max_retries=0,
    )
    attempts = []

    async def fake_once(_session, **kwargs):
        attempts.append(kwargs["provider_name"])
        return _ChatCompletionAttempt(text="available", retryable=False, retry_after_sec=None)

    monkeypatch.setattr("kouhai_bot.llm.get_config", lambda: cfg)
    monkeypatch.setattr("kouhai_bot.llm.aiohttp.ClientSession", _DummySession)
    monkeypatch.setattr("kouhai_bot.llm._post_chat_completion_once", fake_once)

    with caplog.at_level("WARNING", logger="kouhai-bot.llm"):
        result = await chat_completion(
            [{"role": "user", "content": "judge"}],
            task="judge",
            problem_rating=1800,
        )

    assert result.text == "available"
    assert attempts == ["only"]
    assert any("unfiltered" in record.message for record in caplog.records)


def test_rating_gate_bypass_and_noop_cases():
    providers = [_provider("bounded", min_rating=2200)]

    assert _apply_rating_gate(
        providers,
        1800,
        task_name="judge",
        provider_name_pinned=True,
    ) == providers
    assert _apply_rating_gate(
        providers,
        None,
        task_name="judge",
        provider_name_pinned=False,
    ) == providers
    assert _apply_rating_gate(
        providers,
        1800,
        task_name="clarify",
        provider_name_pinned=False,
    ) == providers


class _DummySession:
    def __init__(self, *_args, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_chat_completion_skips_out_of_range_provider_without_attempt(monkeypatch):
    first = _provider("first", min_rating=2200)
    second = _provider("second", max_rating=2000)
    cfg = BotConfig(
        llm_smart_providers=[first, second],
        llm_general_providers=[_provider("general")],
        llm_max_retries=0,
    )
    attempts = []

    async def fake_once(_session, **kwargs):
        attempts.append(kwargs["provider_name"])
        return _ChatCompletionAttempt(
            text="second response",
            retryable=False,
            retry_after_sec=None,
        )

    monkeypatch.setattr("kouhai_bot.llm.get_config", lambda: cfg)
    monkeypatch.setattr("kouhai_bot.llm.aiohttp.ClientSession", _DummySession)
    monkeypatch.setattr("kouhai_bot.llm._post_chat_completion_once", fake_once)

    result = await chat_completion(
        [{"role": "user", "content": "judge"}],
        task="judge",
        problem_rating=2000,
    )

    assert isinstance(result, ChatCompletionResult)
    assert result.text == "second response"
    assert attempts == ["second"]
