"""Runtime hot-migration patch — old service group only.

The official API reveals a member's ``openid`` only when they interact, and
there is no roster/history endpoint (member-list API is invite-only: code
11253). So the legacy file-store (keyed by numeric QQ id) cannot be mapped in
bulk. Instead, on every service-group message we capture ``(openid, nickname)``,
match the nickname against the staged source DB, and immediately migrate that
member's old GROUP solves + submissions into the live SQLite — once, atomically,
idempotent via ``legacy_migration_map``. The global problem catalog is loaded
once (insert-if-absent, never clobbering live-prepared problems).

Gated by config ``legacy_migration.{enabled,group_openid,source_db}``; other
groups are untouched (they have no legacy data). Private-judge data is NOT
migrated (owner decision: keep only the group copy). The staged source DB is
local runtime data produced by a throwaway script — never committed.
"""

from __future__ import annotations

import asyncio
import logging
import os

import aiosqlite

from .config import get_config
from .db.database import Database, get_database
from .db.repos import users
from .db.timeutil import now_iso

logger = logging.getLogger("kouhai-bot.legacy_migration")

_lock = asyncio.Lock()
_catalog_loaded = False
_source_conn: aiosqlite.Connection | None = None
_source_tried = False
_inflight: set[str] = set()


def is_migration_group(group_openid: str) -> bool:
    """True only for the configured legacy service group (cheap, no IO)."""
    if not group_openid:
        return False
    try:
        cfg = get_config()
    except Exception:
        return False
    return bool(
        cfg.legacy_migration_enabled
        and group_openid == cfg.legacy_migration_group_openid
    )


async def ensure_order_floor(db: Database) -> None:
    """Startup hook: persist the legacy order_num floor before any event arrives.

    Native solves number above the old global max so they never collide with a
    legacy order whose owner has not spoken (and may never speak). Loading the
    floor at startup — before the gateway connects — closes the race where the
    first inbound message solves a problem (e.g. the AC backdoor) before the
    fire-and-forget migration task has run. No-op when migration is disabled;
    idempotent (the catalog load is once-only).
    """
    try:
        cfg = get_config()
    except Exception:
        return
    if not cfg.legacy_migration_enabled or not cfg.legacy_migration_group_openid:
        return
    await _ensure_catalog(db)


async def _get_source() -> aiosqlite.Connection | None:
    """Lazily open the staged source DB read-only (cached, fail-once)."""
    global _source_conn, _source_tried
    if _source_conn is not None:
        return _source_conn
    if _source_tried:
        return None
    _source_tried = True
    try:
        cfg = get_config()
    except Exception:
        return None
    path = cfg.legacy_migration_source_db
    if not path or not os.path.isfile(path):
        logger.warning("[legacy_migration] source_db missing: %r", path)
        return None
    try:
        conn = await aiosqlite.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='legacy_users'"
        ) as cur:
            if await cur.fetchone() is None:
                logger.warning("[legacy_migration] source_db has no legacy_users table")
                await conn.close()
                return None
    except Exception as exc:
        logger.error("[legacy_migration] cannot open source_db: %s", exc)
        return None
    _source_conn = conn
    return conn


async def _ensure_catalog(db: Database) -> None:
    """Load the global problem catalog once (insert-if-absent)."""
    global _catalog_loaded
    if _catalog_loaded:
        return
    async with _lock:
        if _catalog_loaded:
            return
        src = await _get_source()
        if src is None:
            return
        probs = await src.execute_fetchall("SELECT * FROM legacy_problems")
        eds = await src.execute_fetchall("SELECT * FROM legacy_editorials")
        floor_rows = await src.execute_fetchall(
            "SELECT COALESCE(MAX(order_num), 0) AS mx FROM legacy_solves"
        )
        order_floor = int(floor_rows[0]["mx"])
        mig_group = get_config().legacy_migration_group_openid
        now = now_iso()
        async with db.transaction() as conn:
            for p in probs:
                await conn.execute(
                    "INSERT INTO problems "
                    "(pid, contest_id, idx, name, rating, tags, statement_sha256, "
                    " statement_json, summary_zh, summary_status, summary_sha256, "
                    " created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(pid) DO NOTHING",
                    (
                        p["pid"], p["contest_id"], p["idx"], p["name"], p["rating"],
                        p["tags_json"], p["statement_sha256"], p["statement_json"],
                        p["summary_zh"], p["summary_status"], p["summary_sha256"],
                        now, now,
                    ),
                )
            for e in eds:
                await conn.execute(
                    "INSERT INTO editorials "
                    "(pid, tutorial_url, tutorial_title, sections_json, source_sha256, "
                    " translation_zh, translation_status, statement_sha256, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(pid) DO NOTHING",
                    (
                        e["pid"], e["tutorial_url"], e["tutorial_title"],
                        e["sections_json"], e["source_sha256"], e["translation_zh"],
                        e["translation_status"], e["statement_sha256"], now,
                    ),
                )
            if mig_group:
                await conn.execute(
                    "INSERT INTO group_order_floor (group_openid, floor) VALUES (?,?) "
                    "ON CONFLICT(group_openid) DO UPDATE SET "
                    "floor = MAX(group_order_floor.floor, excluded.floor)",
                    (mig_group, order_floor),
                )
        _catalog_loaded = True
        logger.info(
            "[legacy_migration] catalog loaded: %d problems, %d editorials, order_floor=%d",
            len(probs), len(eds), order_floor,
        )


