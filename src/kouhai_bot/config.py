"""Global configuration — reads from config.yaml at startup."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .llm_config import LlmProviderConfig, build_provider_queues_from_yaml


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _try_load_dotenv() -> None:
    """Best-effort local .env loading hook, kept patchable for tests."""
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    load_dotenv(_repo_root() / ".env", override=False)


def _find_config_yaml() -> Path:
    """Find config.yaml: KOUHAI_CONFIG env > repo root."""
    env_path = os.environ.get("KOUHAI_CONFIG", "").strip()
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p
        raise RuntimeError(f"KOUHAI_CONFIG is set but file not found: {env_path}")

    repo_path = _repo_root() / "config.yaml"
    if repo_path.exists():
        return repo_path

    raise RuntimeError(
        "Cannot find config.yaml. Place it at the repo root, "
        "or set KOUHAI_CONFIG=/path/to/config.yaml"
    )


@dataclass
class UserGroupConfig:
    name: str
    display_name: str
    user_ids: list[int] = field(default_factory=list)
    submit_delay_sec: int = 0
    submit_delay_message: str = ""


def _parse_user_groups(groups_data: list[dict]) -> list[UserGroupConfig]:
    groups: list[UserGroupConfig] = []
    seen_names: set[str] = set()
    seen_users: dict[int, str] = {}

    for g in groups_data:
        if not isinstance(g, dict):
            raise RuntimeError(
                f"config.yaml: each entry in user_groups must be a mapping, "
                f"got {type(g).__name__}: {g!r}"
            )
        name = str(g.get("name", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise RuntimeError(f"Invalid user group name: {name}")
        normalized = name.lower()
        if normalized == "default":
            raise RuntimeError("Cannot define reserved group name 'default'")
        if normalized in seen_names:
            raise RuntimeError(f"Duplicate user group name: {name}")
        seen_names.add(normalized)

        user_ids = [int(uid) for uid in g.get("user_ids", [])]
        for uid in user_ids:
            previous = seen_users.get(uid)
            if previous is not None:
                raise RuntimeError(
                    f"User {uid} appears in both groups {previous} and {name}"
                )
            seen_users[uid] = name

        groups.append(
            UserGroupConfig(
                name=name,
                display_name=str(g.get("display_name", name)),
                user_ids=user_ids,
                submit_delay_sec=int(g.get("submit_delay_sec", 0)),
                submit_delay_message=str(g.get("submit_delay_message", "")),
            )
        )
    return groups


@dataclass
class BotConfig:
    """Singleton config loaded from config.yaml at startup."""

    # --- QQ ---
    bot_qq: int = 0
    napcat_ws_host: str = "0.0.0.0"
    napcat_ws_port: int = 8095
    napcat_http_host: str = "127.0.0.1"
    napcat_http_port: int = 3000

    # --- LLM fallback ---
    llm_smart_providers: list[LlmProviderConfig] = field(default_factory=list)
    llm_general_providers: list[LlmProviderConfig] = field(default_factory=list)
    llm_max_retries: int = 2
    llm_retry_base_delay_sec: float = 1.0
    llm_retry_max_delay_sec: float = 8.0
    judge_timeout_sec: int = 1200
    clarify_timeout_sec: int = 600
    review_timeout_sec: int = 600
    summary_timeout_sec: int = 120

    # --- Qwen-VL ---
    qwen_api_key: str = ""
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = ""

    # --- Group ---
    current_group: int = 0

    # --- Problems ---
    min_rating: int = 2000
    max_rating: int = 3000
    newproblem_cooldown: int = 300
    submit_ac_backdoor: str = ""
    daily_post_cron: str = "0 12 * * *"

    # --- User groups ---
    user_groups: list[UserGroupConfig] = field(default_factory=list)

    # --- Curfew ---
    curfew_start_hour: int = 0
    curfew_duration_hours: int = 0

    # --- Paths ---
    data_dir: str = str(Path.home() / ".kouhai-bot")
    sessions_dir: str = ""

    def __post_init__(self) -> None:
        if not self.sessions_dir:
            self.sessions_dir = os.path.join(self.data_dir, "sessions")

    @classmethod
    def from_yaml(cls) -> BotConfig:
        """Load config from config.yaml."""
        _try_load_dotenv()
        config_path = _find_config_yaml()
        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise RuntimeError("config.yaml must be a YAML mapping at the top level")

        c = cls()

        # --- QQ ---
        c.bot_qq = int(data.get("bot_qq", c.bot_qq))
        if c.bot_qq <= 0:
            raise RuntimeError("config.yaml: bot_qq is required")
        c.napcat_ws_host = str(data.get("napcat_ws_host", c.napcat_ws_host))
        c.napcat_ws_port = int(data.get("napcat_ws_port", c.napcat_ws_port))
        c.napcat_http_host = str(data.get("napcat_http_host", c.napcat_http_host))
        c.napcat_http_port = int(data.get("napcat_http_port", c.napcat_http_port))

        # --- LLM ---
        llm = data.get("llm", {})
        if not isinstance(llm, dict):
            raise RuntimeError("config.yaml: 'llm' must be a mapping")
        (
            c.llm_smart_providers,
            c.llm_general_providers,
        ) = build_provider_queues_from_yaml(llm)
        c.llm_max_retries = int(llm.get("max_retries", c.llm_max_retries))
        c.llm_retry_base_delay_sec = float(
            llm.get("retry_base_delay_sec", c.llm_retry_base_delay_sec)
        )
        c.llm_retry_max_delay_sec = float(
            llm.get("retry_max_delay_sec", c.llm_retry_max_delay_sec)
        )
        c.judge_timeout_sec = int(llm.get("judge_timeout_sec", c.judge_timeout_sec))
        c.clarify_timeout_sec = int(
            llm.get("clarify_timeout_sec", c.clarify_timeout_sec)
        )
        c.review_timeout_sec = int(
            llm.get("review_timeout_sec", c.review_timeout_sec)
        )
        c.summary_timeout_sec = int(
            llm.get("summary_timeout_sec", c.summary_timeout_sec)
        )

        # --- Qwen-VL ---
        qwen = data.get("qwen", {})
        if not isinstance(qwen, dict):
            qwen = {}
        c.qwen_api_key = str(qwen.get("api_key", c.qwen_api_key))
        c.qwen_base_url = str(qwen.get("base_url", c.qwen_base_url))
        c.qwen_model = str(qwen.get("model", ""))
        if not c.qwen_model:
            raise RuntimeError("config.yaml: qwen.model is required")

        # --- Group ---
        if "current_group" not in data:
            raise RuntimeError("config.yaml: current_group is required")
        c.current_group = int(data["current_group"])

        # --- Problems ---
        problem = data.get("problem", {})
        if isinstance(problem, dict):
            c.min_rating = int(problem.get("min_rating", c.min_rating))
            c.max_rating = int(problem.get("max_rating", c.max_rating))
            c.newproblem_cooldown = int(
                problem.get("newproblem_cooldown", c.newproblem_cooldown)
            )
            c.submit_ac_backdoor = str(
                problem.get("submit_ac_backdoor", c.submit_ac_backdoor)
            ).strip()
            c.daily_post_cron = str(
                problem.get("daily_post_cron", c.daily_post_cron)
            )
        if "submit_ac_backdoor" in data:
            c.submit_ac_backdoor = str(data.get("submit_ac_backdoor", "")).strip()

        # --- User groups ---
        ug_list = data.get("user_groups", [])
        if isinstance(ug_list, list):
            c.user_groups = _parse_user_groups(ug_list)

        # --- Curfew ---
        curfew = data.get("curfew", {})
        if isinstance(curfew, dict):
            c.curfew_start_hour = int(
                curfew.get("start_hour", c.curfew_start_hour)
            )
            c.curfew_duration_hours = int(
                curfew.get("duration_hours", c.curfew_duration_hours)
            )

        # --- Paths ---
        data_dir = data.get("data_dir", "")
        if data_dir:
            c.data_dir = os.path.expanduser(str(data_dir))

        # Reset derived paths so __post_init__ recomputes them from the
        # (possibly updated) data_dir.
        c.sessions_dir = ""
        c.__post_init__()
        return c


_config: BotConfig | None = None


def get_config() -> BotConfig:
    global _config
    if _config is None:
        _config = BotConfig.from_yaml()
    return _config
