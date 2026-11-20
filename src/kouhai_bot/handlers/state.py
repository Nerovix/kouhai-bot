"""DB-backed state helpers for the official-native handler layer.

Replaces the legacy JSON-file helpers in shared.py (get_today_problem /
load_scoreboard / load_problem_statement / …). Handlers and the coordinator
call these; they read the normalized SQLite schema (group = view).
"""

from __future__ import annotations

from typing import Any

from ..db.database import Database
from ..db.repos import posts, problems
from ..problem_content import (
    format_problem_statement_for_llm,
    statement_fingerprint,
)


async def current_problem(db: Database, group_openid: str) -> dict[str, Any] | None:
    """The group's current problem as a legacy-shaped dict (today/name/rating…)."""
    post = await posts.get_current_post(db, group_openid)
    if not post:
        return None
    pid = post["pid"]
    prob = await problems.get_problem(db, pid)
    result: dict[str, Any] = {
        "today": pid,
        "posted_at": post["posted_at"],
        "post_date": post["post_date"],
        "solved_at": post["solved_at"],
        "first_solver_openid": post["first_solver_openid"],
        "card": post["card"],
    }
    if prob:
        result.update({
            "name": prob["name"],
            "rating": prob["rating"],
            "contest_id": prob["contest_id"],
            "idx": prob["idx"],
            "tags": prob["tags"],
            "statement_sha256": prob["statement_sha256"],
        })
    return result


async def current_posted_at(db: Database, group_openid: str) -> int | None:
    post = await posts.get_current_post(db, group_openid)
    return post["posted_at"] if post else None


async def problem_statement_json(db: Database, pid: str) -> dict[str, Any]:
    prob = await problems.get_problem(db, pid)
    return prob["statement"] if prob else {}


async def problem_statement_text(db: Database, pid: str) -> str:
    return format_problem_statement_for_llm(await problem_statement_json(db, pid))


async def problem_rating(db: Database, pid: str) -> int | None:
    prob = await problems.get_problem(db, pid)
    return prob["rating"] if prob else None


async def verified_summary(db: Database, pid: str) -> str | None:
    prob = await problems.get_problem(db, pid)
    if not prob:
        return None
    sha = prob["statement_sha256"] or statement_fingerprint(prob["statement"])
    return await problems.get_verified_summary(db, pid, sha)


async def is_problem_solved(db: Database, group_openid: str, pid: str) -> bool:
    if not pid:
        return False
    row = await db.fetch_one(
        "SELECT 1 FROM group_solves WHERE group_openid=? AND pid=? LIMIT 1",
        (group_openid, pid),
    )
    return row is not None


async def latest_solved_pid(db: Database, group_openid: str) -> str | None:
    row = await db.fetch_one(
        "SELECT pid FROM group_solves WHERE group_openid=? "
        "ORDER BY order_num DESC LIMIT 1",
        (group_openid,),
    )
    return row["pid"] if row else None