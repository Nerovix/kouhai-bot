"""Problem catalog + editorial repositories (fingerprint-gated)."""

from __future__ import annotations

import json
from typing import Any

from ..database import Database
from ..timeutil import now_iso


async def upsert_problem(
    db: Database,
    pid: str,
    *,
    contest_id: int | None = None,
    idx: str = "",
    name: str = "",
    rating: int | None = None,
    tags: list[str] | None = None,
    statement_sha256: str = "",
    statement_json: str = "{}",
) -> None:
    now = now_iso()
    await db.execute(
        "INSERT INTO problems "
        "(pid, contest_id, idx, name, rating, tags, statement_sha256, "
        " statement_json, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(pid) DO UPDATE SET "
        "contest_id=excluded.contest_id, idx=excluded.idx, name=excluded.name, "
        "rating=excluded.rating, tags=excluded.tags, "
        "statement_sha256=excluded.statement_sha256, "
        "statement_json=excluded.statement_json, updated_at=excluded.updated_at",
        (
            pid, contest_id, idx, name, rating,
            json.dumps(tags or [], ensure_ascii=False),
            statement_sha256, statement_json, now, now,
        ),
    )


async def get_problem(db: Database, pid: str) -> dict[str, Any] | None:
    row = await db.fetch_one("SELECT * FROM problems WHERE pid=?", (pid,))
    if not row:
        return None
    p = dict(row)
    p["tags"] = json.loads(p["tags"] or "[]")
    p["statement"] = json.loads(p["statement_json"] or "{}")
    return p


async def set_problem_summary(
    db: Database,
    pid: str,
    summary_zh: str,
    status: str,
    statement_sha256: str,
) -> None:
    await db.execute(
        "UPDATE problems SET summary_zh=?, summary_status=?, summary_sha256=?, "
        "updated_at=? WHERE pid=?",
        (summary_zh, status, statement_sha256, now_iso(), pid),
    )


async def get_verified_summary(
    db: Database, pid: str, statement_sha256: str
) -> str | None:
    """Return the Chinese summary only when verified AND source-bound."""
    row = await db.fetch_one(
        "SELECT summary_zh, summary_status, summary_sha256 FROM problems WHERE pid=?",
        (pid,),
    )
    if not row:
        return None
    if row["summary_status"] != "verified":
        return None
    if row["summary_sha256"] != statement_sha256:
        return None
    return row["summary_zh"] or None


async def upsert_editorial_source(
    db: Database,
    pid: str,
    *,
    tutorial_url: str,
    tutorial_title: str,
    sections_json: str,
    source_sha256: str,
    statement_sha256: str,
) -> None:
    now = now_iso()
    await db.execute(
        "INSERT INTO editorials "
        "(pid, tutorial_url, tutorial_title, sections_json, source_sha256, "
        " statement_sha256, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(pid) DO UPDATE SET "
        "tutorial_url=excluded.tutorial_url, tutorial_title=excluded.tutorial_title, "
        "sections_json=excluded.sections_json, source_sha256=excluded.source_sha256, "
        "statement_sha256=excluded.statement_sha256, "
        "translation_status='', translation_zh='', updated_at=excluded.updated_at",
        (pid, tutorial_url, tutorial_title, sections_json, source_sha256,
         statement_sha256, now),
    )


async def set_editorial_translation(
    db: Database, pid: str, translation_zh: str, status: str
) -> None:
    await db.execute(
        "UPDATE editorials SET translation_zh=?, translation_status=?, updated_at=? "
        "WHERE pid=?",
        (translation_zh, status, now_iso(), pid),
    )


async def set_no_editorial(
    db: Database, pid: str, statement_sha256: str, reason: str
) -> None:
    now = now_iso()
    await db.execute(
        "INSERT INTO editorials "
        "(pid, statement_sha256, translation_status, no_editorial_reason, "
        " search_complete, updated_at) "
        "VALUES (?, ?, 'no_editorial', ?, 1, ?) "
        "ON CONFLICT(pid) DO UPDATE SET "
        "statement_sha256=excluded.statement_sha256, "
        "translation_status='no_editorial', "
        "no_editorial_reason=excluded.no_editorial_reason, "
        "search_complete=1, updated_at=excluded.updated_at",
        (pid, statement_sha256, reason, now),
    )


async def get_verified_editorial(
    db: Database, pid: str, statement_sha256: str
) -> dict[str, Any] | None:
    """English source only when translation marker binds editorial+statement."""
    row = await db.fetch_one("SELECT * FROM editorials WHERE pid=?", (pid,))
    if not row:
        return None
    if row["translation_status"] != "verified":
        return None
    if row["statement_sha256"] != statement_sha256:
        return None
    e = dict(row)
    e["sections"] = json.loads(e["sections_json"] or "[]")
    return e


async def get_editorial_state(
    db: Database, pid: str, statement_sha256: str
) -> str:
    """'verified' | 'no_editorial' | 'incomplete' for the given statement."""
    row = await db.fetch_one(
        "SELECT translation_status, statement_sha256 FROM editorials WHERE pid=?",
        (pid,),
    )
    if not row or row["statement_sha256"] != statement_sha256:
        return "incomplete"
    status = row["translation_status"]
    if status in {"verified", "no_editorial"}:
        return status
    return "incomplete"