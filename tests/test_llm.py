import asyncio
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.config import BotConfig
from kouhai_bot.llm import (
    ChatCompletionResult,
    _ChatCompletionAttempt,
    _apply_rating_gate,
    _read_streaming_chat_completion,
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
    smart, _general = build_provider_queues_from_yaml(_queues([
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


class _ScriptedContent:
    """Async line stream over a (delay, line) script, mimicking resp.content."""

    def __init__(self, script):
        self._script = list(script)

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for delay, line in self._script:
            if delay:
                await asyncio.sleep(delay)
            yield line


class _ScriptedResponse:
    def __init__(self, script):
        self.content = _ScriptedContent(script)


def _delta_event(text):
    payload = json.dumps({"choices": [{"delta": {"content": text}}]})
    return [f"data: {payload}\n".encode(), b"\n"]


@pytest.mark.asyncio
async def test_stream_progress_watchdog_passes_healthy_stream():
    script = []
    for chunk in ("O", "K"):
        script += [(0.15, line) for line in _delta_event(chunk)]
    finish = json.dumps(
        {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"total_tokens": 2}}
    )
    script += [(0.15, f"data: {finish}\n".encode()), (0.15, b"\n")]
    script.append((0.15, b"data: [DONE]\n"))

    result = await _read_streaming_chat_completion(
        _ScriptedResponse(script),
        provider_name="fake",
        progress_timeout_sec=0.5,
    )

    assert result.text == "OK"
    assert result.retryable is False


@pytest.mark.asyncio
async def test_stream_progress_watchdog_trips_on_heartbeat_masked_dead_stream(caplog):
    script = [(0.0, line) for line in _delta_event("A")]
    for _ in range(20):
        script.append((0.05, b": ping\n"))
        script.append((0.05, b"\n"))
        script.append((0.05, b'data: {"choices":[{"delta":{}}]}\n'))
        script.append((0.05, b"\n"))

    started = time.monotonic()
    with caplog.at_level("WARNING", logger="kouhai-bot.llm"):
        result = await _read_streaming_chat_completion(
            _ScriptedResponse(script),
            provider_name="fake",
            progress_timeout_sec=0.3,
        )
    elapsed = time.monotonic() - started

    assert result.text is None
    assert result.retryable is True
    assert result.failure_kind == "service_unavailable"
    assert any(
        "no meaningful progress" in record.getMessage()
        for record in caplog.records
    )
    assert 0.25 <= elapsed < 2.0


@pytest.mark.asyncio
async def test_stream_progress_watchdog_trips_on_total_silence(caplog):
    script = [(0.0, line) for line in _delta_event("A")]
    script.append((10.0, b"data: [DONE]\n"))

    with caplog.at_level("WARNING", logger="kouhai-bot.llm"):
        result = await _read_streaming_chat_completion(
            _ScriptedResponse(script),
            provider_name="fake",
            progress_timeout_sec=0.25,
        )

    assert result.text is None
    assert result.retryable is True
    assert any(
        "no meaningful progress" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_stream_progress_watchdog_disabled_accepts_quiet_stream():
    script = [(0.0, line) for line in _delta_event("A")]
    script.append((0.6, b"\n"))
    finish = json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    script.append((0.0, f"data: {finish}\n".encode()))
    script.append((0.0, b"\n"))
    script.append((0.0, b"data: [DONE]\n"))

    result = await _read_streaming_chat_completion(
        _ScriptedResponse(script),
        provider_name="fake",
        progress_timeout_sec=0,
    )

    assert result.text == "A"
