"""/feedback reports (raise-on-corrupt) + last-interaction cache (warn-on-corrupt)."""

from __future__ import annotations

import json
import logging
from typing import Any

from ..database import Database
from ..timeutil import now_iso

logger = logging.getLogger("kouhai-bot.db.feedback")


async def append_bad_report(
    db: Database,
    *,
    group_openid: str,
    user_openid: str,
    note: str,
    cmd_message_id: str,
    target_pid: str,
    target_record: dict[str, Any],
    scope: str = "group",
    reported_at: str | None = None,
) -> int:
    now = now_iso()
    cur = await db.execute(
        "INSERT INTO bad_reports "
        "(scope, group_openid, user_openid, reported_at, note, cmd_message_id, "
        " target_pid, target_record_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (scope, group_openid, user_openid, reported_at or now, note[:500],
         cmd_message_id, target_pid,
         json.dumps(target_record, ensure_ascii=False), now),
    )
    return cur.lastrowid


async def load_bad_reports(
    db: Database, group_openid: str
) -> list[dict[str, Any]]:
    """Append-only report history. A corrupt snapshot raises (never reset)."""
    rows = await db.fetch_all(
        "SELECT * FROM bad_reports WHERE group_openid=? ORDER BY id",
        (group_openid,),
    )
    reports: list[dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        # A malformed stored snapshot is corruption, not an empty store.
        item["target_record"] = json.loads(item["target_record_json"])
        item["target"] = {
            "problem": item["target_pid"],
            "record": item["target_record"],
        }
        reports.append(item)
    return reports


async def set_last_interaction(
    db: Database,
    group_openid: str,
    user_openid: str,
    record: dict[str, Any],
    *,
    scope: str = "group",
) -> None:
    await db.execute(
        "INSERT INTO last_interaction "
        "(scope, group_openid, user_openid, record_json, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(scope, group_openid, user_openid) DO UPDATE SET "
        "record_json=excluded.record_json, updated_at=excluded.updated_at",
        (scope, group_openid, user_openid,
         json.dumps(record, ensure_ascii=False), now_iso()),
    )


async def get_last_interaction(
    db: Database, group_openid: str, user_openid: str, *, scope: str = "group"
) -> dict[str, Any] | None:
    """CACHE semantics: a corrupt/missing entry reads as empty (warn + None)."""
    row = await db.fetch_one(
        "SELECT record_json FROM last_interaction "
        "WHERE scope=? AND group_openid=? AND user_openid=?",
        (scope, group_openid, user_openid),
    )
    if not row:
        return None
    try:
        return json.loads(row["record_json"])
    except (json.JSONDecodeError, TypeError):
        logger.warning(
            "corrupt last_interaction cache for %s/%s; treating as empty",
            group_openid, user_openid,
        )
        return None