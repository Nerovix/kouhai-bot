from types import SimpleNamespace

from kouhai_bot import config
from kouhai_bot.problems import fetcher


def test_qwen_config_uses_yaml_config_when_env_missing(monkeypatch):
    monkeypatch.setattr(fetcher, "QWEN_API_KEY", "")
    monkeypatch.setattr(fetcher, "QWEN_BASE_URL", "https://env.example/v1")
    monkeypatch.setattr(fetcher, "QWEN_MODEL", "")
    monkeypatch.setattr(
        config,
        "_config",
        SimpleNamespace(
            qwen_api_key="sk-from-config",
            qwen_base_url="https://dashscope.example/v1/",
            qwen_model="qwen-vl-test",
        ),
    )
    monkeypatch.delenv("QWEN_BASE_URL", raising=False)

    assert fetcher._qwen_config() == (
        "sk-from-config",
        "https://dashscope.example/v1",
        "qwen-vl-test",
    )
