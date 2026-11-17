"""Reply-budget quota + durable per-group READY prefetch slot."""

from __future__ import annotations

import json
from typing import Any

from ..database import Database, conn_fetchone
from ..timeutil import now_iso


async def get_active_count(db: Database, group_openid: str, day: str) -> int:
    return await db.fetch_val(
        "SELECT active_count FROM reply_budget WHERE group_openid=? AND day=?",
        (group_openid, day),
        default=0,
    )


async def incr_active(db: Database, group_openid: str, day: str) -> int:
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO reply_budget (group_openid, day, active_count) "
            "VALUES (?, ?, 1) "
            "ON CONFLICT(group_openid, day) DO UPDATE SET "
            "active_count=active_count+1",
            (group_openid, day),
        )
        row = await conn_fetchone(conn,
            "SELECT active_count FROM reply_budget WHERE group_openid=? AND day=?",
            (group_openid, day),
        )
        return row[0]


async def set_prefetch_slot(
    db: Database,
    group_openid: str,
    *,
    slot_id: str,
    problem_json: str,
    statement_sha256: str,
    editorial_sha256: str = "",
) -> None:
    now = now_iso()
    await db.execute(
        "INSERT INTO prefetch_slot "
        "(group_openid, slot_id, problem_json, statement_sha256, editorial_sha256, "
        " claimed, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?) "
        "ON CONFLICT(group_openid) DO UPDATE SET "
        "slot_id=excluded.slot_id, problem_json=excluded.problem_json, "
        "statement_sha256=excluded.statement_sha256, "
        "editorial_sha256=excluded.editorial_sha256, claimed=0, "
        "updated_at=excluded.updated_at",
        (group_openid, slot_id, problem_json, statement_sha256,
         editorial_sha256, now, now),
    )


async def get_prefetch_slot(
    db: Database, group_openid: str
) -> dict[str, Any] | None:
    row = await db.fetch_one(
        "SELECT * FROM prefetch_slot WHERE group_openid=?", (group_openid,)
    )
    if not row:
        return None
    slot = dict(row)
    slot["problem"] = json.loads(slot["problem_json"] or "{}")
    slot["claimed"] = bool(slot["claimed"])
    return slot


async def claim_prefetch_slot(
    db: Database, group_openid: str
) -> dict[str, Any] | None:
    """Atomically claim an unclaimed READY slot (None if absent/claimed)."""
    async with db.transaction() as conn:
        row = await conn_fetchone(conn,
            "SELECT * FROM prefetch_slot WHERE group_openid=? AND claimed=0",
            (group_openid,),
        )
        if not row:
            return None
        await conn.execute(
            "UPDATE prefetch_slot SET claimed=1, updated_at=? WHERE group_openid=?",
            (now_iso(), group_openid),
        )
        slot = dict(row)
        slot["problem"] = json.loads(slot["problem_json"] or "{}")
        slot["claimed"] = True
        return slot


async def release_prefetch_slot(db: Database, group_openid: str) -> None:
    await db.execute(
        "DELETE FROM prefetch_slot WHERE group_openid=?", (group_openid,)
    )