import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot import config
from kouhai_bot.config import BotConfig


@pytest.fixture(autouse=True)
def _clear_official_env(monkeypatch):
    """Official credential env fallback must not leak in from the real env."""
    monkeypatch.delenv("KOUHAI_APP_ID", raising=False)
    monkeypatch.delenv("KOUHAI_APP_SECRET", raising=False)


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


def test_current_group_optional_defaults_to_zero(monkeypatch, tmp_path):
    """Official mode is multi-group: current_group is no longer a hard gate."""
    data = yaml.safe_load(_make_yaml())
    del data["current_group"]
    cfg = _from_yaml(yaml.dump(data), monkeypatch, tmp_path)
    assert cfg.current_group == 0


def test_bot_qq_optional_defaults_to_zero(monkeypatch, tmp_path):
    """Official bots have no numeric QQ identity requirement."""
    data = yaml.safe_load(_make_yaml())
    data["bot_qq"] = 0
    cfg = _from_yaml(yaml.dump(data), monkeypatch, tmp_path)
    assert cfg.bot_qq == 0

    del data["bot_qq"]
    cfg = _from_yaml(yaml.dump(data), monkeypatch, tmp_path)
    assert cfg.bot_qq == 0


def test_legacy_bot_qq_still_parsed_when_present(monkeypatch, tmp_path):
    cfg = _from_yaml(_make_yaml(bot_qq=1900000001), monkeypatch, tmp_path)
    assert cfg.bot_qq == 1900000001


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


def test_config_allows_missing_qwen_model(monkeypatch, tmp_path):
    data = yaml.safe_load(_make_yaml())
    data["qwen"]["model"] = ""
    cfg = _from_yaml(yaml.dump(data), monkeypatch, tmp_path)
    assert cfg.qwen_model == ""


def test_config_jev_base_url_has_no_default(monkeypatch, tmp_path):
    cfg = _from_yaml(_make_yaml(), monkeypatch, tmp_path)
    assert cfg.jev_base_url == ""


def test_config_jev_empty_base_url_does_not_fall_back(monkeypatch, tmp_path):
    data = yaml.safe_load(_make_yaml())
    data["jev"] = {"api_key": "sk-jev", "base_url": ""}
    cfg = _from_yaml(yaml.dump(data), monkeypatch, tmp_path)
    assert cfg.jev_base_url == ""


def test_config_jev_base_url_strips_trailing_slash(monkeypatch, tmp_path):
    data = yaml.safe_load(_make_yaml())
    data["jev"] = {"api_key": "sk-jev", "base_url": "https://jev.test/"}
    cfg = _from_yaml(yaml.dump(data), monkeypatch, tmp_path)
    assert cfg.jev_base_url == "https://jev.test"


def test_config_appends_legacy_multimodal_model_queue_to_general(monkeypatch, tmp_path):
    data = yaml.safe_load(_make_yaml())
    data["llm"]["multimodal_model"] = [
        # same name as the general entry → deduplicated away
        {
            "name": "general-test",
            "api_key": "sk-mm",
            "base_url": "http://localhost:8080/v1",
            "model": "legacy-dup",
        },
        {
            "name": "mmx",
            "api_key": "sk-mm",
            "base_url": "http://localhost:8080/v1",
            "model": "mmx-vision",
            "model_tag": "『MMx』",
        },
    ]
    cfg = _from_yaml(yaml.dump(data), monkeypatch, tmp_path)
    assert not hasattr(cfg, "llm_multimodal_providers")
    assert [p.name for p in cfg.llm_general_providers] == ["general-test", "mmx"]
    assert cfg.llm_general_providers[1].model == "mmx-vision"


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
            "stream_progress_timeout_sec": 457,
            "judge_timeout_sec": 1500,
            "clarify_timeout_sec": 700,
            "review_timeout_sec": 800,
            "summary_timeout_sec": 180,
        }
    )
    cfg = _from_yaml(yaml_str, monkeypatch, tmp_path)
    assert cfg.llm_stream_idle_timeout_sec == 456
    assert cfg.llm_stream_progress_timeout_sec == 457
    assert cfg.judge_timeout_sec == 1500
    assert cfg.clarify_timeout_sec == 700
    assert cfg.review_timeout_sec == 800
    assert cfg.summary_timeout_sec == 180


def test_llm_stream_idle_timeout_defaults_to_120(monkeypatch, tmp_path):
    cfg = _from_yaml(_make_yaml(), monkeypatch, tmp_path)

    assert cfg.llm_stream_idle_timeout_sec == 120


def test_llm_stream_progress_timeout_defaults_to_90(monkeypatch, tmp_path):
    cfg = _from_yaml(_make_yaml(), monkeypatch, tmp_path)

    assert cfg.llm_stream_progress_timeout_sec == 90


def test_provider_stream_loads_from_yaml(monkeypatch, tmp_path):
    data = yaml.safe_load(_make_yaml())
    data["llm"]["smart_model"][0]["stream"] = True
    cfg = _from_yaml(yaml.dump(data), monkeypatch, tmp_path)

    assert cfg.llm_smart_providers[0].stream is True


def test_provider_payload_options_load_from_yaml(monkeypatch, tmp_path):
    data = yaml.safe_load(_make_yaml())
    provider = data["llm"]["smart_model"][0]
    provider["send_thinking"] = False
    provider["send_response_format"] = False
    provider["temperature"] = 1
    provider["extra_body"] = {"service_tier": "priority"}
    cfg = _from_yaml(yaml.dump(data), monkeypatch, tmp_path)

    loaded = cfg.llm_smart_providers[0]
    assert loaded.send_thinking is False
    assert loaded.send_response_format is False
    assert loaded.temperature == 1.0
    assert loaded.extra_body == {"service_tier": "priority"}


