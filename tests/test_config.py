import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot import config
from kouhai_bot.config import BotConfig


def _make_yaml(**overrides) -> str:
    """Build a minimal valid config.yaml string with required fields."""
    data = {
        "bot_qq": 1234567890,
        "current_group": 999999,
        "llm": {
            "smart_model": [
                {
                    "name": "smart-test",
                    "api_key": "sk-test",
                    "base_url": "http://localhost:8080/v1",
                    "model": "test-smart-model",
                }
            ],
            "general_model": [
                {
                    "name": "general-test",
                    "api_key": "sk-test",
                    "base_url": "http://localhost:8080/v1",
                    "model": "test-general-model",
                }
            ],
        },
        "qwen": {
            "api_key": "sk-qwen",
            "model": "qwen-vl-max",
        },
    }
    data.update(overrides)
    return yaml.dump(data)


def _from_yaml(yaml_str: str, monkeypatch, tmp_path) -> BotConfig:
    """Create a temp config file and load it, disabling dotenv."""
    monkeypatch.setattr(config, "_try_load_dotenv", lambda: None)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml_str, encoding="utf-8")
    monkeypatch.setenv("KOUHAI_CONFIG", str(config_path))
    return BotConfig.from_yaml()


def test_config_requires_current_group(monkeypatch, tmp_path):
    yaml_str = _make_yaml()
    data = yaml.safe_load(yaml_str)
    del data["current_group"]
    with pytest.raises(RuntimeError, match="current_group"):
        _from_yaml(yaml.dump(data), monkeypatch, tmp_path)


def test_config_requires_bot_qq(monkeypatch, tmp_path):
    data = yaml.safe_load(_make_yaml())
    data["bot_qq"] = 0
    with pytest.raises(RuntimeError, match="bot_qq"):
        _from_yaml(yaml.dump(data), monkeypatch, tmp_path)


def test_config_requires_smart_model_queue(monkeypatch, tmp_path):
    data = yaml.safe_load(_make_yaml())
    data["llm"]["smart_model"] = []
    with pytest.raises(RuntimeError, match="smart_model"):
        _from_yaml(yaml.dump(data), monkeypatch, tmp_path)


def test_config_requires_general_model_queue(monkeypatch, tmp_path):
    data = yaml.safe_load(_make_yaml())
    data["llm"]["general_model"] = []
    with pytest.raises(RuntimeError, match="general_model"):
        _from_yaml(yaml.dump(data), monkeypatch, tmp_path)


def test_config_requires_qwen_model(monkeypatch, tmp_path):
    data = yaml.safe_load(_make_yaml())
    data["qwen"]["model"] = ""
    with pytest.raises(RuntimeError, match="qwen.model"):
        _from_yaml(yaml.dump(data), monkeypatch, tmp_path)


def test_config_requires_provider_name(monkeypatch, tmp_path):
    data = yaml.safe_load(_make_yaml())
    data["llm"]["smart_model"][0]["name"] = ""
    with pytest.raises(RuntimeError, match="name"):
        _from_yaml(yaml.dump(data), monkeypatch, tmp_path)


def test_config_requires_provider_model(monkeypatch, tmp_path):
    data = yaml.safe_load(_make_yaml())
    data["llm"]["general_model"][0]["model"] = ""
    with pytest.raises(RuntimeError, match="model"):
        _from_yaml(yaml.dump(data), monkeypatch, tmp_path)


def test_legacy_single_provider_queue_does_not_satisfy_required_queues(monkeypatch, tmp_path):
    data = yaml.safe_load(_make_yaml())
    data["llm"] = {
        "providers": [
            {"name": "legacy", "api_key": "k", "base_url": "http://x/v1", "model": "m"}
        ]
    }
    with pytest.raises(RuntimeError, match="smart_model"):
        _from_yaml(yaml.dump(data), monkeypatch, tmp_path)


def test_llm_timeouts_load_from_yaml(monkeypatch, tmp_path):
    yaml_str = _make_yaml(
        llm={
            "smart_model": [
                {"name": "smart", "api_key": "k", "base_url": "http://x/v1", "model": "smart"}
            ],
            "general_model": [
                {"name": "general", "api_key": "k", "base_url": "http://x/v1", "model": "general"}
            ],
            "stream_idle_timeout_sec": 456,
            "judge_timeout_sec": 1500,
            "clarify_timeout_sec": 700,
            "review_timeout_sec": 800,
            "summary_timeout_sec": 180,
        }
    )
    cfg = _from_yaml(yaml_str, monkeypatch, tmp_path)
    assert cfg.llm_stream_idle_timeout_sec == 456
    assert cfg.judge_timeout_sec == 1500
    assert cfg.clarify_timeout_sec == 700
    assert cfg.review_timeout_sec == 800
    assert cfg.summary_timeout_sec == 180


def test_llm_stream_idle_timeout_defaults_to_120(monkeypatch, tmp_path):
    cfg = _from_yaml(_make_yaml(), monkeypatch, tmp_path)

    assert cfg.llm_stream_idle_timeout_sec == 120


def test_provider_stream_loads_from_yaml(monkeypatch, tmp_path):
    data = yaml.safe_load(_make_yaml())
    data["llm"]["smart_model"][0]["stream"] = True
    cfg = _from_yaml(yaml.dump(data), monkeypatch, tmp_path)

    assert cfg.llm_smart_providers[0].stream is True


def test_current_group_loaded(monkeypatch, tmp_path):
    cfg = _from_yaml(_make_yaml(current_group=123456), monkeypatch, tmp_path)
    assert cfg.current_group == 123456


def test_submit_ac_backdoor_loaded(monkeypatch, tmp_path):
    cfg = _from_yaml(_make_yaml(submit_ac_backdoor="open-sesame"), monkeypatch, tmp_path)
    assert cfg.submit_ac_backdoor == "open-sesame"


def test_provider_queue_not_list_raises(monkeypatch, tmp_path):
    data = yaml.safe_load(_make_yaml())
    data["llm"]["smart_model"] = "not-a-list"
    with pytest.raises(RuntimeError, match="must be a list"):
        _from_yaml(yaml.dump(data), monkeypatch, tmp_path)


def test_provider_entry_not_dict_raises(monkeypatch, tmp_path):
    data = yaml.safe_load(_make_yaml())
    data["llm"]["general_model"] = ["not-a-dict"]
    with pytest.raises(RuntimeError, match="must be a mapping"):
        _from_yaml(yaml.dump(data), monkeypatch, tmp_path)
