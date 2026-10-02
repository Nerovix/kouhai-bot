"""Shared pytest configuration for the kouhai-bot test-suite.

Network isolation: the suite must never talk to a real NapCat instance
(the production bot runs on this machine, so an unmocked call would reach
it). Every message send/query in ``kouhai_bot.napcat.client`` goes through
``_http_post``, so blocking it here makes unmocked calls degrade exactly
like an unreachable NapCat (``{"status": "failed"}`` → the callers' normal
None/failure paths) instead of touching production. Every blocked call
emits a warning so the run summary shows it if a test forgot its mock.
"""

import warnings

import pytest


@pytest.fixture(autouse=True)
def _block_real_napcat_http(monkeypatch):
    async def _blocked(action, data):
        warnings.warn(
            f"tests attempted a real NapCat call ({action}); blocked by conftest",
            stacklevel=2,
        )
        return {"status": "failed", "data": {}}

    monkeypatch.setattr("kouhai_bot.napcat.client._http_post", _blocked)