def test_provider_send_response_format_defaults_true(monkeypatch, tmp_path):
    cfg = _from_yaml(_make_yaml(), monkeypatch, tmp_path)

    assert cfg.llm_smart_providers[0].send_response_format is True


def test_provider_extra_body_must_be_mapping(monkeypatch, tmp_path):
    data = yaml.safe_load(_make_yaml())
    data["llm"]["smart_model"][0]["extra_body"] = "bad"

    with pytest.raises(RuntimeError, match="extra_body"):
        _from_yaml(yaml.dump(data), monkeypatch, tmp_path)


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


def test_official_section_loaded(monkeypatch, tmp_path):
    monkeypatch.delenv("KOUHAI_APP_ID", raising=False)
    monkeypatch.delenv("KOUHAI_APP_SECRET", raising=False)
    cfg = _from_yaml(_make_yaml(official={
        "app_id": "1900000000", "app_secret": "secret", "bot_name": "助手",
        "bot_avatar_url": "https://cdn.example.test/bot.png",
    }), monkeypatch, tmp_path)
    assert cfg.app_id == "1900000000"
    assert cfg.app_secret == "secret"
    assert cfg.official_bot_name == "助手"
    assert cfg.official_bot_avatar_url == "https://cdn.example.test/bot.png"
    assert cfg.official_api_base == "https://api.bot.qq.com"
    cfg.validate_official()  # no raise


def test_official_sandbox_base(monkeypatch, tmp_path):
    cfg = _from_yaml(_make_yaml(official={
        "app_id": "1", "app_secret": "s", "sandbox": True,
    }), monkeypatch, tmp_path)
    assert cfg.official_api_base == "https://sandbox.api.sgroup.qq.com"


def test_official_env_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("KOUHAI_APP_ID", "env-app")
    monkeypatch.setenv("KOUHAI_APP_SECRET", "env-secret")
    cfg = _from_yaml(_make_yaml(), monkeypatch, tmp_path)
    assert cfg.app_id == "env-app"
    assert cfg.app_secret == "env-secret"


def test_validate_official_missing_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("KOUHAI_APP_ID", raising=False)
    monkeypatch.delenv("KOUHAI_APP_SECRET", raising=False)
    cfg = _from_yaml(_make_yaml(), monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="app_id"):
        cfg.validate_official()


def test_official_defaults(monkeypatch, tmp_path):
    cfg = _from_yaml(_make_yaml(), monkeypatch, tmp_path)
    assert cfg.app_id == ""
    assert cfg.app_secret == ""
    assert cfg.official_api_base == "https://api.bot.qq.com"
    assert cfg.official_token_base == "https://api.bot.qq.com"
    assert cfg.official_sandbox is False
    assert cfg.official_bot_name == ""
    assert cfg.official_bot_avatar_url == ""
    assert cfg.official_intents == (1 << 25) | (1 << 26) | (1 << 24)


def test_validate_official_missing_secret_raises(monkeypatch, tmp_path):
    cfg = _from_yaml(
        _make_yaml(official={"app_id": "1900000000"}), monkeypatch, tmp_path
    )
    with pytest.raises(RuntimeError, match="app_secret"):
        cfg.validate_official()


def test_official_custom_bases_strip_trailing_slash(monkeypatch, tmp_path):
    cfg = _from_yaml(
        _make_yaml(official={
            "app_id": "1900000000",
            "app_secret": "secret",
            "api_base": "https://api.example.test/",
            "token_base": "https://token.example.test/",
        }),
        monkeypatch,
        tmp_path,
    )
    assert cfg.official_api_base == "https://api.example.test"
    assert cfg.official_token_base == "https://token.example.test"


def test_official_explicit_intents_override_default(monkeypatch, tmp_path):
    cfg = _from_yaml(
        _make_yaml(official={"app_id": "1", "app_secret": "s", "intents": 7}),
        monkeypatch,
        tmp_path,
    )
    assert cfg.official_intents == 7


def test_official_sandbox_keeps_explicit_api_base(monkeypatch, tmp_path):
    cfg = _from_yaml(
        _make_yaml(official={
            "app_id": "1", "app_secret": "s", "sandbox": True,
            "api_base": "https://custom.example.test",
        }),
        monkeypatch,
        tmp_path,
    )
    assert cfg.official_sandbox is True
    assert cfg.official_api_base == "https://custom.example.test"


def test_official_yaml_credentials_take_precedence_over_env(monkeypatch, tmp_path):
    monkeypatch.setenv("KOUHAI_APP_ID", "env-app")
    monkeypatch.setenv("KOUHAI_APP_SECRET", "env-secret")
    cfg = _from_yaml(
        _make_yaml(official={"app_id": "yaml-app", "app_secret": "yaml-secret"}),
        monkeypatch,
        tmp_path,
    )
    assert cfg.app_id == "yaml-app"
    assert cfg.app_secret == "yaml-secret"


def test_official_credentials_are_stripped(monkeypatch, tmp_path):
    cfg = _from_yaml(
        _make_yaml(official={"app_id": " 1900000000 ", "app_secret": " secret "}),
        monkeypatch,
        tmp_path,
    )
    assert cfg.app_id == "1900000000"
    assert cfg.app_secret == "secret"


def test_official_non_mapping_section_ignored(monkeypatch, tmp_path):
    cfg = _from_yaml(_make_yaml(official="not-a-mapping"), monkeypatch, tmp_path)
    assert cfg.app_id == ""
    assert cfg.official_api_base == "https://api.bot.qq.com"
