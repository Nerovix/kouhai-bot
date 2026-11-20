"""Shared pytest configuration for the kouhai-bot test-suite.

Network isolation: the suite must never talk to a real official QQ API (or
any other live HTTP endpoint). Every outbound REST/token/LLM call in the new
architecture funnels through ``aiohttp.ClientSession._request``, so blocking
it here makes unmocked calls fail loudly (``aiohttp.ClientError``) instead of
reaching production. Tests that exercise the transport inject a fake session
or monkeypatch the call themselves, which overrides this autouse block.
"""

import warnings

import aiohttp
import pytest


@pytest.fixture
async def db(tmp_path):
    from kouhai_bot.db import Database

    database = Database(tmp_path / "test.db")
    await database.open()
    yield database
    await database.close()


@pytest.fixture(autouse=True)
def _block_real_http(monkeypatch):
    async def _blocked(self, method, url, *args, **kwargs):
        warnings.warn(
            f"tests attempted a real HTTP call ({method} {url}); blocked by conftest",
            stacklevel=2,
        )
        raise aiohttp.ClientError(f"real HTTP call blocked in tests: {method} {url}")

    monkeypatch.setattr(aiohttp.ClientSession, "_request", _blocked)


@pytest.fixture(autouse=True)
def _reset_global_runtime():
    """Clear process-global runtime/database/cooldown state between tests."""
    yield
    from kouhai_bot import legacy_migration
    from kouhai_bot.db import set_database
    from kouhai_bot.handlers import (
        reset_cooldowns_for_tests,
        reset_message_dedup_for_tests,
    )
    from kouhai_bot.handlers.runtime import set_runtime

    set_runtime(None)
    set_database(None)
    legacy_migration.reset_for_tests()
    reset_cooldowns_for_tests()
    reset_message_dedup_for_tests()