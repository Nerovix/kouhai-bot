"""Group echo/repeat handling for group messages."""

from __future__ import annotations

import asyncio
import copy
import logging
import random
import unicodedata
from collections import deque
from dataclasses import dataclass
from typing import Callable

from .config import get_config
from .napcat.client import send_group_msg

logger = logging.getLogger("kouhai-bot.echo")


@dataclass(frozen=True)
class EchoEntry:
    user_id: int
    echo_key: str
    segments: list[dict]
    message_id: int | str
    is_command: bool = False


def canonical_echo_key(segments: list[dict]) -> str:
    """Build a stable repeat-comparison key from echoable message segments."""
    cfg = get_config()
    parts: list[str] = []
    for seg in segments:
        seg_type = seg.get("type", "")
        data = seg.get("data", {})
        if seg_type == "text":
            text = unicodedata.normalize("NFC", str(data.get("text", "")))
            parts.append(f"text:{text}")
        elif seg_type == "face":
            parts.append(f"face:{data.get('id', '')}")
        elif seg_type == "at":
            qq = str(data.get("qq", ""))
            if qq == str(cfg.bot_qq):
                qq = "bot"
            parts.append(f"at:{qq}")
    return "\x1f".join(parts)


def _clone_segments(segments: list[dict]) -> list[dict]:
    return copy.deepcopy(segments)


class GroupEcho:
    """Bounded recent-message buffer for repeat detection."""

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
        group_id: int,
        user_id: int,
        segments: list[dict],
        raw_text: str,
        message_id: int | str,
    ) -> bool:
        """Record a group message and echo it when a repeat streak triggers."""
        cfg = get_config()
        if group_id != cfg.current_group:
            return False
        if not segments:
            return False

        echo_key = canonical_echo_key(segments)
        if not echo_key:
            return False

        echo_segments: list[dict] | None = None
        async with self._lock:
            buf = self._buffer
            stored_segments = _clone_segments(segments)
            buf.append(EchoEntry(
                user_id=user_id,
                echo_key=echo_key,
                segments=stored_segments,
                message_id=message_id,
                is_command=raw_text.startswith("/"),
            ))

            streak_key = buf[-1].echo_key
            streak_len = 0
            streak_users: set[int] = set()
            for entry in reversed(buf):
                if entry.echo_key != streak_key or entry.is_command:
                    break
                streak_len += 1
                streak_users.add(entry.user_id)

            if streak_len < self.trigger_count:
                return False

            has_bot = any(str(uid) == str(cfg.bot_qq) for uid in streak_users)
            if not has_bot and self._rng() < self.echo_probability:
                echo_segments = _clone_segments(stored_segments)
                buf.append(EchoEntry(
                    user_id=int(cfg.bot_qq),
                    echo_key=streak_key,
                    segments=_clone_segments(stored_segments),
                    message_id=f"echo:{message_id}",
                ))

        if echo_segments is None:
            logger.debug("Skipped probabilistic echo for group %s", group_id)
            return False

        logger.info("Echoing repeated group message in group %s", group_id)
        await send_group_msg(group_id, echo_segments)
        return True

    def buffer_snapshot(self) -> list[EchoEntry]:
        """Return a copy of the buffer for tests and diagnostics."""
        return list(self._buffer)


_echo = GroupEcho()


async def check_and_echo(
    *,
    group_id: int,
    user_id: int,
    segments: list[dict],
    raw_text: str,
    message_id: int | str,
) -> bool:
    return await _echo.check_and_echo(
        group_id=group_id,
        user_id=user_id,
        segments=segments,
        raw_text=raw_text,
        message_id=message_id,
    )
