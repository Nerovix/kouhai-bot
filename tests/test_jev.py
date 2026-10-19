import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.config import BotConfig
from kouhai_bot import jev
from kouhai_bot.handlers.shared import (
    build_jev_second_judge_request,
    build_second_judge_messages,
)


PROBLEM = "Given n, output n+1.\nInput: n (1 <= n <= 10)."
SUBMISSION = "print(n + 1)"
HISTORY = [
    {
        "type": "submit",
        "result": "incorrect",
        "content": "first attempt",
        "timestamp": "2026-10-19T10:00:00+08:00",
    }
]
EDITORIAL = "The answer is n+1 because the statement asks for the successor."


def test_jev_request_reuses_second_judge_prompt():
    request = build_jev_second_judge_request(
        PROBLEM, SUBMISSION, HISTORY, EDITORIAL, "editorial source"
    )
    principles = request["state"]["judging_principles"]
    # Same judging rules as the production second review...
    assert "2) 二审不是题解匹配器" in principles
    assert "8) 在判 correct=true 前" in principles
    # ...minus the JSON output contract Jev cannot honour.
    assert "只输出 JSON" not in principles
    assert "输出示例" not in principles

    task = request["state"]["review_task"]
    assert task["problem_statement"] == PROBLEM
    assert task["current_submission"] == SUBMISSION
    assert task["official_reference"]["editorial"] == EDITORIAL
    assert task["official_reference"]["source"] == "editorial source"
    assert task["first_pass"]["verdict"] is None

    question = request["questions"]["verdict"]
    assert question["type"] == "choice"
    assert set(question["criteria"]) == {"correct", "incorrect"}


def test_jev_request_task_matches_second_judge_user_message():
    request = build_jev_second_judge_request(
        PROBLEM, SUBMISSION, HISTORY, EDITORIAL, "source"
    )
    messages = build_second_judge_messages(
        PROBLEM, SUBMISSION, HISTORY, {}, EDITORIAL, "source"
    )
    assert request["state"]["review_task"] == json.loads(messages[1]["content"])


def _cfg(**overrides) -> BotConfig:
    cfg = BotConfig()
    cfg.jev_api_key = "test-key"
    cfg.jev_base_url = "https://www.rightapi.ai"
    cfg.jev_model = "jev-latest"
    cfg.jev_timeout_sec = 5
    cfg.jev_max_retries = 1
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


SUCCESS_BODY = json.dumps(
    {
        "model": "jev-1.13.0",
        "answers": {
            "verdict": {
                "type": "choice",
                "choice": "correct",
                "confidence": 0.76,
                "probabilities": {"correct": 0.88, "incorrect": 0.12},
            }
        },
        "usage": {"input_tokens": 340, "output_tokens": 32},
    }
)


@pytest.mark.asyncio
async def test_run_jev_parses_choice(monkeypatch):
    monkeypatch.setattr(jev, "get_config", lambda: _cfg())

    async def fake_post(url, api_key, payload, timeout):
        assert url == "https://www.rightapi.ai/v1/systemone"
        assert payload["model"] == "jev-latest"
        assert api_key == "test-key"
        return 200, SUCCESS_BODY

    monkeypatch.setattr(jev, "_post_once", fake_post)
    result = await jev.run_jev({"x": 1}, {"verdict": {"type": "choice"}})
    assert result.choice == "correct"
    assert result.model == "jev-1.13.0"
    assert result.probabilities == {"correct": 0.88, "incorrect": 0.12}
    assert result.confidence == pytest.approx(0.76)
    assert result.input_tokens == 340
    assert result.failure_kind == ""


@pytest.mark.asyncio
async def test_run_jev_retries_overloaded_upstream(monkeypatch):
    monkeypatch.setattr(jev, "get_config", lambda: _cfg())

    class _NoSleep:
        async def __call__(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(jev.asyncio, "sleep", _NoSleep())
    calls = []

    async def fake_post(url, api_key, payload, timeout):
        calls.append(1)
        if len(calls) == 1:
            return 529, "upstream overloaded"
        return 200, SUCCESS_BODY

    monkeypatch.setattr(jev, "_post_once", fake_post)
    result = await jev.run_jev({"x": 1}, {"verdict": {"type": "choice"}})
    assert result.choice == "correct"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_run_jev_rejects_malformed_answer(monkeypatch):
    monkeypatch.setattr(jev, "get_config", lambda: _cfg())

    async def fake_post(url, api_key, payload, timeout):
        return 200, json.dumps({"answers": {}})

    monkeypatch.setattr(jev, "_post_once", fake_post)
    result = await jev.run_jev({"x": 1}, {"verdict": {"type": "choice"}})
    assert result.choice is None
    assert result.failure_kind == "malformed"


@pytest.mark.asyncio
async def test_run_jev_accepts_v1_suffixed_base(monkeypatch):
    monkeypatch.setattr(
        jev, "get_config", lambda: _cfg(jev_base_url="https://www.rightapi.ai/v1/")
    )

    async def fake_post(url, api_key, payload, timeout):
        assert url == "https://www.rightapi.ai/v1/systemone"
        return 200, SUCCESS_BODY

    monkeypatch.setattr(jev, "_post_once", fake_post)
    result = await jev.run_jev({"x": 1}, {"verdict": {"type": "choice"}})
    assert result.choice == "correct"


@pytest.mark.asyncio
async def test_run_jev_requires_api_key(monkeypatch):
    monkeypatch.setattr(jev, "get_config", lambda: _cfg(jev_api_key=""))

    async def fake_post(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("no request should be made without a key")

    monkeypatch.setattr(jev, "_post_once", fake_post)
    result = await jev.run_jev({"x": 1}, {"verdict": {"type": "choice"}})
    assert result.failure_kind == "not_configured"