async def _match_nickname(src: aiosqlite.Connection, nickname: str) -> str | None:
    """Resolve a nickname to a unique old QQ id (latest preferred, then any).

    Exact stripped match only — ambiguous nicknames (claimed by >1 old user)
    are skipped and logged for manual resolution. Parameterized queries keep
    nickname content (incl. joke SQL strings) inert.
    """
    nick = (nickname or "").strip()
    if not nick:
        return None
    rows = await src.execute_fetchall(
        "SELECT DISTINCT old_user_id FROM nickname_index "
        "WHERE nickname=? AND is_latest=1",
        (nick,),
    )
    if len(rows) == 1:
        return rows[0]["old_user_id"]
    if len(rows) > 1:
        logger.warning(
            "[legacy_migration] ambiguous latest nickname %r -> %s",
            nick, [r["old_user_id"] for r in rows],
        )
        return None
    rows = await src.execute_fetchall(
        "SELECT DISTINCT old_user_id FROM nickname_index WHERE nickname=?",
        (nick,),
    )
    if len(rows) == 1:
        return rows[0]["old_user_id"]
    if len(rows) > 1:
        logger.warning(
            "[legacy_migration] ambiguous nickname %r -> %s",
            nick, [r["old_user_id"] for r in rows],
        )
    return None


async def _migrate_user(
    db: Database, group_openid: str, user_openid: str, nickname: str
) -> None:
    src = await _get_source()
    if src is None:
        return
    existing = await db.fetch_one(
        "SELECT old_user_id FROM legacy_migration_map "
        "WHERE group_openid=? AND user_openid=?",
        (group_openid, user_openid),
    )
    if existing:
        return
    old_uid = await _match_nickname(src, nickname)
    if old_uid is None:
        logger.info(
            "[legacy_migration] no unique match for nickname %r (openid %s…)",
            nickname, user_openid[:8],
        )
        return
    solves = await src.execute_fetchall(
        "SELECT pid, solve_date, order_num, nickname FROM legacy_solves "
        "WHERE old_user_id=? ORDER BY order_num",
        (old_uid,),
    )
    subs = await src.execute_fetchall(
        "SELECT pid, type, content, result, reason, reply, model_tag, timestamp, seq "
        "FROM legacy_submissions WHERE old_user_id=? ORDER BY seq",
        (old_uid,),
    )
    now = now_iso()
    async with db.transaction() as conn:
        # Re-check inside the transaction: a concurrent task may have committed
        # this member already (NULL request_id submissions append, so the map
        # row is the only idempotency guard).
        async with conn.execute(
            "SELECT 1 FROM legacy_migration_map WHERE group_openid=? AND user_openid=?",
            (group_openid, user_openid),
        ) as cur:
            if await cur.fetchone():
                return
        for s in solves:
            await conn.execute(
                "INSERT INTO group_solves "
                "(group_openid, user_openid, pid, nickname, solve_date, order_num, "
                " source, created_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(group_openid, user_openid, pid) DO NOTHING",
                (
                    group_openid, user_openid, s["pid"], s["nickname"],
                    s["solve_date"], s["order_num"], "migrated", now,
                ),
            )
        for r in subs:
            await conn.execute(
                "INSERT INTO submissions "
                "(request_id, group_openid, user_openid, pid, type, content, result, "
                " reason, reply, model_tag, synced, source_group_openid, seq, "
                " timestamp, created_at, updated_at) "
                "VALUES (NULL,?,?,?,?,?,?,?,?,?,0,'',?,?,?,?)",
                (
                    group_openid, user_openid, r["pid"], r["type"], r["content"],
                    r["result"], r["reason"], r["reply"], r["model_tag"],
                    r["seq"], r["timestamp"], now, now,
                ),
            )
        await conn.execute(
            "INSERT INTO legacy_migration_map "
            "(group_openid, user_openid, old_user_id, nickname, migrated_at) "
            "VALUES (?,?,?,?,?)",
            (group_openid, user_openid, old_uid, nickname, now),
        )
    logger.info(
        "[legacy_migration] migrated openid %s… -> old QQ %s: %d solves, %d submissions",
        user_openid[:8], old_uid, len(solves), len(subs),
    )


async def maybe_migrate(
    group_openid: str, user_openid: str, nickname: str
) -> None:
    """Capture identity + hot-migrate this member (service group only)."""
    if not is_migration_group(group_openid) or not user_openid:
        return
    try:
        db = get_database()
        await users.upsert_user(db, user_openid, nickname or None)
        await _ensure_catalog(db)
        await _migrate_user(db, group_openid, user_openid, nickname)
    except Exception as exc:
        logger.error(
            "[legacy_migration] failed for openid %s…: %s",
            user_openid[:8], exc, exc_info=True,
        )


def schedule_migration(
    group_openid: str, user_openid: str, nickname: str
) -> None:
    """Fire-and-forget hook for the dispatch loop (never blocks receipt).

    Dedups concurrent tasks per openid; correctness is still guaranteed by the
    in-transaction map re-check even if two tasks race.
    """
    if not is_migration_group(group_openid) or not user_openid:
        return
    if user_openid in _inflight:
        return
    _inflight.add(user_openid)

    async def _run() -> None:
        try:
            await maybe_migrate(group_openid, user_openid, nickname)
        finally:
            _inflight.discard(user_openid)

    asyncio.create_task(_run(), name=f"legacy_migrate_{user_openid[:8]}")


def reset_for_tests() -> None:
    global _catalog_loaded, _source_conn, _source_tried
    _catalog_loaded = False
    _source_conn = None
    _source_tried = False
    _inflight.clear()


async def aclose_source() -> None:
    """Close the cached source connection (clean shutdown / test teardown)."""
    global _source_conn, _source_tried, _catalog_loaded
    if _source_conn is not None:
        await _source_conn.close()
    _source_conn = None
    _source_tried = False
    _catalog_loaded = False