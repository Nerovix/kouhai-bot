import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.config import BotConfig
from kouhai_bot.llm_config import LlmProviderConfig
from kouhai_bot.handlers.shared import (
    build_judge_messages,
    build_second_judge_messages,
    call_chat_completion,
    get_judge_prompt,
    call_chat_completion_result,
    judge_submission_result,
    robust_json_parse,
    summarize_problem,
    translate_editorial_to_zh,
    translate_sample_notes,
)
from kouhai_bot.llm import ChatCompletionResult, _ChatCompletionAttempt, _post_chat_completion_once


def test_judge_prompt_rejects_repaired_greedy_and_unbatched_simulation():
    prompt = get_judge_prompt()

    assert 'Judge the algorithm the user actually wrote' in prompt
    assert 'Do not repair "take the current largest interval/cost and split it in the middle"' in prompt
    assert 'maximum marginal gain' in prompt
    assert 'step-by-step heap simulation' in prompt
    assert 'batching or logarithmic optimization' in prompt


def test_translate_editorial_to_zh_uses_structured_match_output():
    calls = []

    async def fake_call(messages, **kwargs):
        calls.append({"messages": messages, **kwargs})
        return ChatCompletionResult(
            text=json.dumps({"matched": "yes", "result": "中文题解译文"}),
            model_tag="🐳",
        )

    with patch("kouhai_bot.handlers.shared.call_chat_completion_result", fake_call):
        translated, tag, matched = asyncio.run(translate_editorial_to_zh(
            "Official editorial",
            pid="542D",
            problem_text="Problem statement",
        ))

    assert translated == "中文题解译文"
    assert tag == "🐳"
    assert matched is True
    call = calls[0]
    assert call["task"] == "summary"
    assert call["response_format"] == {"type": "json_object"}
    assert call["thinking"] == {"type": "enabled"}
    assert "matched" in call["messages"][0]["content"]
    payload = json.loads(call["messages"][1]["content"])
    assert payload["pid"] == "542D"
    assert payload["problem"] == "Problem statement"
    assert payload["official_editorial"] == "Official editorial"


def test_build_judge_messages_uses_structured_dialogue_context():
    messages = build_judge_messages(
        "Problem statement",
        "current idea",
        [
            {"type": "clarify", "content": "what is n?", "result": "clarify", "reply": "n is input"},
            {"type": "submit", "content": "old wrong idea", "result": "incorrect", "reason": "misses edge case"},
        ],
    )

    payload = json.loads(messages[1]["content"])
    assert payload["task"] == "first_pass_judge_complete_solution"
    assert payload["problem_statement"] == "Problem statement"
    assert payload["current_submission"] == "current idea"
    assert payload["dialogue"][0] == {
        "turn": 1,
        "role": "user",
        "kind": "clarify",
        "content": "what is n?",
        "note": "user_claim",
        "verdict": "clarify",
    }
    assert payload["dialogue"][1]["role"] == "assistant"
    assert payload["dialogue"][1]["note"] == "bot_feedback_not_user_claim"
    assert payload["dialogue"][2]["verdict"] == "incorrect"
    assert payload["dialogue"][3]["note"] == "bot_reason_not_user_claim"
    assert "history" not in payload


def test_build_second_judge_messages_contains_review_contract():
    messages = build_second_judge_messages(
        "Problem statement",
        "User solution",
        [{"type": "clarify", "content": "history", "result": "clarify", "reply": "bot reply"}],
        {"correct": True, "reason": "first pass", "reply": "", "reaction": ""},
        "Official editorial text",
        "https://codeforces.com/blog/entry/1",
    )

    system_text = messages[0]["content"]
    payload = json.loads(messages[1]["content"])
    assert "一审 bot 做出判定时看不到官方题解" in system_text
    assert "不是题解匹配器" in system_text
    assert "和官方题解完全不同" in system_text
    assert "不是重新审查完整性" in system_text
    assert "这些完整性问题已经由一审处理" in system_text
    assert "题解可能与本题不对应" not in system_text
    assert "用户实际写出的做法" in system_text
    assert "维护当前最大区间" in system_text
    assert "边际收益" in system_text
    assert "逐个添加/逐个弹堆" in system_text
    assert payload["task"] == "second_review_correctness_only"
    assert payload["problem_statement"] == "Problem statement"
    assert payload["current_submission"] == "User solution"
    assert payload["dialogue"][0]["content"] == "history"
    assert payload["dialogue"][1]["note"] == "bot_feedback_not_user_claim"
    assert payload["first_pass"]["reason_to_audit"] == "first pass"
    assert payload["official_reference"]["editorial"] == "Official editorial text"
    assert payload["official_reference"]["source"] == "https://codeforces.com/blog/entry/1"
    assert "history" not in payload
    assert "first_judge_result" not in payload


