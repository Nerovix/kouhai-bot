import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.config import BotConfig
from kouhai_bot.llm_config import LlmProviderConfig
from kouhai_bot.handlers.shared import (
    SummaryDoublecheckResult,
    build_judge_messages,
    build_multimodal_user_content,
    build_second_judge_messages,
    _build_summary_prompt,
    _build_summary_repair_prompt,
    _summary_required_convention_issues,
    _summary_symbol_scope_issues,
    call_chat_completion,
    get_judge_prompt,
    call_chat_completion_result,
    doublecheck_problem_summary,
    get_problem_summary,
    judge_submission,
    judge_submission_result,
    parse_json_with_llm_repair,
    robust_json_parse,
    save_problem_summary,
    summarize_problem,
    translate_editorial_to_zh,
    translate_sample_notes,
)
from kouhai_bot.llm import (
    ChatCompletionResult,
    _ChatCompletionAttempt,
    _post_chat_completion_once,
    strip_leaked_thinking,
)
from tools import summary_doublecheck as summary_doublecheck_tool


def test_problem_summary_cache_is_bound_to_statement_source(tmp_path):
    cfg = BotConfig(data_dir=str(tmp_path))
    statement_dir = tmp_path / "statements"
    statement_dir.mkdir()
    statement_path = statement_dir / "542D.json"
    statement_path.write_text(
        json.dumps({"description": "original", "images": []}),
        encoding="utf-8",
    )

    with patch("kouhai_bot.handlers.shared.get_config", return_value=cfg), patch(
        "kouhai_bot.problem_content.get_config",
        return_value=cfg,
    ):
        save_problem_summary(1, "542D", "已审计题意")
        assert get_problem_summary(1, "542D") == "已审计题意"

        statement_path.write_text(
            json.dumps({"description": "changed", "images": []}),
            encoding="utf-8",
        )
        assert get_problem_summary(1, "542D") == ""


def test_problem_summary_cache_rejects_stale_prepared_source(tmp_path):
    cfg = BotConfig(data_dir=str(tmp_path))
    statement_dir = tmp_path / "statements"
    statement_dir.mkdir()
    (statement_dir / "542D.json").write_text(
        json.dumps({"description": "current", "images": []}),
        encoding="utf-8",
    )

    with patch("kouhai_bot.handlers.shared.get_config", return_value=cfg), patch(
        "kouhai_bot.problem_content.get_config",
        return_value=cfg,
    ):
        with pytest.raises(ValueError, match="stale summary source"):
            save_problem_summary(
                1,
                "542D",
                "旧题面对应的题意",
                source_sha256="0" * 64,
            )


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

    cfg = _openai_cfg(summary_timeout_sec=123)
    with patch("kouhai_bot.handlers.shared.get_config", return_value=cfg), \
            patch("kouhai_bot.handlers.shared.call_chat_completion_result", fake_call):
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
    def __init__(self, *_args, **_kwargs):
        pass

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


