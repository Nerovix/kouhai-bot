"""User group membership and per-group submit restrictions."""

from __future__ import annotations

import math
import time

from .config import UserGroupConfig, get_config
from .handlers.shared import get_problem_posted_at

DEFAULT_GROUP = "default"


def default_user_group() -> UserGroupConfig:
    return UserGroupConfig(name=DEFAULT_GROUP, display_name=DEFAULT_GROUP)


def configured_user_groups() -> list[UserGroupConfig]:
    return list(get_config().user_groups or [])


def get_user_group(user_id: int) -> UserGroupConfig:
    target = int(user_id)
    for group in configured_user_groups():
        if target in group.user_ids:
            return group
    return default_user_group()


def is_default_group(group: UserGroupConfig | str) -> bool:
    name = group.name if isinstance(group, UserGroupConfig) else str(group)
    return name == DEFAULT_GROUP


def submit_remaining_sec(
    user_id: int,
    group_id: int,
    *,
    now: float | None = None,
) -> int:
    """Seconds left before this user may /submit (0 if not cooling down)."""
    user_group = get_user_group(user_id)
    if is_default_group(user_group):
        return 0
    posted_at = get_problem_posted_at(group_id)
    if posted_at is None:
        return 0
    window = int(user_group.submit_delay_sec or 0)
    if window <= 0:
        return 0
    current = time.time() if now is None else now
    remaining = window - (current - posted_at)
    if remaining <= 0:
        return 0
    return int(math.ceil(remaining))


def format_submit_wait(
    user_id: int,
    group_id: int,
    *,
    now: float | None = None,
) -> str:
    remaining = submit_remaining_sec(user_id, group_id, now=now)
    if remaining >= 60:
        minutes = (remaining + 59) // 60
        return f"请等待 {minutes} 分钟后再提交"
    return f"请等待 {remaining} 秒后再提交"


def format_group_submit_message(
    user_id: int,
    group_id: int,
    *,
    now: float | None = None,
) -> str:
    user_group = get_user_group(user_id)
    wait = format_submit_wait(user_id, group_id, now=now)
    template = (
        user_group.submit_delay_message
        or f"{user_group.display_name}用户{{wait}}"
    )
    return template.replace("{wait}", wait)


def is_group_submit_blocked(
    user_id: int,
    group_id: int,
    *,
    now: float | None = None,
) -> bool:
    """True when this user's group must not /submit yet after the latest problem post."""
    return submit_remaining_sec(user_id, group_id, now=now) > 0
