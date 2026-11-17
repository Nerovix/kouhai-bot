"""Group echo/repeat handling for official group messages (multi-group).

Each group has an independent bounded recent-message buffer. When the same
plain text repeats ``trigger_count`` times in a row (commands excluded), the
bot probabilistically repeats it once. Requires the group to deliver full
messages (GROUP_MESSAGE_CREATE). Official content is plain text — no OneBot
segments — so the repeat key is the NFC-normalized text.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import unicodedata
from collections import deque
from dataclasses import dataclass
from typing import Callable

from .handlers.runtime import get_runtime

logger = logging.getLogger("kouhai-bot.echo")

_BOT_OPENID = "__bot__"
_LEADING_AT_RE = re.compile(r"^(?:<@![0-9A-Fa-f]+>\s*)+")


@dataclass(frozen=True)
class EchoEntry:
    user_openid: str
    echo_key: str
    content: str
    message_id: str
    is_command: bool = False


def canonical_echo_key(content: str) -> str:
    text = _LEADING_AT_RE.sub("", content or "")
    return unicodedata.normalize("NFC", text.strip())


class GroupEcho:
    """Bounded recent-message buffer for repeat detection in one group."""

    def __init__(
        self,
        *,
        max_entries: int = 50,
        trigger_count: int = 2,
        echo_probability: float = 0.25,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.max_entries = max_entries
        self.trigger_count = trigger_count
        self.echo_probability = echo_probability
        self._rng = rng
        self._buffer: deque[EchoEntry] = deque(maxlen=max_entries)
        self._lock = asyncio.Lock()

    async def check_and_echo(
        self,
        *,
        group_openid: str,
        user_openid: str,
        content: str,
        raw_text: str,
        message_id: str,
    ) -> bool:
        echo_key = canonical_echo_key(content)
        if not echo_key:
            return False

        echo_content: str | None = None
        async with self._lock:
            buf = self._buffer
            buf.append(EchoEntry(
                user_openid=user_openid,
                echo_key=echo_key,
                content=content,
                message_id=message_id,
                is_command=raw_text.startswith("/"),
            ))

            streak_key = buf[-1].echo_key
            streak_len = 0
            streak_users: set[str] = set()
            for entry in reversed(buf):
                if entry.echo_key != streak_key or entry.is_command:
                    break
                streak_len += 1
                streak_users.add(entry.user_openid)

            if streak_len < self.trigger_count:
                return False

            has_bot = _BOT_OPENID in streak_users
            if not has_bot and self._rng() < self.echo_probability:
                echo_content = content
                buf.append(EchoEntry(
                    user_openid=_BOT_OPENID,
                    echo_key=streak_key,
                    content=content,
                    message_id=f"echo:{message_id}",
                ))

        if echo_content is None:
            return False

        logger.info("Echoing repeated group message in group %s", group_openid)
        await get_runtime().sender.send_group_text(group_openid, echo_content)
        return True

    def buffer_snapshot(self) -> list[EchoEntry]:
        return list(self._buffer)


_groups: dict[str, GroupEcho] = {}


def _group_echo(group_openid: str) -> GroupEcho:
    echo = _groups.get(group_openid)
    if echo is None:
        echo = GroupEcho()
        _groups[group_openid] = echo
    return echo


async def check_and_echo(
    *,
    group_openid: str,
    user_openid: str,
    content: str,
    raw_text: str,
    message_id: str,
) -> bool:
    return await _group_echo(group_openid).check_and_echo(
        group_openid=group_openid,
        user_openid=user_openid,
        content=content,
        raw_text=raw_text,
        message_id=message_id,
    )


def reset_echo_for_tests() -> None:
    _groups.clear()