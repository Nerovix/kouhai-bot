import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.config import BotConfig
from kouhai_bot.llm_config import LlmProviderConfig
from kouhai_bot.handlers.shared import call_chat_completion, summarize_problem
from kouhai_bot.llm import _ChatCompletionAttempt, _post_chat_completion_once


class _DummySession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _AsyncByteLines:
    def __init__(self, lines):
        self._lines = [line if isinstance(line, bytes) else line.encode("utf-8") for line in lines]
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._lines):
            raise StopAsyncIteration
        line = self._lines[self._index]
        self._index += 1
        return line


class _DummyResponse:
    def __init__(self, *, status=200, headers=None, text="", json_data=None, lines=None):
        self.status = status
        self.headers = headers or {}
        self._text = text
        self._json_data = json_data or {}
        self.content = _AsyncByteLines(lines or [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self):
        return self._text

    async def json(self):
        return self._json_data


class _PostSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


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


def test_dashscope_provider_payload_enables_stream():
    provider = LlmProviderConfig(
        name="qwen",
        api_key="sk-test",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen-test",
    )
    cfg = _openai_cfg(llm_providers=[provider])
    calls = []

    async def fake_once(session, **kwargs):
        calls.append(kwargs["payload"].copy())
        return _ChatCompletionAttempt(text="OK", retryable=False, retry_after_sec=None)

    with patch("kouhai_bot.llm.get_config", return_value=cfg), \
            patch("kouhai_bot.llm.aiohttp.ClientSession", _DummySession), \
            patch("kouhai_bot.llm._post_chat_completion_once", side_effect=fake_once):
        result = asyncio.run(call_chat_completion(
            [{"role": "user", "content": "Reply with exactly OK."}],
            task="judge",
        ))

    assert result == "OK"
    assert calls[0]["stream"] is True


def test_non_dashscope_provider_payload_does_not_enable_stream():
    cfg = _openai_cfg()
    calls = []

    async def fake_once(session, **kwargs):
        calls.append(kwargs["payload"].copy())
        return _ChatCompletionAttempt(text="OK", retryable=False, retry_after_sec=None)

    with patch("kouhai_bot.llm.get_config", return_value=cfg), \
            patch("kouhai_bot.llm.aiohttp.ClientSession", _DummySession), \
            patch("kouhai_bot.llm._post_chat_completion_once", side_effect=fake_once):
        result = asyncio.run(call_chat_completion(
            [{"role": "user", "content": "Reply with exactly OK."}],
            task="judge",
        ))

    assert result == "OK"
    assert "stream" not in calls[0]


def test_streaming_chat_completion_reads_sse_delta_chunks():
    response = _DummyResponse(lines=[
        ': keep-alive\n',
        'data: {"choices":[{"delta":{"content":"Hello"}}]}\n',
        '\n',
        'data: {"choices":[{"delta":{"content":" world"}}]}\n',
        '\n',
        'data: [DONE]\n',
        '\n',
    ])
    session = _PostSession(response)

    result = asyncio.run(_post_chat_completion_once(
        session,
        provider_name="qwen",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        headers={},
        payload={"stream": True},
        timeout=120,
    ))

    assert result.text == "Hello world"
    assert result.retryable is False
    assert session.calls[0][1]["json"] == {"stream": True}


def test_streaming_chat_completion_eof_without_done_is_retryable():
    response = _DummyResponse(lines=[
        'data: {"choices":[{"delta":{"content":"partial"}}]}\n',
        '\n',
    ])
    session = _PostSession(response)

    result = asyncio.run(_post_chat_completion_once(
        session,
        provider_name="qwen",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        headers={},
        payload={"stream": True},
        timeout=120,
    ))

    assert result.text is None
    assert result.retryable is True
    assert result.failure_kind == "service_unavailable"


def test_streaming_chat_completion_malformed_sse_is_retryable():
    response = _DummyResponse(lines=[
        'data: not-json\n',
        '\n',
    ])
    session = _PostSession(response)

    result = asyncio.run(_post_chat_completion_once(
        session,
        provider_name="qwen",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        headers={},
        payload={"stream": True},
        timeout=120,
    ))

    assert result.text is None
    assert result.retryable is True
    assert result.failure_kind == "service_unavailable"
