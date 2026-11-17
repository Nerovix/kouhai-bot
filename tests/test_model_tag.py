"""Model-tag plumbing: display-only metadata, never LLM-visible.

Covers llm.append_model_tag (rstrip, no doubling), llm.strip_model_tags
(trailing configured tags removed iteratively, mid-text preserved),
configured_model_tags across both provider queues, and the prompt builders
(shared._history_dialogue, submit._build_review_history) plus the history
card rendering the tag exactly once for legacy embedded-tag records.

Private-judge / sync-cheer / UserGroupConfig / top5 coverage is dropped
(those features were removed in the official-API migration).
"""

from __future__ import annotations

import pytest

from kouhai_bot.config import BotConfig
from kouhai_bot.handlers.cards import build_history_card_nodes
from kouhai_bot.handlers.cmd.submit import _build_review_history
from kouhai_bot.handlers.shared import _history_dialogue
from kouhai_bot.llm import (
    append_model_tag,
    configured_model_tags,
    strip_model_tags,
)
from kouhai_bot.llm_config import LlmProviderConfig

TAG = "🐳"
TAG2 = "֎AI"


def _provider(name: str, tag: str) -> LlmProviderConfig:
    return LlmProviderConfig(
        name=name, api_key="test-key",
        base_url="https://llm.example.invalid/v1", model="m", model_tag=tag,
    )


@pytest.fixture
def tagged_config(tmp_path, monkeypatch):
    cfg = BotConfig(data_dir=str(tmp_path))
    cfg.llm_smart_providers = [_provider("smart-a", TAG), _provider("smart-b", "")]
    cfg.llm_general_providers = [_provider("general-a", TAG2), _provider("general-b", TAG)]
    monkeypatch.setattr("kouhai_bot.config._config", cfg)
    return cfg


@pytest.fixture
def unconfigured(tmp_path, monkeypatch):
    monkeypatch.setattr("kouhai_bot.config._config", None)
    monkeypatch.setenv("KOUHAI_CONFIG", str(tmp_path / "missing.yaml"))


# ── append_model_tag ────────────────────────────────────────────────────


def test_append_direct_no_separator():
    assert append_model_tag("判对了", TAG) == "判对了" + TAG


def test_append_empty_tag_is_noop_but_rstrips():
    assert append_model_tag("判对了\n", "") == "判对了"


def test_append_rstrips_before_appending():
    assert append_model_tag("判对了 \n", TAG) == "判对了" + TAG


def test_append_does_not_double_an_echoed_tag():
    assert append_model_tag("判对了" + TAG, TAG) == "判对了" + TAG
    # trailing whitespace after the echoed tag is still normalized
    assert append_model_tag("判对了" + TAG + "\n", TAG) == "判对了" + TAG


def test_append_different_tag_still_appends():
    assert append_model_tag("判对了" + TAG, TAG2) == "判对了" + TAG + TAG2


# ── strip_model_tags ────────────────────────────────────────────────────


def test_strip_trailing_tag_and_rstrip():
    assert strip_model_tags("判对了" + TAG, [TAG]) == "判对了"
    assert strip_model_tags("判对了 " + TAG, [TAG]) == "判对了"


def test_strip_stacked_tags_iteratively():
    assert strip_model_tags("判对了" + TAG + TAG2, [TAG, TAG2]) == "判对了"
    assert strip_model_tags("判对了" + TAG2 + TAG, [TAG, TAG2]) == "判对了"


def test_strip_preserves_mid_text_occurrences():
    assert strip_model_tags(f"用户引用了 {TAG} 这个符号", [TAG]) == f"用户引用了 {TAG} 这个符号"
    assert strip_model_tags(f"a{TAG}b", [TAG]) == f"a{TAG}b"


def test_strip_unknown_tag_and_empty_text():
    assert strip_model_tags("判对了" + TAG, [TAG2]) == "判对了" + TAG
    assert strip_model_tags("", [TAG]) == ""
    assert strip_model_tags("判对了", []) == "判对了"


def test_strip_uses_configured_tags_by_default(tagged_config):
    assert strip_model_tags("判对了" + TAG) == "判对了"
    assert strip_model_tags("解释如下" + TAG2) == "解释如下"
    # a tag nobody configured is user content and stays
    assert strip_model_tags("判对了『未配置』") == "判对了『未配置』"


