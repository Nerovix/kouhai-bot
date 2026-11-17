"""Group posts, per-group first-blood ledger, and the "group = view" scoreboard."""

from __future__ import annotations

import json
import math
from typing import Any

from ..database import Database, conn_fetchone
from ..timeutil import beijing_date, now_iso, unix_now


def rating_to_points(rating: int) -> float:
    return math.pow(2.0, (rating - 2000) / 300)


async def post_problem(
    db: Database,
    group_openid: str,
    pid: str,
    *,
    posted_at: int | None = None,
    post_date: str | None = None,
    card_json: str = "{}",
) -> None:
    """Publish a problem as the group's current view (demote previous)."""
    posted_at = unix_now() if posted_at is None else posted_at
    post_date = beijing_date(posted_at) if post_date is None else post_date
    now = now_iso()
    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE group_posts SET is_current=0 WHERE group_openid=? AND is_current=1",
            (group_openid,),
        )
        await conn.execute(
            "INSERT INTO group_posts "
            "(group_openid, pid, posted_at, post_date, card_json, is_current, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?) "
            "ON CONFLICT(group_openid, pid) DO UPDATE SET "
            "posted_at=excluded.posted_at, post_date=excluded.post_date, "
            "card_json=excluded.card_json, is_current=1",
            (group_openid, pid, posted_at, post_date, card_json, now),
        )


def _post_row(row: Any) -> dict[str, Any]:
    p = dict(row)
    p["card"] = json.loads(p["card_json"] or "{}")
    return p


async def get_current_post(
    db: Database, group_openid: str
) -> dict[str, Any] | None:
    row = await db.fetch_one(
        "SELECT * FROM group_posts WHERE group_openid=? AND is_current=1",
        (group_openid,),
    )
    return _post_row(row) if row else None


async def get_post(
    db: Database, group_openid: str, pid: str
) -> dict[str, Any] | None:
    row = await db.fetch_one(
        "SELECT * FROM group_posts WHERE group_openid=? AND pid=?",
        (group_openid, pid),
    )
    return _post_row(row) if row else None


async def record_solve(
    db: Database,
    group_openid: str,
    user_openid: str,
    pid: str,
    nickname: str,
    *,
    source: str = "native",
    solved_at: int | None = None,
) -> dict[str, Any]:
    """Record a solve in the group's view.

    Dedup is per (user, pid): a repeat solve returns is_first_blood=False
    without a new ledger row. First blood = no prior solve for this pid in
    this group. order_num is the group's monotonic solve counter.
    """
    solved_at = unix_now() if solved_at is None else solved_at
    now = now_iso()
    async with db.transaction() as conn:
        existing = await conn_fetchone(conn,
            "SELECT id FROM group_solves "
            "WHERE group_openid=? AND user_openid=? AND pid=?",
            (group_openid, user_openid, pid),
        )
        if existing:
            count = await conn_fetchone(conn,
                "SELECT COUNT(*) FROM group_solves WHERE group_openid=? AND user_openid=?",
                (group_openid, user_openid),
            )
            return {
                "is_first_blood": False,
                "order_num": None,
                "solved_count": count[0],
                "duplicate": True,
            }

        prior = await conn_fetchone(conn,
            "SELECT COUNT(*) FROM group_solves WHERE group_openid=? AND pid=?",
            (group_openid, pid),
        )
        is_first_blood = prior[0] == 0
        total = await conn_fetchone(conn,
            "SELECT COUNT(*) FROM group_solves WHERE group_openid=?",
            (group_openid,),
        )
        order_num = total[0] + 1
        await conn.execute(
            "INSERT INTO group_solves "
            "(group_openid, user_openid, pid, nickname, solve_date, order_num, "
            " source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (group_openid, user_openid, pid, nickname,
             beijing_date(solved_at), order_num, source, now),
        )
        if is_first_blood:
            await conn.execute(
                "UPDATE group_posts SET solved_at=?, first_solver_openid=? "
                "WHERE group_openid=? AND pid=?",
                (solved_at, user_openid, group_openid, pid),
            )
        count = await conn_fetchone(conn,
            "SELECT COUNT(*) FROM group_solves WHERE group_openid=? AND user_openid=?",
            (group_openid, user_openid),
        )
        return {
            "is_first_blood": is_first_blood,
            "order_num": order_num,
            "solved_count": count[0],
            "duplicate": False,
        }


async def get_group_solves(
    db: Database, group_openid: str
) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT * FROM group_solves WHERE group_openid=? ORDER BY order_num",
        (group_openid,),
    )
    return [dict(r) for r in rows]


async def group_scoreboard(
    db: Database, group_openid: str
) -> list[dict[str, Any]]:
    """Weighted leaderboard for the group's view (native + synced solves).

    score = Σ 2^((rating-2000)/300); unknown rating contributes nothing.
    Sort by (-round(score,6), user_openid); equal scores share a rank.
    Display nickname prefers the latest users.nickname, then the solve
    snapshot, then the openid (never a generic placeholder).
    """
    rows = await db.fetch_all(
        "SELECT s.user_openid, s.pid, s.nickname AS snap_nick, "
        "       u.nickname AS live_nick, p.rating "
        "FROM group_solves s "
        "LEFT JOIN users u ON u.user_openid = s.user_openid "
        "LEFT JOIN problems p ON p.pid = s.pid "
        "WHERE s.group_openid=? ORDER BY s.order_num",
        (group_openid,),
    )
    totals: dict[str, dict[str, Any]] = {}
    for r in rows:
        uid = r["user_openid"]
        entry = totals.setdefault(uid, {
            "user_openid": uid,
            "nickname": r["live_nick"] or r["snap_nick"] or uid,
            "solved": 0,
            "score": 0.0,
        })
        entry["solved"] += 1
        if r["rating"] is not None:
            entry["score"] += rating_to_points(r["rating"])

    ranked = sorted(
        totals.values(),
        key=lambda item: (-round(item["score"], 6), str(item["user_openid"])),
    )
    prev_score: float | None = None
    display_rank = 0
    for idx, entry in enumerate(ranked, 1):
        score = round(entry["score"], 6)
        if score != prev_score:
            display_rank = idx
            prev_score = score
        entry["rank"] = display_rank
    return ranked


async def mark_used(db: Database, group_openid: str, pid: str) -> None:
    await db.execute(
        "INSERT INTO group_used (group_openid, pid, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT(group_openid, pid) DO NOTHING",
        (group_openid, pid, now_iso()),
    )


async def get_excluded_pids(db: Database, group_openid: str) -> set[str]:
    """Picker exclusion set: used ∪ posted ∪ solved in this group."""
    rows = await db.fetch_all(
        "SELECT pid FROM group_used WHERE group_openid=? "
        "UNION SELECT pid FROM group_posts WHERE group_openid=? "
        "UNION SELECT pid FROM group_solves WHERE group_openid=?",
        (group_openid, group_openid, group_openid),
    )
    return {r["pid"] for r in rows}