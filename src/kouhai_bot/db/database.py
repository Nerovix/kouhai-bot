"""Async SQLite access layer (aiosqlite).

Single-process bot → one shared connection, WAL mode. Explicit
``BEGIN IMMEDIATE`` transactions cover the read-modify-write sections that
the legacy code protected with the per-group coordinator lock plus
``atomic_write_json`` (scoreboard, bad_reports append, last_interaction,
prefetch slot claim).
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

import aiosqlite

from .schema import SCHEMA_SQL, SCHEMA_VERSION

logger = logging.getLogger("kouhai-bot.db")


async def conn_fetchone(
    conn: aiosqlite.Connection, sql: str, params: Sequence[Any] = ()
) -> aiosqlite.Row | None:
    """fetchone helper usable on a raw connection inside a transaction."""
    async with conn.execute(sql, params) as cur:
        return await cur.fetchone()


class Database:
    """A single async SQLite connection with schema bootstrap."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not open; call open() first")
        return self._conn

    async def open(self) -> None:
        if self._conn is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None → autocommit; transactions are explicit.
        self._conn = await aiosqlite.connect(
            str(self._path), isolation_level=None
        )
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._init_schema()

    async def _init_schema(self) -> None:
        await self.conn.executescript(SCHEMA_SQL)
        await self.conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def execute(
        self, sql: str, params: Sequence[Any] = ()
    ) -> aiosqlite.Cursor:
        return await self.conn.execute(sql, params)

    async def executemany(
        self, sql: str, seq: Sequence[Sequence[Any]]
    ) -> aiosqlite.Cursor:
        return await self.conn.executemany(sql, seq)

    async def fetch_one(
        self, sql: str, params: Sequence[Any] = ()
    ) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetch_all(
        self, sql: str, params: Sequence[Any] = ()
    ) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchall()

    async def fetch_val(
        self, sql: str, params: Sequence[Any] = (), default: Any = None
    ) -> Any:
        row = await self.fetch_one(sql, params)
        if row is None:
            return default
        return row[0]

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Serialize a read-modify-write section (BEGIN IMMEDIATE)."""
        conn = self.conn
        await conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            await conn.execute("ROLLBACK")
            raise
        else:
            await conn.execute("COMMIT")


_database: Database | None = None


def get_database() -> Database:
    """Process-wide database singleton, rooted at the configured data_dir."""
    global _database
    if _database is None:
        from ..config import get_config

        cfg = get_config()
        _database = Database(Path(cfg.data_dir) / "kouhai.db")
    return _database


def set_database(db: Database | None) -> None:
    """Override the singleton (tests / explicit bootstrap)."""
    global _database
    _database = db