def test_second_judge_prompt_rejects_repaired_greedy_for_teleporters():
    messages = build_second_judge_messages(
        "CF1661F Teleporters statement",
        "根据相邻点区间的大小维护一个大根堆，每次取最大值从中间截断，直到能量不超过 m",
        [],
        {"correct": True, "reason": "first pass repaired it into marginal gain greedy"},
        "Official editorial uses f(x,k)-f(x,k+1) marginal gain and binary search over the gain threshold.",
        "https://codeforces.com/blog/entry/101790",
    )

    system_text = messages[0]["content"]
    payload = json.loads(messages[1]["content"])
    assert "不要把“维护当前最大区间/最大代价并从中间截断”自动修补成“维护新增一个操作的边际收益”" in system_text
    assert "没有说明批量化或对阈值二分" in system_text
    assert "主动尝试构造小反例" in system_text
    assert "二审不因普通实现细节" in system_text
    assert "从中间截断" in payload["current_submission"]
    assert "f(x,k)-f(x,k+1)" in payload["official_reference"]["editorial"]
    assert "different algorithm" in payload["decision_focus"][-1]


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
    smart_provider = LlmProviderConfig(
        name="openai",
        api_key="sk-test",
        base_url="http://localhost:8080/v1",
        model="gpt-5.5",
    )
    general_provider = LlmProviderConfig(
        name="openai",
        api_key="sk-test",
        base_url="http://localhost:8080/v1",
        model="gpt-5.5-mini",
    )
    cfg = BotConfig(
        llm_smart_providers=[smart_provider],
        llm_general_providers=[general_provider],
        llm_max_retries=2,
        llm_retry_base_delay_sec=1.0,
        llm_retry_max_delay_sec=8.0,
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def test_provider_model_for_uses_provider_model_and_explicit_override():
    provider = LlmProviderConfig(
        name="openai",
        api_key="sk-test",
        base_url="http://localhost:8080/v1",
        model="configured",
    )

    assert provider.model_for() == "configured"
    assert provider.model_for(explicit_model="override") == "override"


def test_general_model_tasks_preserve_provider_model_tag():
    provider = LlmProviderConfig(
        name="openai",
        api_key="sk-test",
        base_url="http://localhost:8080/v1",
        model="general",
        model_tag="『G』",
    )
    cfg = _openai_cfg(llm_general_providers=[provider])
    calls = []

    async def fake_once(session, **kwargs):
        calls.append(kwargs["payload"].copy())
        return _ChatCompletionAttempt(text="OK", retryable=False, retry_after_sec=None)

    with patch("kouhai_bot.llm.get_config", return_value=cfg), \
            patch("kouhai_bot.llm.aiohttp.ClientSession", _DummySession), \
            patch("kouhai_bot.llm._post_chat_completion_once", side_effect=fake_once):
        result = asyncio.run(call_chat_completion_result(
            [{"role": "user", "content": "Reply with exactly OK."}],
            task="summary",
        ))

    assert result.text == "OK"
    assert result.model == "general"
    assert result.model_tag == "『G』"
    assert calls[0]["model"] == "general"


def test_task_entrypoints_are_blackbox_except_model_class_and_tag():
    smart_provider = LlmProviderConfig(
        name="deepseek-smart",
        api_key="sk-test",
        base_url="http://localhost:8080/v1",
        model="deepseek-v4-pro",
        model_tag="『T』",
    )
    general_provider = LlmProviderConfig(
        name="deepseek-general",
        api_key="sk-test",
        base_url="http://localhost:8080/v1",
        model="deepseek-v4-flash",
        model_tag="『T』",
    )
    cfg = _openai_cfg(
        llm_smart_providers=[smart_provider],
        llm_general_providers=[general_provider],
    )
    calls = []

    async def fake_once(session, **kwargs):
        payload = kwargs["payload"].copy()
        calls.append(payload)
        text = "OK"
        if payload.get("response_format") == {"type": "json_object"}:
            user_content = payload["messages"][-1]["content"]
            if "official_editorial" in user_content:
                text = json.dumps({"matched": "yes", "result": "中文题解"})
            else:
                text = json.dumps({
                    "correct": False,
                    "reason": "missing proof",
                    "reply": "再检查一下证明。",
                    "reaction": "",
                })
        return _ChatCompletionAttempt(text=text, retryable=False, retry_after_sec=None)

    with patch("kouhai_bot.handlers.shared.get_config", return_value=cfg), \
            patch("kouhai_bot.llm.get_config", return_value=cfg), \
            patch("kouhai_bot.llm.aiohttp.ClientSession", _DummySession), \
            patch("kouhai_bot.llm._post_chat_completion_once", side_effect=fake_once):
        judge = asyncio.run(judge_submission_result("problem", "solution", []))
        review = asyncio.run(call_chat_completion_result(
            [{"role": "user", "content": "review this"}],
            task="review",
        ))
        summary, summary_tag = asyncio.run(summarize_problem("stmt", "input", "limits"))
        notes, notes_tag = asyncio.run(translate_sample_notes("1 goes to 2"))
        editorial, editorial_tag, matched = asyncio.run(translate_editorial_to_zh(
            "official editorial",
            pid="1A",
            problem_text="problem",
        ))

    assert robust_json_parse(judge.text)["correct"] is False
    assert judge.model_tag == "『T』"
    assert review.text == "OK"
    assert review.model_tag == "『T』"
    assert summary == "OK"
    assert summary_tag == "『T』"
    assert notes == "OK"
    assert notes_tag == "『T』"
    assert editorial == "中文题解"
    assert editorial_tag == "『T』"
    assert matched is True
    assert [call["model"] for call in calls] == [
        "deepseek-v4-pro",
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "deepseek-v4-flash",
        "deepseek-v4-flash",
    ]


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


def test_call_chat_completion_pinned_provider_does_not_fallback():
    openai = LlmProviderConfig(
        name="openai",
        api_key="sk-openai",
        base_url="http://openai.local/v1",
        model="gpt-5.5",
    )
    deepseek = LlmProviderConfig(
        name="deepseek",
        api_key="sk-deepseek",
        base_url="http://deepseek.local/v1",
        model="deepseek-v4-pro",
    )
    cfg = BotConfig(
        llm_smart_providers=[openai, deepseek],
        llm_general_providers=[deepseek],
        llm_max_retries=0,
        llm_retry_base_delay_sec=0.0,
        llm_retry_max_delay_sec=0.0,
    )
    calls = []

    async def fake_once(session, **kwargs):
        calls.append(kwargs["provider_name"])
        return _ChatCompletionAttempt(
            text=None,
            retryable=False,
            retry_after_sec=None,
            failure_kind="service_unavailable",
        )

    with patch("kouhai_bot.llm.get_config", return_value=cfg), \
            patch("kouhai_bot.llm.aiohttp.ClientSession", _DummySession), \
            patch("kouhai_bot.llm._post_chat_completion_once", side_effect=fake_once):
        result = asyncio.run(call_chat_completion_result(
            [{"role": "user", "content": "Reply with exactly OK."}],
            task="judge",
            provider_name="openai",
        ))

    assert result.text is None
    assert result.failure_kind == "service_unavailable"
    assert calls == ["openai"]


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
    cfg = _openai_cfg(llm_smart_providers=[provider], llm_stream_idle_timeout_sec=321)
    calls = []

    async def fake_once(session, **kwargs):
        calls.append({
            "payload": kwargs["payload"].copy(),
            "stream_idle_timeout_sec": kwargs["stream_idle_timeout_sec"],
        })
        return _ChatCompletionAttempt(text="OK", retryable=False, retry_after_sec=None)

    with patch("kouhai_bot.llm.get_config", return_value=cfg), \
            patch("kouhai_bot.llm.aiohttp.ClientSession", _DummySession), \
            patch("kouhai_bot.llm._post_chat_completion_once", side_effect=fake_once):
        result = asyncio.run(call_chat_completion(
            [{"role": "user", "content": "Reply with exactly OK."}],
            task="judge",
            thinking={"type": "enabled"},
        ))

    assert result == "OK"
    assert calls[0]["payload"]["stream"] is True
    assert calls[0]["payload"]["thinking"] == {"type": "enabled"}
    assert calls[0]["payload"]["enable_thinking"] is True
    assert calls[0]["stream_idle_timeout_sec"] == 321


def test_streaming_provider_uses_default_idle_timeout():
    provider = LlmProviderConfig(
        name="qwen",
        api_key="sk-test",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen-test",
    )
    cfg = _openai_cfg(llm_smart_providers=[provider])
    calls = []

    async def fake_once(session, **kwargs):
        calls.append(kwargs["stream_idle_timeout_sec"])
        return _ChatCompletionAttempt(text="OK", retryable=False, retry_after_sec=None)

    with patch("kouhai_bot.llm.get_config", return_value=cfg), \
            patch("kouhai_bot.llm.aiohttp.ClientSession", _DummySession), \
            patch("kouhai_bot.llm._post_chat_completion_once", side_effect=fake_once):
        result = asyncio.run(call_chat_completion(
            [{"role": "user", "content": "Reply with exactly OK."}],
            task="judge",
        ))

    assert result == "OK"
    assert calls == [120]


def test_zenmux_fable_payload_uses_reasoning_effort_without_generic_thinking():
    provider = LlmProviderConfig(
        name="fable",
        api_key="sk-test",
        base_url="https://zenmux.ai/api/v1",
        model="anthropic/claude-fable-5-free",
        reasoning_effort="max",
    )
    cfg = _openai_cfg(llm_smart_providers=[provider])
    calls = []

    async def fake_once(session, **kwargs):
        calls.append(kwargs["payload"].copy())
        return _ChatCompletionAttempt(
            text="{\"ok\":true}",
            retryable=False,
            retry_after_sec=None,
        )

    with patch("kouhai_bot.llm.get_config", return_value=cfg), \
            patch("kouhai_bot.llm.aiohttp.ClientSession", _DummySession), \
            patch("kouhai_bot.llm._post_chat_completion_once", side_effect=fake_once):
        result = asyncio.run(call_chat_completion_result(
            [{"role": "user", "content": "Reply with JSON."}],
            task="judge",
            response_format={"type": "json_object"},
            thinking={"type": "enabled"},
        ))

    assert result.text == "{\"ok\":true}"
    assert calls[0]["model"] == "anthropic/claude-fable-5-free"
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["reasoning_effort"] == "max"
    assert calls[0]["temperature"] == 1
    assert "thinking" not in calls[0]
    assert "enable_thinking" not in calls[0]
    assert "stream" not in calls[0]


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


def test_explicit_stream_provider_payload_enables_stream():
    provider = LlmProviderConfig(
        name="openai",
        api_key="sk-test",
        base_url="http://localhost:8080/v1",
        model="gpt-5.5",
        stream=True,
    )
    cfg = _openai_cfg(llm_smart_providers=[provider])
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


def test_streaming_chat_completion_uses_sock_read_idle_timeout():
    response = _DummyResponse(lines=[
        'data: {"choices":[{"delta":{"content":"OK"}}]}\n',
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
        timeout=7200,
        stream_idle_timeout_sec=600,
    ))

    timeout = session.calls[0][1]["timeout"]
    assert result.text == "OK"
    assert timeout.total == 7200
    assert timeout.sock_read == 600


def test_non_streaming_chat_completion_does_not_use_sock_read_idle_timeout():
    response = _DummyResponse(json_data={
        "choices": [{"message": {"content": "OK"}}],
    })
    session = _PostSession(response)

    result = asyncio.run(_post_chat_completion_once(
        session,
        provider_name="openai",
        base_url="http://localhost:8080/v1",
        headers={},
        payload={},
        timeout=7200,
        stream_idle_timeout_sec=600,
    ))

    timeout = session.calls[0][1]["timeout"]
    assert result.text == "OK"
    assert timeout.total == 7200
    assert timeout.sock_read is None


def test_streaming_chat_completion_reads_sse_delta_chunks(caplog):
    caplog.set_level("INFO", logger="kouhai-bot.llm")
    response = _DummyResponse(lines=[
        ': keep-alive\n',
        'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}\n',
        '\n',
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
    assert "reasoning_content chars=8" in caplog.text


def test_streaming_chat_completion_ignores_metadata_events():
    response = _DummyResponse(lines=[
        'data: {"id":"evt_1","model":"qwen-test","created":1}\n',
        '\n',
        'data: {"choices":[{"delta":{"content":"OK"}}]}\n',
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

    assert result.text == "OK"
    assert result.retryable is False


def test_streaming_chat_completion_reads_responses_delta_events():
    response = _DummyResponse(lines=[
        'data: {"type":"response.created","response":{"id":"resp_1"}}\n',
        '\n',
        'data: {"type":"response.output_text.delta","delta":"{\\"correct\\":"}\n',
        '\n',
        'data: {"type":"response.output_text.delta","delta":"false}"}\n',
        '\n',
        'data: {"type":"response.output_text.done","text":"{\\"correct\\":false}"}\n',
        '\n',
        'data: [DONE]\n',
        '\n',
    ])
    session = _PostSession(response)

    result = asyncio.run(_post_chat_completion_once(
        session,
        provider_name="openai",
        base_url="http://localhost:8080/v1",
        headers={},
        payload={"stream": True},
        timeout=120,
    ))

    assert result.text == '{"correct":false}'
    assert result.retryable is False


def test_streaming_chat_completion_error_event_is_retryable():
    response = _DummyResponse(lines=[
        'data: {"error":{"type":"upstream_error","message":"broken"}}\n',
        '\n',
    ])
    session = _PostSession(response)

    result = asyncio.run(_post_chat_completion_once(
        session,
        provider_name="openai",
        base_url="http://localhost:8080/v1",
        headers={},
        payload={"stream": True},
        timeout=120,
    ))

    assert result.text is None
    assert result.retryable is True
    assert result.failure_kind == "service_unavailable"


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
