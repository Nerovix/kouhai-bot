"""Handler context — the single argument every official-native command receives.

Bundles the group/user identity (str openid), the triggering message id (for
passive replies), the parsed command args, and access to the process runtime
(Database + OfficialSender). Replaces the legacy 7-arg OneBot handler signature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..db.database import Database
from ..official.sender import OfficialSender
from ..render.browser import RenderBrowser
from .runtime import get_runtime


def display_name(user_openid: str, nickname: str = "") -> str:
    """Best display name. Official group events carry member_nickname/username
    (since 2026-11-16); when absent, fall back to a short openid code — never a
    generic placeholder."""
    nick = (nickname or "").strip()
    if nick:
        return nick
    return (user_openid or "?")[:8]


@dataclass
class Ctx:
    group_openid: str
    user_openid: str
    message_id: str
    raw_text: str = ""
    args: str = ""
    nickname: str = ""
    event: dict[str, Any] = field(default_factory=dict)

    @property
    def db(self) -> Database:
        return get_runtime().db

    @property
    def sender(self) -> OfficialSender:
        return get_runtime().sender

    @property
    def browser(self) -> RenderBrowser | None:
        return get_runtime().browser

    @property
    def name(self) -> str:
        return display_name(self.user_openid, self.nickname)