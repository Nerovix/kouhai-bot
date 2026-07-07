"""Group echo/repeat handling for group messages."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass

from .config import get_config
from .napcat.client import build_plain_message, send_group_msg

logger = logging.getLogger("kouhai-bot.echo")


@dataclass(frozen=True)
class EchoEntry:
    user_id: int
    raw_text: str
    message_id: str


class GroupEcho:
    """Bounded recent-message buffer for repeat detection."""

    def __init__(self, *, max_entries: int = 50, trigger_count: int = 3) -> None:
        self.max_entries = max_entries
        self.trigger_count = trigger_count
        self._buffer: deque[EchoEntry] = deque(maxlen=max_entries)
        self._lock = asyncio.Lock()

    async def check_and_echo(
        self,
        *,
        group_id: int,
        user_id: int,
        raw_text: str,
        message_id: str,
    ) -> bool:
        """Record a group message and echo it when a repeat streak triggers."""
        cfg = get_config()
        if group_id != cfg.current_group:
            return False
        if not raw_text:
            return False

        echo_text: str | None = None
        async with self._lock:
            buf = self._buffer
            buf.append(EchoEntry(user_id=user_id, raw_text=raw_text, message_id=message_id))

            streak_text = buf[-1].raw_text
            streak_len = 0
            streak_users: set[int] = set()
            for entry in reversed(buf):
                if entry.raw_text != streak_text or entry.raw_text.startswith("/"):
                    break
                streak_len += 1
                streak_users.add(entry.user_id)

            if streak_len < self.trigger_count:
                return False

            has_bot = any(str(uid) == str(cfg.bot_qq) for uid in streak_users)
            if not has_bot:
                echo_text = streak_text

            for _ in range(streak_len):
                buf.pop()

        if echo_text is None:
            logger.debug("Skipped echo for group %s because bot was in streak", group_id)
            return False

        logger.info("Echoing repeated group message in group %s", group_id)
        await send_group_msg(group_id, build_plain_message(echo_text))
        return True

    def buffer_snapshot(self) -> list[EchoEntry]:
        """Return a copy of the buffer for tests and diagnostics."""
        return list(self._buffer)


_echo = GroupEcho()


async def check_and_echo(
    *,
    group_id: int,
    user_id: int,
    raw_text: str,
    message_id: str,
) -> bool:
    return await _echo.check_and_echo(
        group_id=group_id,
        user_id=user_id,
        raw_text=raw_text,
        message_id=message_id,
    )
