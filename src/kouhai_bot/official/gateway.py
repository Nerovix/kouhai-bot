"""Official QQ bot outbound WebSocket gateway client.

Flow: GET /gateway → connect → op10 hello → op2 identify (or op6 resume)
→ op1 heartbeat / op11 ack → op0 dispatch. op7 asks for reconnect (resume);
op9 invalidates the session (fresh identify). Events are normalized and
handed to ``on_event``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from contextlib import suppress
from typing import Any, Awaitable, Callable

import websockets

from .events import normalize_event
from .rest import OfficialRestClient
from .tokens import AccessTokenManager

logger = logging.getLogger("kouhai-bot.official.gateway")

INTENT_GROUP_AND_C2C = 1 << 25
INTENT_INTERACTION = 1 << 26
INTENT_GROUP_MEMBER = 1 << 24
DEFAULT_INTENTS = INTENT_GROUP_AND_C2C | INTENT_INTERACTION | INTENT_GROUP_MEMBER

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class OfficialGateway:
    def __init__(
        self,
        rest: OfficialRestClient,
        tokens: AccessTokenManager,
        *,
        on_event: EventHandler,
        on_ready: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        intents: int = DEFAULT_INTENTS,
        shard: tuple[int, int] = (0, 1),
    ) -> None:
        self._rest = rest
        self._tokens = tokens
        self._on_event = on_event
        self._on_ready = on_ready
        self._intents = intents
        self._shard = list(shard)
        self._session_id: str | None = None
        self._seq: int | None = None
        self._last_ack = 0.0

    async def run(self, stop_event: asyncio.Event) -> None:
        delay = 1.0
        while not stop_event.is_set():
            try:
                await self._session(stop_event)
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("gateway session ended: %s", e)
            if stop_event.is_set():
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)

    async def _identify_payload(self) -> dict[str, Any]:
        token = await self._tokens.get_token()
        return {
            "op": 2,
            "d": {
                "token": f"QQBot {token}",
                "intents": self._intents,
                "shard": self._shard,
                "properties": {"os": "linux", "browser": "kouhai-bot", "device": "kouhai-bot"},
            },
        }

    async def _session(self, stop_event: asyncio.Event) -> None:
        url = await self._rest.get_gateway()
        async with websockets.connect(url, max_size=2**26, ping_interval=None) as ws:
            hello = json.loads(await ws.recv())
            if hello.get("op") != 10:
                raise RuntimeError(f"expected op10 hello, got {hello}")
            interval = float(hello["d"]["heartbeat_interval"]) / 1000.0
            hb = asyncio.create_task(self._heartbeat(ws, interval, stop_event))
            try:
                if self._session_id and self._seq is not None:
                    token = await self._tokens.get_token()
                    await ws.send(json.dumps({
                        "op": 6,
                        "d": {
                            "token": f"QQBot {token}",
                            "session_id": self._session_id,
                            "seq": self._seq,
                        },
                    }))
                    logger.info("gateway resuming session %s at seq %s", self._session_id, self._seq)
                else:
                    await ws.send(json.dumps(await self._identify_payload()))
                    logger.info("gateway identifying (intents=%s)", self._intents)
                await self._recv_loop(ws, stop_event)
            finally:
                hb.cancel()
                with suppress(asyncio.CancelledError):
                    await hb

    async def _recv_loop(self, ws: Any, stop_event: asyncio.Event) -> None:
        async for raw in ws:
            if stop_event.is_set():
                break
            msg = json.loads(raw)
            op = msg.get("op")
            if op == 0:
                self._seq = msg.get("s", self._seq)
                t = msg.get("t", "")
                d = msg.get("d") or {}
                if t == "READY":
                    self._session_id = d.get("session_id")
                    logger.info("gateway READY (session=%s)", self._session_id)
                    if self._on_ready is not None:
                        await self._on_ready(d.get("user") or {})
                    continue
                event = normalize_event(t, d)
                if event is not None:
                    await self._on_event(event)
            elif op == 11:
                self._last_ack = time.monotonic()
            elif op == 1:
                await ws.send(json.dumps({"op": 11, "d": self._seq}))
            elif op == 7:
                logger.info("gateway op7 reconnect requested")
                break
            elif op == 9:
                logger.warning("gateway op9 invalid session; will re-identify")
                self._session_id = None
                self._seq = None
                break

    async def _heartbeat(
        self, ws: Any, interval: float, stop_event: asyncio.Event
    ) -> None:
        await asyncio.sleep(interval * random.uniform(0.0, 1.0))
        while not stop_event.is_set():
            sent = time.monotonic()
            await ws.send(json.dumps({"op": 1, "d": self._seq}))
            await asyncio.sleep(interval)
            if self._last_ack < sent:
                logger.warning("gateway heartbeat ack missing; closing for reconnect")
                await ws.close()
                return