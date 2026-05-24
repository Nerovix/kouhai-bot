"""Tests for structured command event logs."""

import asyncio
import os
import shutil
import sys
import tempfile
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.eventlog import (
    EVENT_META_KEY,
    TZ,
    load_events,
    log_command_finished,
    log_command_received,
)
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


def _event(text: str = "/elogtest hello") -> dict:
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
    root = tempfile.mkdtemp(prefix="xcpc_eventlog_")
    data_dir = os.path.join(root, "data")
    os.makedirs(data_dir, exist_ok=True)
    return root, data_dir


def test_eventlog_uses_real_date_without_logical_day():
    root, data_dir = _temp_data_dir()
    try:
        fixed = datetime(2026, 5, 15, 3, 59, 12, tzinfo=TZ)
        with patch("kouhai_bot.config._config", _TestConfig(data_dir)), \
                patch("kouhai_bot.eventlog.now_tz", return_value=fixed):
            meta = log_command_received(
                group_id=GID,
                user_id=UID,
                sender={"nickname": "Alice", "card": "", "user_id": UID},
                command="submit",
                message_id="msg_001",
                raw_text="/submit solution",
            )

        with patch("kouhai_bot.config._config", _TestConfig(data_dir)):
            events = load_events(GID, "2026-05-15")
            assert meta["date"] == "2026-05-15"
            assert len(events) == 1
            assert events[0]["timestamp"].startswith("2026-05-15T03:59:12")
            assert events[0]["date"] == "2026-05-15"
            assert "logical_day" not in events[0]
    finally:
        shutil.rmtree(root)


def test_eventlog_finished_links_to_received_request():
    root, data_dir = _temp_data_dir()
    try:
        with patch("kouhai_bot.config._config", _TestConfig(data_dir)):
            meta = log_command_received(
                group_id=GID,
                user_id=UID,
                sender={"nickname": "Alice", "card": "", "user_id": UID},
                command="submit",
                message_id="msg_001",
                raw_text="/submit solution",
            )
            log_command_finished(meta, status="correct", problem="542D")

        with patch("kouhai_bot.config._config", _TestConfig(data_dir)):
            events = load_events(GID, meta["date"])
            assert [item["type"] for item in events] == ["received", "finished"]
            assert events[1]["request_id"] == events[0]["request_id"]
            assert events[1]["status"] == "correct"
            assert events[1]["problem"] == "542D"
            assert meta["finished_logged"] is True
    finally:
        shutil.rmtree(root)


def test_dispatch_writes_received_and_finished_events():
    root, data_dir = _temp_data_dir()
    seen: list[dict] = []

    async def _handler(**kwargs):
        seen.append(kwargs["event"].get(EVENT_META_KEY, {}))

    try:
        register(CommandDef(
            name="elogtest",
            aliases=[],
            description="event log test command",
            usage="",
            handler=_handler,
        ))

        async def _run():
            with patch("kouhai_bot.config._config", _TestConfig(data_dir)):
                await dispatch(_event())
                await asyncio.sleep(0.05)

        asyncio.run(_run())

        assert seen and seen[0].get("request_id")
        with patch("kouhai_bot.config._config", _TestConfig(data_dir)):
            events = load_events(GID, seen[0]["date"])
            assert [item["type"] for item in events] == ["received", "finished"]
            assert events[0]["command"] == "elogtest"
            assert events[1]["status"] == "ok"
            assert events[1]["request_id"] == events[0]["request_id"]
    finally:
        shutil.rmtree(root)


