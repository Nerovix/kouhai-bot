"""User group membership and per-group submit restrictions."""

from __future__ import annotations

import math
import time
from typing import Any

from .config import UserGroupConfig, get_config
from .handlers.shared import get_problem_posted_at

DEFAULT_GROUP = "default"
WAIT_STATE_KEY = "user_group_waits"


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


def is_dynamic_submit_delay_enabled(group: UserGroupConfig | str) -> bool:
    user_group = get_user_group_config(group) if isinstance(group, str) else group
    if user_group is None or is_default_group(user_group):
        return False
    return int(user_group.submit_delay_sec or 0) > 0


def get_user_group_config(group: UserGroupConfig | str) -> UserGroupConfig | None:
    name = group.name if isinstance(group, UserGroupConfig) else str(group)
    if name == DEFAULT_GROUP:
        return default_user_group()
    for configured in configured_user_groups():
        if configured.name == name:
            return configured
    return None


def _wait_state(sb: dict[str, Any]) -> dict[str, Any]:
    state = sb.get(WAIT_STATE_KEY)
    if not isinstance(state, dict):
        state = {}
        sb[WAIT_STATE_KEY] = state
    groups = state.get("groups")
    if not isinstance(groups, dict):
        groups = {}
        state["groups"] = groups
    settled = state.get("settled_problems")
    if not isinstance(settled, dict):
        settled = {}
        state["settled_problems"] = settled
    return state


def _group_wait_state(sb: dict[str, Any], group_name: str) -> dict[str, Any]:
    state = _wait_state(sb)
    groups = state["groups"]
    group_state = groups.get(group_name)
    if not isinstance(group_state, dict):
        group_state = {}
        groups[group_name] = group_state
    users = group_state.get("users")
    if not isinstance(users, dict):
        users = {}
        group_state["users"] = users
    return group_state


def effective_submit_delay_sec_for_scoreboard(
    user_id: int,
    sb: dict[str, Any],
) -> int:
    """Current post-new-problem wait window for this user."""
    user_group = get_user_group(user_id)
    if not is_dynamic_submit_delay_enabled(user_group):
        return 0

    floor = int(user_group.submit_delay_sec or 0)
    state = sb.get(WAIT_STATE_KEY)
    groups = state.get("groups") if isinstance(state, dict) else {}
    group_state = groups.get(user_group.name) if isinstance(groups, dict) else {}
    users = group_state.get("users") if isinstance(group_state, dict) else {}
    if not isinstance(users, dict):
        users = {}
    user_state = users.get(str(user_id))
    if not isinstance(user_state, dict):
        return floor
    try:
        saved = int(user_state.get("wait_sec", floor))
    except (TypeError, ValueError):
        saved = floor
    return max(floor, saved)


def effective_submit_delay_sec(user_id: int, group_id: int) -> int:
    from .handlers.shared import load_scoreboard

    user_group = get_user_group(user_id)
    if not is_dynamic_submit_delay_enabled(user_group):
        return 0
    return effective_submit_delay_sec_for_scoreboard(
        user_id,
        load_scoreboard(group_id),
    )


def _first_solver_for_problem(sb: dict[str, Any], problem_id: str) -> int | None:
    matches = [
        solve for solve in sb.get("solves", [])
        if str(solve.get("problem", "") or "") == problem_id
    ]
    if not matches:
        return None

    def _order(item: dict[str, Any]) -> int:
        try:
            return int(item.get("order", 1 << 30))
        except (TypeError, ValueError):
            return 1 << 30

    first = sorted(matches, key=_order)[0]
    try:
        return int(first.get("user_id"))
    except (TypeError, ValueError):
        return None


def settle_dynamic_submit_wait_for_problem(
    sb: dict[str, Any],
    problem_id: str,
) -> bool:
    """Apply one dynamic wait update for a solved problem.

    Returns True when scoreboard data changed. The caller owns persistence.
    """
    pid = str(problem_id or "")
    if not pid:
        return False
    state = _wait_state(sb)
    settled = state["settled_problems"]
    if settled.get(pid):
        return False

    solver = _first_solver_for_problem(sb, pid)
    if solver is None:
        return False

    for user_group in configured_user_groups():
        if not is_dynamic_submit_delay_enabled(user_group):
            continue
        group_state = _group_wait_state(sb, user_group.name)
        users = group_state["users"]
        floor = int(user_group.submit_delay_sec or 0)
        for uid in user_group.user_ids:
            uid_int = int(uid)
            uid_text = str(uid_int)
            current = effective_submit_delay_sec_for_scoreboard(uid_int, sb)
            if uid_int == solver:
                next_wait = current * 2
            else:
                next_wait = max(floor, int(math.ceil(current / 2)))
            previous = users.get(uid_text)
            if not isinstance(previous, dict):
                previous = {}
                users[uid_text] = previous
            previous["wait_sec"] = next_wait

    settled[pid] = int(time.time())
    return True


def submit_remaining_sec(
    user_id: int,
    group_id: int,
    *,
    now: float | None = None,
) -> int:
    """Seconds left before this user may /submit (0 if not cooling down)."""
    window = effective_submit_delay_sec(user_id, group_id)
    if window <= 0:
        return 0
    posted_at = get_problem_posted_at(group_id)
    if posted_at is None:
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