def test_multimodal_task_uses_multimodal_provider_and_preserves_image_content():
    provider = LlmProviderConfig(
        name="mmx",
        api_key="sk-test",
        base_url="http://localhost:8080/v1",
        model="mmx-vision",
        model_tag="『MMx』",
    )
    cfg = _openai_cfg(llm_multimodal_providers=[provider])
    content = [
        {"type": "text", "text": "Read this statement."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
    ]
    calls = []

    async def fake_once(session, **kwargs):
        calls.append(kwargs["payload"].copy())
        return _ChatCompletionAttempt(text="OK", retryable=False, retry_after_sec=None)

    with patch("kouhai_bot.llm.get_config", return_value=cfg), \
            patch("kouhai_bot.llm.aiohttp.ClientSession", _DummySession), \
            patch("kouhai_bot.llm._post_chat_completion_once", side_effect=fake_once):
        result = asyncio.run(call_chat_completion_result(
            [{"role": "user", "content": content}],
            task="multimodal_clarify",
        ))

    assert result.text == "OK"
    assert result.model == "mmx-vision"
    assert result.model_tag == "『MMx』"
    assert calls[0]["messages"][0]["content"] == content
    assert "max_tokens" not in calls[0]
    assert "max_completion_tokens" not in calls[0]


def test_summarize_problem_with_images_uses_multimodal_task():
    provider = LlmProviderConfig(
        name="mmx",
        api_key="sk-test",
        base_url="http://localhost:8080/v1",
        model="mmx-vision",
        model_tag="『MMx』",
    )
    cfg = _openai_cfg(llm_multimodal_providers=[provider], summary_timeout_sec=123)
    calls = []

    async def fake_call(messages, **kwargs):
        calls.append({"messages": messages, **kwargs})
        if kwargs["task"] == "summary":
            return ChatCompletionResult(
                text=json.dumps({"accurate": True, "issues": []}),
                model_tag="『T』",
            )
        return ChatCompletionResult(
            text=json.dumps({"summary": "带图题意"}),
            model_tag="『MMx』",
        )

    with patch("kouhai_bot.handlers.shared.get_config", return_value=cfg), \
            patch("kouhai_bot.handlers.shared._download_image_data_url_async", AsyncMock(return_value="data:image/png;base64,abc")), \
            patch("kouhai_bot.handlers.shared.call_chat_completion_result", side_effect=fake_call):
        summary, tag = asyncio.run(summarize_problem(
            "stmt",
            "input",
            "limits",
            [{"src": "https://codeforces.com/p.png", "kind": "graphic"}],
        ))

    assert summary == "带图题意"
    assert tag == "『MMx』"
    assert calls[0]["task"] == "multimodal_summary"
    assert calls[1]["task"] == "summary"
    content = calls[0]["messages"][1]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[-1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}


def test_translate_sample_notes_with_images_uses_multimodal_task():
    provider = LlmProviderConfig(
        name="mmx",
        api_key="sk-test",
        base_url="http://localhost:8080/v1",
        model="mmx-vision",
        model_tag="『MMx』",
    )
    cfg = _openai_cfg(llm_multimodal_providers=[provider], summary_timeout_sec=123)
    calls = []

    async def fake_call(messages, **kwargs):
        calls.append({"messages": messages, **kwargs})
        return ChatCompletionResult(text="样例图解释", model_tag="『MMx』")

    with patch("kouhai_bot.handlers.shared.get_config", return_value=cfg), \
            patch("kouhai_bot.handlers.shared._download_image_data_url_async", AsyncMock(return_value="data:image/png;base64,abc")), \
            patch("kouhai_bot.handlers.shared.call_chat_completion_result", side_effect=fake_call):
        text, tag = asyncio.run(translate_sample_notes(
            "The diagram is [[IMAGE_1: graphic]].",
            [{"src": "https://codeforces.com/p.png", "kind": "graphic", "marker": "IMAGE_1"}],
        ))

    assert text == "样例图解释"
    assert tag == "『MMx』"
    assert calls[0]["task"] == "multimodal_summary"
    content = calls[0]["messages"][1]["content"]
    assert isinstance(content, list)
    assert "IMAGE_1" in content[1]["text"]
    assert content[-1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}


def test_translate_editorial_with_images_uses_multimodal_task():
    provider = LlmProviderConfig(
        name="mmx",
        api_key="sk-test",
        base_url="http://localhost:8080/v1",
        model="mmx-vision",
        model_tag="『MMx』",
    )
    cfg = _openai_cfg(llm_multimodal_providers=[provider], summary_timeout_sec=123)
    calls = []

    async def fake_call(messages, **kwargs):
        calls.append({"messages": messages, **kwargs})
        return ChatCompletionResult(
            text=json.dumps({"matched": "yes", "result": "中文题解"}),
            model_tag="『MMx』",
        )

    with patch("kouhai_bot.handlers.shared.get_config", return_value=cfg), \
            patch("kouhai_bot.handlers.shared._download_image_data_url_async", AsyncMock(return_value="data:image/png;base64,abc")), \
            patch("kouhai_bot.handlers.shared.call_chat_completion_result", side_effect=fake_call):
        translated, tag, matched = asyncio.run(translate_editorial_to_zh(
            "Official editorial",
            pid="1A",
            problem_text="Problem uses [[IMAGE_1: formula]].",
            images=[{"src": "https://codeforces.com/f.png", "kind": "formula", "marker": "IMAGE_1"}],
        ))

    assert translated == "中文题解"
    assert tag == "『MMx』"
    assert matched is True
    assert calls[0]["task"] == "multimodal_summary"
    content = calls[0]["messages"][1]["content"]
    assert isinstance(content, list)
    assert "official_editorial" in content[0]["text"]
    assert "IMAGE_1" in content[1]["text"]


def test_multimodal_content_prioritizes_images_without_text_fallback():
    images = [
        {
            "src": f"https://codeforces.com/{idx}.png",
            "kind": "formula",
            "marker": f"IMAGE_{idx}",
            "placeholder": f"[[IMAGE_{idx}: formula: x_{idx}]]",
            "alt": f"x_{idx}",
        }
        for idx in range(1, 9)
    ] + [
        {
            "src": "https://codeforces.com/9.png",
            "kind": "graphic",
            "marker": "IMAGE_9",
            "placeholder": "[[IMAGE_9: graphic]]",
        },
        {
            "src": "https://codeforces.com/10.png",
            "kind": "formula",
            "marker": "IMAGE_10",
            "placeholder": "[[IMAGE_10: formula]]",
        },
    ]

    with patch(
        "kouhai_bot.handlers.shared._download_image_data_url_async",
        AsyncMock(side_effect=lambda url: f"data:image/png;base64,{url.rsplit('/', 1)[-1]}"),
    ) as download:
        content = asyncio.run(build_multimodal_user_content("statement", images))

    image_parts = [part for part in content if part.get("type") == "image_url"]
    attached_labels = "\n".join(
        content[idx]["text"]
        for idx in range(1, len(content) - 1, 2)
        if content[idx].get("type") == "text"
    )
    omitted_note = content[-1]["text"]
    downloaded_urls = {call.args[0] for call in download.call_args_list}

    assert len(image_parts) == 8
    assert "IMAGE_9" in attached_labels
    assert "IMAGE_10" in attached_labels
    assert "IMAGE_7" not in attached_labels
    assert "IMAGE_8" not in attached_labels
    assert "IMAGE_7" in omitted_note
    assert "IMAGE_8" in omitted_note
    assert "未随请求发送" in omitted_note
    assert "https://codeforces.com/7.png" not in downloaded_urls
    assert "https://codeforces.com/8.png" not in downloaded_urls


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
            if isinstance(user_content, list):
                user_content = "".join(
                    item.get("text", "")
                    for item in user_content
                    if isinstance(item, dict)
                )
            system_content = payload["messages"][0]["content"]
            if "official_editorial" in user_content:
                text = json.dumps({"matched": "yes", "result": "中文题解"})
            elif "独立语义审校员" in system_content:
                text = json.dumps({"accurate": True, "issues": []})
            elif "summary" in system_content:
                text = json.dumps({"summary": "OK"})
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




def test_parse_json_with_llm_repair_wraps_plain_text_summary():
    calls = []

    async def fake_call(messages, **kwargs):
        calls.append({"messages": messages, **kwargs})
        return ChatCompletionResult(
            text=json.dumps({"summary": "纯文本题意"}),
            model_tag="『FIX』",
        )

    cfg = _openai_cfg(summary_timeout_sec=123)
    with patch("kouhai_bot.handlers.shared.get_config", return_value=cfg), \
            patch("kouhai_bot.handlers.shared.call_chat_completion_result", side_effect=fake_call):
        parsed, tag = asyncio.run(parse_json_with_llm_repair(
            "纯文本题意",
            expected_schema='{ "summary": string }',
            timeout=123,
        ))

    assert parsed == {"summary": "纯文本题意"}
    assert tag == "『FIX』"
    assert len(calls) == 1
    assert calls[0]["task"] == "summary"
    assert calls[0]["temperature"] == 0.0
    assert calls[0]["timeout"] == 123
    assert calls[0]["response_format"] == {"type": "json_object"}


def test_parse_json_with_llm_repair_retries_until_valid_json():
    outputs = ["still not json", json.dumps({"ok": True})]

    async def fake_call(messages, **kwargs):
        return ChatCompletionResult(text=outputs.pop(0), model_tag="『FIX』")

    cfg = _openai_cfg(summary_timeout_sec=123)
    with patch("kouhai_bot.handlers.shared.get_config", return_value=cfg), \
            patch("kouhai_bot.handlers.shared.call_chat_completion_result", side_effect=fake_call):
        parsed, tag = asyncio.run(parse_json_with_llm_repair(
            "bad",
            expected_schema='{ "ok": boolean }',
            max_attempts=3,
        ))

    assert parsed == {"ok": True}
    assert tag == "『FIX』"
    assert outputs == []


def test_parse_json_with_llm_repair_strips_leaked_thinking_inside_valid_json():
    raw = json.dumps({
        "reply": "<thinking>hidden chain of thought</thinking>可以回答这个边界。",
        "reaction": "",
        "nested": ["<reasoning>internal</reasoning>visible"],
    }, ensure_ascii=False)

    parsed, tag = asyncio.run(parse_json_with_llm_repair(
        raw,
        expected_schema='{ "reply": string, "reaction": string }',
        max_attempts=0,
    ))

    assert parsed == {
        "reply": "可以回答这个边界。",
        "reaction": "",
        "nested": ["visible"],
    }
    assert tag == ""


def test_judge_submission_repairs_malformed_json_response():
    async def fake_judge_result(*args, **kwargs):
        return ChatCompletionResult(text='正确，应判 true', model_tag="『J』")

    async def fake_repair(messages, **kwargs):
        return ChatCompletionResult(
            text=json.dumps({"correct": True, "reason": "格式已修复", "reply": "", "reaction": ""}),
            model_tag="『FIX』",
        )

    cfg = _openai_cfg(summary_timeout_sec=123)
    with patch("kouhai_bot.handlers.shared.get_config", return_value=cfg), \
            patch("kouhai_bot.handlers.shared.judge_submission_result", side_effect=fake_judge_result), \
            patch("kouhai_bot.handlers.shared.call_chat_completion_result", side_effect=fake_repair):
        parsed = asyncio.run(judge_submission("problem", "solution"))

    assert parsed["correct"] is True
    assert parsed["reason"] == "格式已修复"


def test_summary_prompt_lists_unicode_notation_inventory():
    prompt = _build_summary_prompt("stmt", "input", "limits")
    repair = _build_summary_repair_prompt("stmt", "input", "limits", "bad", ["issue"])

    for text in (prompt, repair):
        assert "Unicode 角标白名单" in text
        assert "下标数字：₀₁₂₃₄₅₆₇₈₉" in text
        assert "上标数字：⁰¹²³⁴⁵⁶⁷⁸⁹" in text
        assert "下标拉丁字母：ₐ ₑ ₕ ᵢ ⱼ ₖ" in text
        assert "上标小写拉丁字母：ᵃ ᵇ ᶜ" in text
        assert "上标大写拉丁字母：ᴬ ᴮ ᴰ" in text
        assert "aᵢ₊₁" in text
        assert "10¹⁸" in text
        assert "10⁹+7" in text
        assert "不要自己创造看起来像角标的字母" in text
        assert "字符白名单不够用时，优先改成自然语言" in text
        assert "p_i+1" in text
        assert "pᵢ+1" in text
        assert "p_{i+1}" in text
        assert "pᵢ₊₁" in text
        assert "孤立点也算" in text


def test_summary_symbol_scope_gate_distinguishes_addition_from_next_index():
    source = "Use components containing vertices p_i and p_i+1 respectively."

    assert _summary_symbol_scope_issues(source, "分别取 pᵢ 和 pᵢ+1 所在分量") == []
    issues = _summary_symbol_scope_issues(source, "分别取 pᵢ 和 pᵢ₊₁ 所在分量")

    assert len(issues) == 1
    assert "p_i+1" in issues[0]
    assert "pᵢ₊₁" in issues[0]


def test_summary_symbol_scope_gate_avoids_ambiguous_source_with_both_forms():
    source = "Compare p_i+1 with p_{i+1}."

    assert _summary_symbol_scope_issues(source, "比较 pᵢ₊₁") == []


def test_summary_symbol_scope_gate_handles_braced_source_and_latex_candidate():
    source = "Use a_{p_{i} + 1} and the vertex p_{i} + 2."

    nested_array_issues = _summary_symbol_scope_issues(source, "使用 aₚᵢ₊₁")
    latex_issues = _summary_symbol_scope_issues(source, "使用顶点 $p_{i+2}$")

    assert len(nested_array_issues) == 1
    assert "p_i+1" in nested_array_issues[0]
    assert len(latex_issues) == 1
    assert "p_i+2" in latex_issues[0]


def test_summary_required_convention_gate_preserves_isolated_vertex_rule():
    source = (
        "Note that a vertex that has no edges is considered to have only incoming edges."
    )

    issues = _summary_required_convention_issues(source, "每个分量有唯一只有入边的点")

    assert len(issues) == 1
    assert "isolated vertices also count" in issues[0]
    assert _summary_required_convention_issues(
        source,
        "每个分量有唯一只有入边的点（孤立点也算）",
    ) == []


def test_summary_required_convention_gate_handles_wording_and_whitespace_variants():
    source = (
        "A vertex\nwith no edges is also considered to have only incoming edges."
    )

    issues = _summary_required_convention_issues(source, "每个分量有唯一只有入边的点")

    assert len(issues) == 1


def test_summarize_problem_uses_configured_timeout():
    cfg = _openai_cfg(summary_timeout_sec=321)

    from kouhai_bot.llm import ChatCompletionResult

    async def fake_call_chat(messages, model="", task="", temperature=0.7, timeout=120,
                             response_format=None, thinking=None):
        assert task == "summary"
        assert timeout == 321
        assert response_format == {"type": "json_object"}
        assert temperature == 0.4
        return ChatCompletionResult(text=json.dumps({"summary": "summary ok"}), model_tag="🐳")

    with patch("kouhai_bot.handlers.shared.get_config", return_value=cfg), \
            patch("kouhai_bot.handlers.shared.call_chat_completion_result", side_effect=fake_call_chat), \
            patch(
                "kouhai_bot.handlers.shared.doublecheck_problem_summary",
                AsyncMock(return_value=SummaryDoublecheckResult(accurate=True)),
            ):
        summary, tag = asyncio.run(summarize_problem("stmt", "input", "limits"))
        assert summary == "summary ok"
        assert tag == "🐳"


def test_summarize_problem_repairs_solution_leak_and_format_sections():
    cfg = _openai_cfg(summary_timeout_sec=321)
    calls = []
    bad = (
        "给定数组 a。\n输入：第一行 n。\n输出：答案。"
        "解题思路是枚举每个位置即可，复杂度 O(n)。"
    )
    fixed = "给定长度为 n 的数组 a，要求计算满足题目条件的答案并输出。n≤10⁵，时限 1s。"

    async def fake_call_chat(messages, **kwargs):
        calls.append({"messages": messages, **kwargs})
        text = bad if len(calls) == 1 else fixed
        tag = "bad" if len(calls) == 1 else "fixed"
        return ChatCompletionResult(text=json.dumps({"summary": text}), model_tag=tag)

    with patch("kouhai_bot.handlers.shared.get_config", return_value=cfg), \
            patch("kouhai_bot.handlers.shared.call_chat_completion_result", side_effect=fake_call_chat), \
            patch(
                "kouhai_bot.handlers.shared.doublecheck_problem_summary",
                AsyncMock(return_value=SummaryDoublecheckResult(accurate=True)),
            ):
        summary, tag = asyncio.run(summarize_problem("stmt", "input", "limits"))

    assert summary == fixed
    assert tag == "fixed"
    assert len(calls) == 2
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[1]["temperature"] == 0.2
    assert "bad_summary" in calls[1]["messages"][-1]["content"]


def test_summarize_problem_invalid_json_returns_none():
    cfg = _openai_cfg(summary_timeout_sec=321)

    async def fake_call_chat(messages, **kwargs):
        return ChatCompletionResult(text="plain text summary", model_tag="🐳")

    with patch("kouhai_bot.handlers.shared.get_config", return_value=cfg), \
            patch("kouhai_bot.handlers.shared.call_chat_completion_result", side_effect=fake_call_chat), \
            patch(
                "kouhai_bot.handlers.shared.doublecheck_problem_summary",
                AsyncMock(return_value=SummaryDoublecheckResult(accurate=True)),
            ):
        summary, tag = asyncio.run(summarize_problem("stmt", "input", "limits"))

    assert summary is None
    assert tag == ""


def test_summarize_problem_rejects_indeterminate_semantic_check():
    cfg = _openai_cfg(summary_timeout_sec=321)

    async def fake_call_chat(messages, **kwargs):
        return ChatCompletionResult(
            text=json.dumps({"summary": "给定 n，输出答案。"}),
            model_tag="🐳",
        )

    with patch("kouhai_bot.handlers.shared.get_config", return_value=cfg), \
            patch("kouhai_bot.handlers.shared.call_chat_completion_result", side_effect=fake_call_chat), \
            patch(
                "kouhai_bot.handlers.shared.doublecheck_problem_summary",
                AsyncMock(return_value=SummaryDoublecheckResult(
                    accurate=None,
                    failure_kind="service_unavailable",
                )),
            ):
        summary, tag = asyncio.run(summarize_problem("stmt", "input", "limits"))

    assert summary is None
    assert tag == ""


def test_summarize_problem_repairs_missing_time_and_memory_limits():
    cfg = _openai_cfg(summary_timeout_sec=321)
    calls = []
    first = "给定长度为 n 的数组 a，要求输出答案。n≤10⁵。"
    fixed = "给定长度为 n 的数组 a，要求输出答案。n≤10⁵，时限2s，内存256MB。"

    async def fake_call_chat(messages, **kwargs):
        calls.append(messages[-1]["content"])
        text = first if len(calls) == 1 else fixed
        return ChatCompletionResult(text=json.dumps({"summary": text}), model_tag="🐳")

    with patch("kouhai_bot.handlers.shared.get_config", return_value=cfg), \
            patch("kouhai_bot.handlers.shared.call_chat_completion_result", side_effect=fake_call_chat), \
            patch(
                "kouhai_bot.handlers.shared.doublecheck_problem_summary",
                AsyncMock(return_value=SummaryDoublecheckResult(accurate=True)),
            ):
        summary, tag = asyncio.run(summarize_problem(
            "stmt",
            "input",
            "Time: 2 seconds, Memory: 256 megabytes",
        ))

    assert summary == fixed
    assert tag == "🐳"
    assert len(calls) == 2
    assert "summary omits time limit" in calls[1]
    assert "summary omits memory limit" in calls[1]


def test_summarize_problem_does_not_rewrite_formula_notation_with_regex():
    cfg = _openai_cfg(summary_timeout_sec=321)
    summary_text = "给定 a_i、a_n、1e18、10^9+7，以及 f(x)=\\sum_{i=1}^{n} floor(x / p_i)。"

    async def fake_call_chat(messages, **kwargs):
        return ChatCompletionResult(text=json.dumps({"summary": summary_text}), model_tag="🐳")

    with patch("kouhai_bot.handlers.shared.get_config", return_value=cfg), \
            patch("kouhai_bot.handlers.shared.call_chat_completion_result", side_effect=fake_call_chat), \
            patch(
                "kouhai_bot.handlers.shared.doublecheck_problem_summary",
                AsyncMock(return_value=SummaryDoublecheckResult(accurate=True)),
            ):
        summary, tag = asyncio.run(summarize_problem("stmt", "input", "limits"))

    assert summary == summary_text
    assert tag == "🐳"


def test_doublecheck_problem_summary_uses_general_model_and_checks_symbol_scope():
    cfg = _openai_cfg(summary_timeout_sec=321)
    calls = []

    async def fake_call_chat(messages, **kwargs):
        calls.append({"messages": messages, **kwargs})
        return ChatCompletionResult(
            text=json.dumps({
                "accurate": False,
                "issues": [{
                    "severity": "critical",
                    "summary_claim": "含 pᵢ₊₁ 的弱连通分量",
                    "source_fact": "containing vertices p_i and p_i+1 respectively",
                    "explanation": "p_{i+1} 是下一个排列元素，p_i+1 是顶点编号加一。",
                }],
            }),
            model_tag="🐳",
            provider_name="general",
            model="general-model",
        )

    with patch("kouhai_bot.handlers.shared.get_config", return_value=cfg), \
            patch("kouhai_bot.handlers.shared.call_chat_completion_result", side_effect=fake_call_chat):
        result = asyncio.run(doublecheck_problem_summary(
            "components containing vertices p_i and p_i+1 respectively",
            "",
            "Time: 2s, Memory: 1024MB",
            "分别取含 pᵢ 和 pᵢ₊₁ 的弱连通分量",
        ))

    assert result.accurate is False
    assert result.issues[0]["severity"] == "critical"
    assert "p_i+1" in result.issues[0]["source_fact"]
    assert calls[0]["task"] == "summary"
    assert calls[0]["temperature"] == 0.0
    assert calls[0]["timeout"] == 321
    assert calls[0]["response_format"] == {"type": "json_object"}
    checker_prompt = calls[0]["messages"][-1]["content"]
    assert "p_i+1" in checker_prompt
    assert "p_{i+1}" in checker_prompt
    assert "candidate_summary" in checker_prompt


def test_doublecheck_problem_summary_rejects_inconsistent_or_malformed_payloads():
    cfg = _openai_cfg(summary_timeout_sec=321)
    payloads = [
        {
            "accurate": True,
            "issues": [{
                "severity": "major",
                "summary_claim": "候选说法",
                "source_fact": "题面事实",
                "explanation": "两者不一致",
            }],
        },
        {
            "accurate": False,
            "issues": [{
                "severity": "major",
                "summary_claim": "候选说法",
                "source_fact": {"unexpected": "object"},
                "explanation": "两者不一致",
            }],
        },
    ]

    for payload in payloads:
        with patch("kouhai_bot.handlers.shared.get_config", return_value=cfg), \
                patch(
                    "kouhai_bot.handlers.shared.call_chat_completion_result",
                    AsyncMock(return_value=ChatCompletionResult(text=json.dumps(payload))),
                ):
            result = asyncio.run(doublecheck_problem_summary(
                "statement",
                "input",
                "limits",
                "candidate",
            ))

        assert result.accurate is None
        assert result.failure_kind == "invalid_response"


def test_doublecheck_problem_summary_accepts_actionable_omission_issue():
    cfg = _openai_cfg(summary_timeout_sec=321)
    payload = {
        "accurate": False,
        "issues": [{
            "severity": "major",
            "summary_claim": "",
            "source_fact": "题面明确规定孤立点也算只有入边的点",
            "explanation": "候选简述遗漏了这项约定",
        }],
    }

    with patch("kouhai_bot.handlers.shared.get_config", return_value=cfg), \
            patch(
                "kouhai_bot.handlers.shared.call_chat_completion_result",
                AsyncMock(return_value=ChatCompletionResult(text=json.dumps(payload))),
            ):
        result = asyncio.run(doublecheck_problem_summary(
            "statement",
            "input",
            "limits",
            "candidate",
        ))

    assert result.accurate is False
    assert result.issues[0]["summary_claim"] == ""


def test_summary_doublecheck_tool_explicit_inputs_ignore_state_and_strip_thinking(tmp_path):
    cfg = _openai_cfg(data_dir=str(tmp_path), current_group=123)
    summary_file = tmp_path / "candidate.txt"
    summary_file.write_text("<think>hidden</think>\n候选简述", encoding="utf-8")
    checker = AsyncMock(return_value=SummaryDoublecheckResult(accurate=True))
    args = SimpleNamespace(
        group=None,
        pid="CF1900a",
        summary_file=summary_file,
        expect=None,
    )

    with patch.object(summary_doublecheck_tool, "get_config", return_value=cfg), \
            patch.object(
                summary_doublecheck_tool,
                "_load_json",
                side_effect=AssertionError("explicit inputs must not read group state"),
            ), \
            patch.object(
                summary_doublecheck_tool,
                "load_problem_statement_json",
                return_value={
                    "description": "statement",
                    "input": "input",
                    "time_limit": "2s",
                    "memory_limit": "256MB",
                },
            ) as statement_loader, \
            patch.object(summary_doublecheck_tool, "doublecheck_problem_summary", checker):
        exit_code = asyncio.run(summary_doublecheck_tool._run(args))

    assert exit_code == 0
    statement_loader.assert_called_once_with("1900A")
    checker.assert_awaited_once_with(
        "statement",
        "input",
        "Time: 2s, Memory: 256MB",
        "候选简述",
    )


def test_summarize_problem_repairs_symbol_scope_before_model_doublecheck():
    cfg = _openai_cfg(summary_timeout_sec=321)
    bad = "对 i=1..k-1，分别取 pᵢ 和 pᵢ₊₁ 所在分量的汇点并连边。"
    fixed = "对 i=1..k，分别取 pᵢ 和编号为 pᵢ+1 的顶点所在分量的汇点并连边。"
    responses = [
        ChatCompletionResult(text=json.dumps({"summary": bad}), model_tag="first"),
        ChatCompletionResult(text=json.dumps({"summary": fixed}), model_tag="fixed"),
        ChatCompletionResult(
            text=json.dumps({"accurate": True, "issues": []}),
            model_tag="checker",
        ),
    ]
    calls = []

    async def fake_call_chat(messages, **kwargs):
        calls.append({"messages": messages, **kwargs})
        return responses[len(calls) - 1]

    with patch("kouhai_bot.handlers.shared.get_config", return_value=cfg), \
            patch("kouhai_bot.handlers.shared.call_chat_completion_result", side_effect=fake_call_chat):
        summary, tag = asyncio.run(summarize_problem(
            "For i from 1 to m-1, use components containing p_i and p_i+1.",
            "",
            "",
        ))

    assert summary == fixed
    assert tag == "fixed"
    assert [call["temperature"] for call in calls] == [0.4, 0.2, 0.0]
    repair_payload = calls[1]["messages"][-1]["content"]
    assert "p_i+1" in repair_payload
    assert "pᵢ₊₁" in repair_payload


def test_summarize_problem_repairs_semantic_mismatch_and_doublechecks_again():
    cfg = _openai_cfg(summary_timeout_sec=321)
    bad = "若 aₚᵢ=0 则加边 u→v，否则加边 v→u。"
    fixed = "若 aₚᵢ=0 则加边 v→u，否则加边 u→v。"
    responses = [
        ChatCompletionResult(text=json.dumps({"summary": bad}), model_tag="first"),
        ChatCompletionResult(text=json.dumps({
            "accurate": False,
            "issues": [{
                "severity": "critical",
                "summary_claim": "aₚᵢ=0 时加边 u→v",
                "source_fact": "a_{p_i}=0 时加边 v→u",
                "explanation": "两个分支的边方向写反了。",
            }],
        }), model_tag="checker"),
        ChatCompletionResult(text=json.dumps({"summary": fixed}), model_tag="fixed"),
        ChatCompletionResult(
            text=json.dumps({"accurate": True, "issues": []}),
            model_tag="checker",
        ),
    ]
    calls = []

    async def fake_call_chat(messages, **kwargs):
        calls.append({"messages": messages, **kwargs})
        return responses[len(calls) - 1]

    with patch("kouhai_bot.handlers.shared.get_config", return_value=cfg), \
            patch("kouhai_bot.handlers.shared.call_chat_completion_result", side_effect=fake_call_chat):
        summary, tag = asyncio.run(summarize_problem(
            "If a_{p_i}=0, add v to u; otherwise add u to v.",
            "",
            "",
        ))

    assert summary == fixed
    assert tag == "fixed"
    assert [call["temperature"] for call in calls] == [0.4, 0.0, 0.2, 0.0]
    assert "边方向写反" in calls[2]["messages"][-1]["content"]


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



def test_zenmux_minimax_m3_payload_accepts_adaptive_thinking():
    provider = LlmProviderConfig(
        name="minimax-m3",
        api_key="sk-test",
        base_url="https://zenmux.ai/api/v1",
        model="minimax/minimax-m3",
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
            [{"role": "user", "content": "Reply OK."}],
            task="summary",
            thinking={"type": "adaptive"},
        ))

    assert result.text == "OK"
    assert calls[0]["model"] == "minimax/minimax-m3"
    assert calls[0]["thinking"] == {"type": "adaptive"}
    assert "enable_thinking" not in calls[0]
    assert "thinking_budget" not in calls[0]
    assert "reasoning_effort" not in calls[0]