def test_process_event_can_wait_for_handler_completion():
    root, data_dir = _temp_data_dir()
    seen: list[dict] = []

    async def _handler(**kwargs):
        await asyncio.sleep(0)
        seen.append(kwargs["event"].get(EVENT_META_KEY, {}))

    try:
        register(CommandDef(
            name="elogsync",
            aliases=[],
            description="event log sync test command",
            usage="",
            handler=_handler,
        ))

        async def _run():
            with patch("kouhai_bot.config._config", _TestConfig(data_dir)):
                await process_event(_event("/elogsync hello"), spawn_handlers=False)

        asyncio.run(_run())

        assert seen and seen[0].get("request_id")
        with patch("kouhai_bot.config._config", _TestConfig(data_dir)):
            events = load_events(GID, seen[0]["date"])
            assert [item["type"] for item in events] == ["received", "finished"]
            assert events[0]["command"] == "elogsync"
            assert events[1]["status"] == "ok"
    finally:
        shutil.rmtree(root)


def test_process_event_canonicalizes_alias_for_handler_and_eventlog():
    root, data_dir = _temp_data_dir()
    seen: list[dict | str] = []

    async def _handler(**kwargs):
        seen.append(kwargs["raw_text"])
        seen.append(kwargs["event"].get(EVENT_META_KEY, {}))

    try:
        register(CommandDef(
            name="elogalias",
            aliases=["ea"],
            description="event log alias test command",
            usage="",
            handler=_handler,
        ))

        async def _run():
            with patch("kouhai_bot.config._config", _TestConfig(data_dir)):
                await process_event(_event("/ea hello"), spawn_handlers=False)

        asyncio.run(_run())

        assert seen
        assert seen[0] == "/elogalias hello"
        with patch("kouhai_bot.config._config", _TestConfig(data_dir)):
            events = load_events(GID, seen[1]["date"])
            assert [item["type"] for item in events] == ["received", "finished"]
            assert events[0]["command"] == "elogalias"
            assert events[0]["raw_text_preview"] == "/ea hello"
            assert events[1]["command"] == "elogalias"
            assert events[1]["status"] == "ok"
    finally:
        shutil.rmtree(root)


def test_process_event_canonicalizes_command_case_for_handler():
    root, data_dir = _temp_data_dir()
    seen: list[dict | str] = []

    async def _handler(**kwargs):
        seen.append(kwargs["raw_text"])
        seen.append(kwargs["event"].get(EVENT_META_KEY, {}))

    try:
        register(CommandDef(
            name="elogcase",
            aliases=[],
            description="event log case test command",
            usage="",
            handler=_handler,
        ))

        async def _run():
            with patch("kouhai_bot.config._config", _TestConfig(data_dir)):
                await process_event(_event("/ELOGCASE hello"), spawn_handlers=False)

        asyncio.run(_run())

        assert seen
        assert seen[0] == "/elogcase hello"
        with patch("kouhai_bot.config._config", _TestConfig(data_dir)):
            events = load_events(GID, seen[1]["date"])
            assert [item["type"] for item in events] == ["received", "finished"]
            assert events[0]["command"] == "elogcase"
            assert events[0]["raw_text_preview"] == "/ELOGCASE hello"
            assert events[1]["status"] == "ok"
    finally:
        shutil.rmtree(root)


def test_process_event_accepts_leading_variation_selector_before_command():
    root, data_dir = _temp_data_dir()
    seen: list[dict] = []

    async def _handler(**kwargs):
        seen.append(kwargs["raw_text"])
        seen.append(kwargs["event"].get(EVENT_META_KEY, {}))

    try:
        register(CommandDef(
            name="elogvs",
            aliases=[],
            description="event log vs test command",
            usage="",
            handler=_handler,
        ))

        async def _run():
            with patch("kouhai_bot.config._config", _TestConfig(data_dir)):
                await process_event(_event("\ufe0f/elogvs hello"), spawn_handlers=False)

        asyncio.run(_run())

        assert seen
        assert seen[0] == "/elogvs hello"
        with patch("kouhai_bot.config._config", _TestConfig(data_dir)):
            events = load_events(GID, seen[1]["date"])
            assert [item["type"] for item in events] == ["received", "finished"]
            assert events[0]["command"] == "elogvs"
            assert events[1]["status"] == "ok"
    finally:
        shutil.rmtree(root)
