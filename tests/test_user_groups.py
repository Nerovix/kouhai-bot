import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.config import BotConfig, UserGroupConfig
from kouhai_bot.handlers.shared import get_problem_posted_at, mark_problem_posted
from kouhai_bot.user_groups import (
    DEFAULT_GROUP,
    configured_user_groups,
    format_group_submit_message,
    format_submit_wait,
    get_user_group,
    is_group_submit_blocked,
    submit_remaining_sec,
)


def _make_yaml(**overrides) -> str:
    data = {
        "current_group": 123,
        "llm": {
            "providers": [
                {"name": "t", "api_key": "k", "base_url": "http://x/v1", "model": "m"}
            ]
        },
        "qwen": {"api_key": "k", "model": "qwen-vl-max"},
    }
    data.update(overrides)
    return yaml.dump(data)


def _from_yaml(yaml_str: str, monkeypatch, tmp_path) -> BotConfig:
    from kouhai_bot import config
    monkeypatch.setattr(config, "_try_load_dotenv", lambda: None)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml_str, encoding="utf-8")
    monkeypatch.setenv("KOUHAI_CONFIG", str(config_path))
    return BotConfig.from_yaml()


def test_config_parses_user_groups(monkeypatch, tmp_path):
    yaml_str = _make_yaml(
        user_groups=[{
            "name": "starred",
            "display_name": "打星",
            "user_ids": [111, 222],
            "submit_delay_sec": 600,
            "submit_delay_message": "打星用户将在 {wait} 后开放提交～",
        }]
    )
    cfg = _from_yaml(yaml_str, monkeypatch, tmp_path)

    assert len(cfg.user_groups) == 1
    group = cfg.user_groups[0]
    assert group.name == "starred"
    assert group.display_name == "打星"
    assert group.user_ids == [111, 222]
    assert group.submit_delay_sec == 600
    assert group.submit_delay_message == "打星用户将在 {wait} 后开放提交～"


def test_config_rejects_duplicate_group_users(monkeypatch, tmp_path):
    yaml_str = _make_yaml(
        user_groups=[
            {"name": "starred", "user_ids": [111]},
            {"name": "guest", "user_ids": [111]},
        ]
    )
    with pytest.raises(RuntimeError, match="appears in both"):
        _from_yaml(yaml_str, monkeypatch, tmp_path)


def test_config_rejects_reserved_default_group(monkeypatch, tmp_path):
    yaml_str = _make_yaml(
        user_groups=[{"name": "default", "user_ids": [1]}]
    )
    with pytest.raises(RuntimeError, match="reserved"):
        _from_yaml(yaml_str, monkeypatch, tmp_path)


def test_user_group_submit_window(tmp_path, monkeypatch):
    group_id = 999
    state_dir = tmp_path / "groups" / str(group_id)
    state_dir.mkdir(parents=True)
    state_path = state_dir / "state.json"
    state_path.write_text('{"today": "542D", "posted_at": 1000}', encoding="utf-8")

    cfg = BotConfig(
        current_group=group_id,
        data_dir=str(tmp_path),
        user_groups=[
            UserGroupConfig(
                name="starred",
                display_name="打星",
                user_ids=[42],
                submit_delay_sec=300,
                submit_delay_message="打星用户{wait}",
            )
        ],
    )
    monkeypatch.setattr("kouhai_bot.user_groups.get_config", lambda: cfg)
    monkeypatch.setattr("kouhai_bot.handlers.shared.get_config", lambda: cfg)

    assert configured_user_groups()[0].display_name == "打星"
    assert get_user_group(42).name == "starred"
    assert get_user_group(7).name == DEFAULT_GROUP
    assert is_group_submit_blocked(42, group_id, now=1200)
    assert not is_group_submit_blocked(42, group_id, now=1400)
    assert not is_group_submit_blocked(7, group_id, now=1100)
    assert submit_remaining_sec(42, group_id, now=1200) == 100
    assert format_submit_wait(42, group_id, now=1200) == "请等待 2 分钟后再提交"
    assert format_submit_wait(42, group_id, now=1295) == "请等待 5 秒后再提交"
    assert format_group_submit_message(42, group_id, now=1200) == "打星用户请等待 2 分钟后再提交"


def test_get_problem_posted_at_falls_back_to_daily_msg_mtime(tmp_path, monkeypatch):
    group_id = 1002
    state_dir = tmp_path / "groups" / str(group_id)
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text('{"today": "542D"}', encoding="utf-8")
    daily_path = state_dir / "daily_msg.json"
    daily_path.write_text('{"pid": "542D"}', encoding="utf-8")
    os.utime(daily_path, (5000, 5000))

    cfg = BotConfig(current_group=group_id, data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.handlers.shared.get_config", lambda: cfg)

    assert get_problem_posted_at(group_id) == 5000


def test_mark_problem_posted_updates_state(tmp_path, monkeypatch):
    group_id = 1001
    state_dir = tmp_path / "groups" / str(group_id)
    state_dir.mkdir(parents=True)
    state_path = state_dir / "state.json"
    state_path.write_text('{"today": "1A"}', encoding="utf-8")

    cfg = BotConfig(current_group=group_id, data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.handlers.shared.get_config", lambda: cfg)

    mark_problem_posted(group_id, posted_at=4242)

    assert get_problem_posted_at(group_id) == 4242
