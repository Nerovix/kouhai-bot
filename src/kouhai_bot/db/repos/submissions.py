"""Global submission/clarify/review records (request_id upsert)."""

from __future__ import annotations

from typing import Any

from ..database import Database
from ..timeutil import now_iso

PENDING = "pending"
SUPERSEDED = "superseded"
REPLIED_RESULTS = frozenset({
    "correct", "incorrect", "clarify", "review",
    "timeout", "service_unavailable", "no_statement",
})

_COLUMNS = (
    "request_id", "group_openid", "user_openid", "pid", "type",
    "content", "result", "reason", "reply", "model_tag",
    "synced", "source_group_openid", "seq", "timestamp",
)


def _row_to_record(row: Any) -> dict[str, Any]:
    r = dict(row)
    r["synced"] = bool(r["synced"])
    return r


async def save_submission(db: Database, record: dict[str, Any]) -> None:
    """Upsert by request_id (records without one append). Save-before-deliver."""
    now = now_iso()
    vals = {
        "request_id": record.get("request_id") or None,
        "group_openid": record["group_openid"],
        "user_openid": record["user_openid"],
        "pid": record["pid"],
        "type": record.get("type", "submit"),
        "content": record.get("content", ""),
        "result": record.get("result", PENDING),
        "reason": record.get("reason", ""),
        "reply": record.get("reply", ""),
        "model_tag": record.get("model_tag", ""),
        "synced": 1 if record.get("synced") else 0,
        "source_group_openid": record.get("source_group_openid", ""),
        "seq": int(record.get("seq", 0)),
        "timestamp": record.get("timestamp") or now,
    }
    placeholders = ", ".join("?" for _ in _COLUMNS)
    updates = ", ".join(f"{c}=excluded.{c}" for c in _COLUMNS if c != "request_id")
    await db.execute(
        f"INSERT INTO submissions ({', '.join(_COLUMNS)}, created_at, updated_at) "
        f"VALUES ({placeholders}, ?, ?) "
        f"ON CONFLICT(request_id) DO UPDATE SET {updates}, updated_at=excluded.updated_at",
        (*[vals[c] for c in _COLUMNS], now, now),
    )


async def remove_submission(db: Database, request_id: str) -> None:
    await db.execute("DELETE FROM submissions WHERE request_id=?", (request_id,))


async def get_submission(
    db: Database, request_id: str
) -> dict[str, Any] | None:
    row = await db.fetch_one(
        "SELECT * FROM submissions WHERE request_id=?", (request_id,)
    )
    return _row_to_record(row) if row else None


async def get_user_problem_submissions(
    db: Database,
    group_openid: str,
    user_openid: str,
    pid: str,
    *,
    exclude_request_id: str | None = None,
) -> list[dict[str, Any]]:
    if exclude_request_id:
        rows = await db.fetch_all(
            "SELECT * FROM submissions "
            "WHERE group_openid=? AND user_openid=? AND pid=? "
            "AND (request_id IS NULL OR request_id!=?) ORDER BY id",
            (group_openid, user_openid, pid, exclude_request_id),
        )
    else:
        rows = await db.fetch_all(
            "SELECT * FROM submissions "
            "WHERE group_openid=? AND user_openid=? AND pid=? ORDER BY id",
            (group_openid, user_openid, pid),
        )
    return [_row_to_record(r) for r in rows]


async def clear_user_problem(
    db: Database, group_openid: str, user_openid: str, pid: str
) -> int:
    cur = await db.execute(
        "DELETE FROM submissions WHERE group_openid=? AND user_openid=? AND pid=?",
        (group_openid, user_openid, pid),
    )
    return cur.rowcount


async def get_cross_group_submissions(
    db: Database, user_openid: str, pid: str, exclude_group_openid: str
) -> list[dict[str, Any]]:
    """All other groups' records for this user×problem (for /sync pull)."""
    rows = await db.fetch_all(
        "SELECT * FROM submissions "
        "WHERE user_openid=? AND pid=? AND group_openid!=? ORDER BY timestamp, id",
        (user_openid, pid, exclude_group_openid),
    )
    return [_row_to_record(r) for r in rows]


async def insert_synced_submission(
    db: Database, group_openid: str, record: dict[str, Any]
) -> None:
    """Materialize a pulled record into the target group's view (tagged synced)."""
    imported = dict(record)
    imported["group_openid"] = group_openid
    imported["synced"] = True
    imported["source_group_openid"] = record.get("group_openid", "")
    imported["request_id"] = None  # imported copies are not upsert keys
    await save_submission(db, imported)