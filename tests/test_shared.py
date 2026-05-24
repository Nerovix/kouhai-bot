import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.config import BotConfig
from kouhai_bot.llm_config import LlmProviderConfig
from kouhai_bot.handlers.shared import call_chat_completion, summarize_problem
from kouhai_bot.llm import _ChatCompletionAttempt


class _DummySession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _openai_cfg(**overrides):
    provider = LlmProviderConfig(
        name="openai",
        api_key="sk-test",
        base_url="http://localhost:8080/v1",
        model="gpt-5.5",
    )
    cfg = BotConfig(
        llm_providers=[provider],
        llm_max_retries=2,
        llm_retry_base_delay_sec=1.0,
        llm_retry_max_delay_sec=8.0,
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def test_call_chat_completion_retries_transient_failures_then_succeeds():
    cfg = _openai_cfg(llm_max_retries=2, llm_retry_base_delay_sec=0.25)
    sleep_mock = AsyncMock()
    calls = []
    responses = [
        _ChatCompletionAttempt(text=None, retryable=True, retry_after_sec=None, failure_kind="service_unavailable"),
        _ChatCompletionAttempt(text="OK", retryable=False, retry_after_sec=None, failure_kind=None),
    ]

    async def fake_once(session, **kwargs):
        calls.append(kwargs["payload"]["model"])
        return responses.pop(0)

    with patch("kouhai_bot.llm.get_config", return_value=cfg), \
            patch("kouhai_bot.llm.aiohttp.ClientSession", _DummySession), \
            patch("kouhai_bot.llm._post_chat_completion_once", side_effect=fake_once) as once_mock, \
            patch("kouhai_bot.llm.asyncio.sleep", sleep_mock):
        result = asyncio.run(call_chat_completion(
            [{"role": "user", "content": "Reply with exactly OK."}],
            task="judge",
        ))

    assert result == "OK"
    assert calls == ["gpt-5.5", "gpt-5.5"]
    assert once_mock.call_count == 2
    sleep_mock.assert_awaited_once_with(0.25)


def test_call_chat_completion_stops_on_non_retryable_failure():
    cfg = _openai_cfg(llm_max_retries=4, llm_retry_base_delay_sec=0.25)
    sleep_mock = AsyncMock()

    async def fake_once(session, **kwargs):
        return _ChatCompletionAttempt(text=None, retryable=False, retry_after_sec=None, failure_kind="error")

    with patch("kouhai_bot.llm.get_config", return_value=cfg), \
            patch("kouhai_bot.llm.aiohttp.ClientSession", _DummySession), \
            patch("kouhai_bot.llm._post_chat_completion_once", side_effect=fake_once) as once_mock, \
            patch("kouhai_bot.llm.asyncio.sleep", sleep_mock):
        result = asyncio.run(call_chat_completion(
            [{"role": "user", "content": "Reply with exactly OK."}],
            task="judge",
        ))

    assert result is None
    assert once_mock.call_count == 1
    sleep_mock.assert_not_awaited()


def test_call_chat_completion_honors_retry_after_with_cap():
    cfg = _openai_cfg(
        llm_max_retries=1,
        llm_retry_base_delay_sec=0.25,
        llm_retry_max_delay_sec=2.5,
    )
    sleep_mock = AsyncMock()
    responses = [
        _ChatCompletionAttempt(text=None, retryable=True, retry_after_sec=99.0, failure_kind="service_unavailable"),
        _ChatCompletionAttempt(text="OK", retryable=False, retry_after_sec=None, failure_kind=None),
    ]

    async def fake_once(session, **kwargs):
        return responses.pop(0)

    with patch("kouhai_bot.llm.get_config", return_value=cfg), \
            patch("kouhai_bot.llm.aiohttp.ClientSession", _DummySession), \
            patch("kouhai_bot.llm._post_chat_completion_once", side_effect=fake_once), \
            patch("kouhai_bot.llm.asyncio.sleep", sleep_mock):
        result = asyncio.run(call_chat_completion(
            [{"role": "user", "content": "Reply with exactly OK."}],
            task="judge",
        ))

    assert result == "OK"
    sleep_mock.assert_awaited_once_with(2.5)


def test_summarize_problem_uses_configured_timeout():
    cfg = _openai_cfg(summary_timeout_sec=321)

    from kouhai_bot.llm import ChatCompletionResult

    async def fake_call_chat(messages, model="", task="", temperature=0.7, timeout=120,
                             response_format=None, thinking=None):
        assert task == "summary"
        assert timeout == 321
        return ChatCompletionResult(text="summary ok", model_tag="🐳")

    with patch("kouhai_bot.handlers.shared.get_config", return_value=cfg), \
            patch("kouhai_bot.handlers.shared.call_chat_completion_result", side_effect=fake_call_chat):
        summary, tag = asyncio.run(summarize_problem("stmt", "input", "limits"))
        assert summary == "summary ok"
        assert tag == "🐳"