def test_provider_payload_options_control_thinking_temperature_and_extra_body():
    provider = LlmProviderConfig(
        name="custom-zenmux",
        api_key="sk-test",
        base_url="https://zenmux.ai/api/v1",
        model="provider/custom-model",
        reasoning_effort="max",
        send_thinking=False,
        temperature=1.0,
        extra_body={"service_tier": "priority"},
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
    assert calls[0]["model"] == "provider/custom-model"
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["reasoning_effort"] == "max"
    assert calls[0]["temperature"] == 1.0
    assert calls[0]["service_tier"] == "priority"
    assert "thinking" not in calls[0]
    assert "enable_thinking" not in calls[0]
    assert "thinking_budget" not in calls[0]
    assert "stream" not in calls[0]


def test_zenmux_grok45free_payload_uses_xhigh_reasoning_and_tag():
    provider = LlmProviderConfig(
        name="grok45free",
        api_key="sk-test",
        base_url="https://zenmux.ai/api/v1",
        model="x-ai/grok-4.5-free",
        reasoning_effort="xhigh",
        model_tag="『∅』",
    )
    cfg = _openai_cfg(llm_smart_providers=[provider])
    calls = []

    async def fake_once(session, **kwargs):
        calls.append(kwargs["payload"].copy())
        return _ChatCompletionAttempt(text="OK", retryable=False, retry_after_sec=None)

    with patch("kouhai_bot.llm.get_config", return_value=cfg), \
            patch("kouhai_bot.llm.aiohttp.ClientSession", _DummySession), \
            patch("kouhai_bot.llm._post_chat_completion_once", side_effect=fake_once):
        result = asyncio.run(call_chat_completion_result(
            [{"role": "user", "content": "Reply OK."}],
            task="judge",
            thinking={"type": "enabled"},
        ))

    assert result.text == "OK"
    assert result.model_tag == "『∅』"
    assert calls[0]["model"] == "x-ai/grok-4.5-free"
    assert calls[0]["reasoning_effort"] == "xhigh"
    assert calls[0]["thinking"] == {"type": "enabled"}
    assert "thinking_budget" not in calls[0]


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


def test_zenmux_minimax_payload_does_not_use_fable_temperature_special_case():
    provider = LlmProviderConfig(
        name="minimax-m3",
        api_key="sk-test",
        base_url="https://zenmux.ai/api/v1",
        model="minimax/minimax-m3",
        reasoning_effort="high",
    )
    cfg = _openai_cfg(llm_general_providers=[provider])
    calls = []

    async def fake_once(session, **kwargs):
        calls.append(kwargs["payload"].copy())
        return _ChatCompletionAttempt(text="OK", retryable=False, retry_after_sec=None)

    with patch("kouhai_bot.llm.get_config", return_value=cfg), \
            patch("kouhai_bot.llm.aiohttp.ClientSession", _DummySession), \
            patch("kouhai_bot.llm._post_chat_completion_once", side_effect=fake_once):
        result = asyncio.run(call_chat_completion(
            [{"role": "user", "content": "Reply with exactly OK."}],
            task="summary",
        ))

    assert result == "OK"
    assert calls[0]["model"] == "minimax/minimax-m3"
    assert calls[0]["reasoning_effort"] == "high"
    assert calls[0]["temperature"] == 0.7



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


def test_dashscope_stream_payload_requests_usage_without_token_caps():
    provider = LlmProviderConfig(
        name="glm52",
        api_key="sk-test",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="glm-5.2",
        reasoning_effort="max",
    )
    cfg = _openai_cfg(llm_smart_providers=[provider])
    calls = []

    async def fake_once(session, **kwargs):
        calls.append(kwargs["payload"].copy())
        return _ChatCompletionAttempt(text="OK", retryable=False, retry_after_sec=None)

    with patch("kouhai_bot.llm.get_config", return_value=cfg), \
            patch("kouhai_bot.llm.aiohttp.ClientSession", _DummySession), \
            patch("kouhai_bot.llm._post_chat_completion_once", side_effect=fake_once):
        result = asyncio.run(call_chat_completion_result(
            [{"role": "user", "content": "Reply OK."}],
            task="judge",
            response_format={"type": "json_object"},
            thinking={"type": "enabled"},
        ))

    assert result.text == "OK"
    assert calls[0]["stream"] is True
    assert calls[0]["stream_options"] == {"include_usage": True}
    assert calls[0]["enable_thinking"] is True
    assert calls[0]["thinking_budget"] == 100000
    assert calls[0]["reasoning_effort"] == "max"
    forbidden = {
        "max_tokens",
        "max_completion_tokens",
        "max_output_tokens",
        "reasoning_budget",
    }
    assert forbidden.isdisjoint(calls[0])


def test_non_streaming_chat_completion_strips_leaked_thinking():
    response = _DummyResponse(json_data={
        "choices": [
            {
                "message": {
                    "content": "<think>hidden reasoning</think>Visible answer"
                }
            }
        ]
    })
    session = _PostSession(response)

    result = asyncio.run(_post_chat_completion_once(
        session,
        provider_name="openai",
        base_url="https://api.openai.com/v1",
        headers={},
        payload={},
        timeout=120,
    ))

    assert result.text == "Visible answer"
    assert result.retryable is False


def test_strip_leaked_thinking_strips_common_thinking_tag_variants():
    assert strip_leaked_thinking("<thinking>hidden reasoning</thinking>Visible answer") == "Visible answer"
    assert strip_leaked_thinking("<analysis>hidden</analysis>Visible") == "Visible"
    assert strip_leaked_thinking("<chain-of-thought>hidden</chain-of-thought>Visible") == "Visible"


def test_non_streaming_chat_completion_strips_leaked_thinking_tag():
    response = _DummyResponse(json_data={
        "choices": [
            {
                "message": {
                    "content": "<thinking>hidden reasoning</thinking>Visible answer"
                }
            }
        ]
    })
    session = _PostSession(response)

    result = asyncio.run(_post_chat_completion_once(
        session,
        provider_name="claude_gateway",
        base_url="https://api.example.com/v1",
        headers={},
        payload={},
        timeout=120,
    ))

    assert result.text == "Visible answer"
    assert result.retryable is False


def test_non_streaming_chat_completion_drops_unclosed_leaked_thinking():
    response = _DummyResponse(json_data={
        "choices": [
            {
                "message": {
                    "content": "Visible prefix\n<think>hidden reasoning without close"
                }
            }
        ]
    })
    session = _PostSession(response)

    result = asyncio.run(_post_chat_completion_once(
        session,
        provider_name="openai",
        base_url="https://api.openai.com/v1",
        headers={},
        payload={},
        timeout=120,
    ))

    assert result.text == "Visible prefix"
    assert result.retryable is False


def test_non_streaming_chat_completion_retries_when_only_leaked_thinking(caplog):
    caplog.set_level("WARNING", logger="kouhai-bot.llm")
    response = _DummyResponse(json_data={
        "choices": [
            {
                "message": {
                    "content": "<think>hidden reasoning</think>"
                }
            }
        ]
    })
    session = _PostSession(response)

    result = asyncio.run(_post_chat_completion_once(
        session,
        provider_name="openai",
        base_url="https://api.openai.com/v1",
        headers={},
        payload={},
        timeout=120,
    ))

    assert result.text is None
    assert result.retryable is True
    assert result.failure_kind == "service_unavailable"
    assert "returned no text after cleanup" in caplog.text


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


def test_streaming_chat_completion_strips_leaked_thinking_tag_blocks():
    response = _DummyResponse(lines=[
        'data: {"choices":[{"delta":{"content":"<think>hidden"}}]}\n',
        '\n',
        'data: {"choices":[{"delta":{"content":" reasoning</think>Visible"}}]}\n',
        '\n',
        'data: {"choices":[{"delta":{"content":" answer"}}]}\n',
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

    assert result.text == "Visible answer"
    assert result.retryable is False


def test_streaming_chat_completion_strips_leaked_thinking_blocks():
    response = _DummyResponse(lines=[
        'data: {"choices":[{"delta":{"content":"<thinking>hidden"}}]}\n',
        '\n',
        'data: {"choices":[{"delta":{"content":" reasoning</thinking>Visible"}}]}\n',
        '\n',
        'data: {"choices":[{"delta":{"content":" answer"}}]}\n',
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

    assert result.text == "Visible answer"
    assert result.retryable is False


def test_streaming_chat_completion_drops_unclosed_leaked_thinking_block():
    response = _DummyResponse(lines=[
        'data: {"choices":[{"delta":{"content":"Visible prefix"}}]}\n',
        '\n',
        'data: {"choices":[{"delta":{"content":"<think>hidden"}}]}\n',
        '\n',
        'data: {"choices":[{"delta":{"content":" reasoning without close"}}]}\n',
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

    assert result.text == "Visible prefix"
    assert result.retryable is False


def test_streaming_chat_completion_empty_after_reasoning_is_not_retried(caplog):
    caplog.set_level("INFO", logger="kouhai-bot.llm")
    response = _DummyResponse(lines=[
        'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}\n',
        '\n',
        'data: {"choices":[{"delta":{},"finish_reason":"length"}],"usage":{"completion_tokens_details":{"reasoning_tokens":1}}}\n',
        '\n',
        'data: [DONE]\n',
        '\n',
    ])
    session = _PostSession(response)

    result = asyncio.run(_post_chat_completion_once(
        session,
        provider_name="glm52",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        headers={},
        payload={"stream": True},
        timeout=120,
    ))

    assert result.text is None
    assert result.retryable is False
    assert result.failure_kind == "length"
    assert result.finish_reason == "length"
    assert result.reasoning_chars == 8
    assert result.usage == {"completion_tokens_details": {"reasoning_tokens": 1}}
    assert "finish_reason=length" in caplog.text


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