def test_strip_without_config_leaves_text_untouched(unconfigured):
    assert configured_model_tags() == []
    assert strip_model_tags("判对了" + TAG) == "判对了" + TAG


# ── configured_model_tags ───────────────────────────────────────────────


def test_configured_tags_merge_queues_dedup_and_skip_empty(tagged_config):
    assert configured_model_tags() == [TAG, TAG2]


def test_configured_tags_strip_whitespace(tmp_path, monkeypatch):
    cfg = BotConfig(data_dir=str(tmp_path))
    cfg.llm_smart_providers = [_provider("p", "  🐳  ")]
    monkeypatch.setattr("kouhai_bot.config._config", cfg)
    assert configured_model_tags() == [TAG]


# ── prompt builders never show tags to the LLM ──────────────────────────


def test_history_dialogue_strips_reply_and_reason_tags(tagged_config):
    history = [
        {
            "type": "submit", "result": "incorrect",
            "content": "我的做法", "reply": "", "reason": "复杂度不对" + TAG,
        },
        {
            "type": "clarify", "result": "clarify",
            "content": "疑问", "reply": "解释如下" + TAG2, "reason": "",
        },
    ]
    dialogue = _history_dialogue(history)
    assert [d["role"] for d in dialogue] == ["user", "assistant", "user", "assistant"]
    assert dialogue[1]["content"] == "复杂度不对"
    assert dialogue[1]["kind"] == "submit_verdict_reason"
    assert dialogue[3]["content"] == "解释如下"
    assert dialogue[3]["kind"] == "clarify_feedback"
    assert all(TAG not in d["content"] and TAG2 not in d["content"] for d in dialogue)


def test_history_dialogue_reply_wins_over_reason(tagged_config):
    history = [{
        "type": "submit", "result": "incorrect",
        "content": "做法", "reply": "回复" + TAG, "reason": "理由" + TAG,
    }]
    dialogue = _history_dialogue(history)
    assert len(dialogue) == 2
    assert dialogue[1]["content"] == "回复"
    assert dialogue[1]["note"] == "bot_feedback_not_user_claim"


def test_history_dialogue_keeps_user_content_verbatim(tagged_config):
    history = [{
        "type": "submit", "result": "pending",
        "content": f"我引用 {TAG} 符号", "reply": "", "reason": "",
    }]
    dialogue = _history_dialogue(history)
    assert dialogue[0]["content"] == f"我引用 {TAG} 符号"
    assert dialogue[0]["note"] == "user_claim"


def test_build_review_history_strips_tags(tagged_config):
    history = [
        {
            "type": "submit", "result": "incorrect",
            "content": "做法", "reason": "判定" + TAG, "reply": "回复" + TAG2,
        },
        {
            "type": "clarify", "result": "clarify",
            "content": "疑问", "reason": "", "reply": "解释" + TAG,
        },
    ]
    text = _build_review_history(history)
    assert "Judge判定: 判定\n" in text
    assert "Bot回复: 回复\n" in text
    assert "Bot回复: 解释\n" in text
    assert TAG not in text and TAG2 not in text
    assert "统计：clarify=1，submit=1（correct=0，incorrect=1），review=0。" in text


# ── history card renders the tag exactly once ───────────────────────────


def test_history_card_renders_tag_once_for_legacy_and_new_records(tagged_config):
    records = [
        # legacy echo: tag embedded in the stored reply AND the dedicated
        # field set → strip the echo, append once
        {"type": "submit", "result": "correct", "content": "旧做法",
         "reply": "判对了" + TAG, "reason": "", "model_tag": TAG},
        # new: raw reply + dedicated model_tag field
        {"type": "submit", "result": "correct", "content": "新做法",
         "reply": "判对了", "reason": "", "model_tag": TAG},
        # incorrect with empty reply → reason fallback carries the tag once
        {"type": "submit", "result": "incorrect", "content": "错做法",
         "reply": "", "reason": "不对", "model_tag": TAG},
    ]
    nodes = build_history_card_nodes(records, user_name="Alice", bot_name="算法助手")
    bot_texts = [n["content_text"] for n in nodes if n["nickname"] == "算法助手"]
    assert bot_texts == ["判对了" + TAG, "判对了" + TAG, "不对。再想想？🤔" + TAG]
    assert all(t.count(TAG) == 1 for t in bot_texts)