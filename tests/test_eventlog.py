"""Tests for command dispatch behavior."""

import asyncio
import os
import shutil
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.handlers import dispatch, process_event
from kouhai_bot.handlers.registry import CommandDef, register


GID = 123456
UID = 42
BOT_QQ = 1234567890


class _TestConfig:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.bot_qq = BOT_QQ
        self.current_group = GID


def _event(text: str = "/dispatchtest hello") -> dict:
    return {
        "type": "message",
        "message_type": "group",
        "group_id": GID,
        "user_id": UID,
        "sender": {"nickname": "Alice", "card": "", "user_id": UID},
        "message_id": "msg_001",
        "raw_message": text,
        "message": [{"type": "text", "data": {"text": text}}],
    }


def _temp_data_dir():
    root = tempfile.mkdtemp(prefix="xcpc_dispatch_")
    data_dir = os.path.join(root, "data")
    os.makedirs(data_dir, exist_ok=True)
    return root, data_dir


def test_dispatch_runs_registered_handler():
    root, data_dir = _temp_data_dir()
    seen: list[str] = []

    async def _handler(**kwargs):
        seen.append(kwargs["raw_text"])

    try:
        register(CommandDef(
            name="dispatchtest",
            aliases=[],
            description="dispatch test command",
            usage="",
            handler=_handler,
        ))

        async def _run():
            with patch("kouhai_bot.config._config", _TestConfig(data_dir)):
                await dispatch(_event())
                await asyncio.sleep(0.05)

        asyncio.run(_run())
        assert seen == ["/dispatchtest hello"]
    finally:
        shutil.rmtree(root)


def test_process_event_can_wait_for_handler_completion():
    root, data_dir = _temp_data_dir()
    seen: list[str] = []

    async def _handler(**kwargs):
        await asyncio.sleep(0)
        seen.append(kwargs["raw_text"])

    try:
        register(CommandDef(
            name="dispatchsync",
            aliases=[],
            description="dispatch sync test command",
            usage="",
            handler=_handler,
        ))

        async def _run():
            with patch("kouhai_bot.config._config", _TestConfig(data_dir)):
                await process_event(_event("/dispatchsync hello"), spawn_handlers=False)

        asyncio.run(_run())
        assert seen == ["/dispatchsync hello"]
    finally:
        shutil.rmtree(root)


def test_process_event_canonicalizes_alias_for_handler():
    root, data_dir = _temp_data_dir()
    seen: list[str] = []

    async def _handler(**kwargs):
        seen.append(kwargs["raw_text"])

    try:
        register(CommandDef(
            name="dispatchalias",
            aliases=["da"],
            description="dispatch alias test command",
            usage="",
            handler=_handler,
        ))

        async def _run():
            with patch("kouhai_bot.config._config", _TestConfig(data_dir)):
                await process_event(_event("/da hello"), spawn_handlers=False)

        asyncio.run(_run())
        assert seen == ["/dispatchalias hello"]
    finally:
        shutil.rmtree(root)


def test_process_event_canonicalizes_command_case_for_handler():
    root, data_dir = _temp_data_dir()
    seen: list[str] = []

    async def _handler(**kwargs):
        seen.append(kwargs["raw_text"])

    try:
        register(CommandDef(
            name="dispatchcase",
            aliases=[],
            description="dispatch case test command",
            usage="",
            handler=_handler,
        ))

        async def _run():
            with patch("kouhai_bot.config._config", _TestConfig(data_dir)):
                await process_event(_event("/DISPATCHCASE hello"), spawn_handlers=False)

        asyncio.run(_run())
        assert seen == ["/dispatchcase hello"]
    finally:
        shutil.rmtree(root)


def test_process_event_accepts_leading_variation_selector_before_command():
    root, data_dir = _temp_data_dir()
    seen: list[str] = []

    async def _handler(**kwargs):
        seen.append(kwargs["raw_text"])

    try:
        register(CommandDef(
            name="dispatchvs",
            aliases=[],
            description="dispatch vs test command",
            usage="",
            handler=_handler,
        ))

        async def _run():
            with patch("kouhai_bot.config._config", _TestConfig(data_dir)):
                await process_event(_event("\ufe0f/dispatchvs hello"), spawn_handlers=False)

        asyncio.run(_run())
        assert seen == ["/dispatchvs hello"]
    finally:
        shutil.rmtree(root)
