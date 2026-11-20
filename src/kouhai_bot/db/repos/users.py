"""User and group repositories (openid native)."""

from __future__ import annotations

import json
from typing import Any

from ..database import Database
from ..timeutil import now_iso


async def upsert_user(
    db: Database, user_openid: str, nickname: str | None = None
) -> None:
    now = now_iso()
    if nickname is None:
        await db.execute(
            "INSERT INTO users (user_openid, created_at, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(user_openid) DO UPDATE SET updated_at=excluded.updated_at",
            (user_openid, now, now),
        )
    else:
        await db.execute(
            "INSERT INTO users (user_openid, nickname, created_at, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_openid) DO UPDATE SET "
            "nickname=excluded.nickname, updated_at=excluded.updated_at",
            (user_openid, nickname, now, now),
        )


async def get_user(db: Database, user_openid: str) -> dict[str, Any] | None:
    row = await db.fetch_one(
        "SELECT * FROM users WHERE user_openid=?", (user_openid,)
    )
    return dict(row) if row else None


async def set_user_nickname(
    db: Database, user_openid: str, nickname: str
) -> None:
    await upsert_user(db, user_openid, nickname=nickname)


async def upsert_group(
    db: Database, group_openid: str, name: str | None = None
) -> None:
    now = now_iso()
    if name is None:
        await db.execute(
            "INSERT INTO groups (group_openid, created_at, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(group_openid) DO UPDATE SET updated_at=excluded.updated_at",
            (group_openid, now, now),
        )
    else:
        await db.execute(
            "INSERT INTO groups (group_openid, name, created_at, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(group_openid) DO UPDATE SET "
            "name=excluded.name, updated_at=excluded.updated_at",
            (group_openid, name, now, now),
        )


async def get_group(db: Database, group_openid: str) -> dict[str, Any] | None:
    row = await db.fetch_one(
        "SELECT * FROM groups WHERE group_openid=?", (group_openid,)
    )
    if not row:
        return None
    g = dict(row)
    g["enabled_jobs"] = json.loads(g["enabled_jobs"] or "[]")
    g["job_schedules"] = json.loads(g["job_schedules"] or "{}")
    return g


async def list_groups(db: Database) -> list[dict[str, Any]]:
    rows = await db.fetch_all("SELECT group_openid FROM groups ORDER BY created_at")
    return [dict(r) for r in rows]


async def set_group_rating(
    db: Database, group_openid: str, min_rating: int | None, max_rating: int | None
) -> None:
    await upsert_group(db, group_openid)
    await db.execute(
        "UPDATE groups SET min_rating=?, max_rating=?, updated_at=? "
        "WHERE group_openid=?",
        (min_rating, max_rating, now_iso(), group_openid),
    )


async def get_group_rating(
    db: Database, group_openid: str
) -> tuple[int | None, int | None]:
    row = await db.fetch_one(
        "SELECT min_rating, max_rating FROM groups WHERE group_openid=?",
        (group_openid,),
    )
    if not row:
        return (None, None)
    return (row["min_rating"], row["max_rating"])


async def set_group_jobs(
    db: Database,
    group_openid: str,
    enabled_jobs: list[str],
    job_schedules: dict[str, str] | None = None,
) -> None:
    await upsert_group(db, group_openid)
    await db.execute(
        "UPDATE groups SET enabled_jobs=?, job_schedules=?, updated_at=? "
        "WHERE group_openid=?",
        (
            json.dumps(enabled_jobs, ensure_ascii=False),
            json.dumps(job_schedules or {}, ensure_ascii=False),
            now_iso(),
            group_openid,
        ),
    )