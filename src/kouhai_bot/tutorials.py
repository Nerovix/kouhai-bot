"""DB-backed editorial read/write boundary (replaces disk JSON markers).

The side-effect-free search/validate/translate action lives in
``editorial_preparation.py`` (still reads the mirrored disk statement cache +
tutorial agent). This module commits only complete results to the SQLite
``editorials`` table and reads verified source/translation back, gated by the
statement fingerprint. Runtime callers (judge second pass, /review, delivery)
use these async helpers.
"""

from __future__ import annotations

import hashlib
import json
import logging

from .db.database import get_database
from .db.repos import problems
from .editorial_content import (
    MIN_EDITORIAL_LEN,
    OfficialEditorial,
    editorial_from_bundle,
)
from .editorial_preparation import (
    EditorialPreparationStatus,
    PreparedEditorial,
    prepare_editorial,
)
from .llm import strip_leaked_thinking
from .problem_content import statement_fingerprint

_REVIEW_EDITORIAL_MAX_LEN = 12000

logger = logging.getLogger("kouhai-bot.tutorials")


def _editorial_fingerprint(editorial: OfficialEditorial) -> str:
    payload = "\0".join(
        [editorial.tutorial_url, editorial.tutorial_title, editorial.text]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _statement_sha(pid: str) -> str:
    prob = await problems.get_problem(get_database(), pid)
    if not prob:
        return ""
    return prob["statement_sha256"] or statement_fingerprint(prob["statement"])


def _editorial_from_row(row: dict) -> OfficialEditorial | None:
    bundle = {
        "tutorial_url": row.get("tutorial_url") or "",
        "tutorial_title": row.get("tutorial_title") or "",
        "sections": row.get("sections") or [],
    }
    return editorial_from_bundle(bundle)


async def get_editorial_state(pid: str) -> str:
    """'verified' | 'no_editorial' | 'incomplete' bound to the current statement."""
    sha = await _statement_sha(pid)
    if not sha:
        return "incomplete"
    return await problems.get_editorial_state(get_database(), pid, sha)


async def has_cached_editorial_zh(pid: str) -> bool:
    return await get_editorial_state(pid) == "verified"


async def is_no_official_editorial(pid: str) -> bool:
    return await get_editorial_state(pid) == "no_editorial"


async def load_cached_editorial_zh(pid: str) -> str:
    sha = await _statement_sha(pid)
    row = await problems.get_verified_editorial(get_database(), pid, sha)
    if not row:
        return ""
    return strip_leaked_thinking((row.get("translation_zh") or "").strip())


async def get_verified_official_editorial(pid: str) -> OfficialEditorial | None:
    sha = await _statement_sha(pid)
    if not sha:
        return None
    row = await problems.get_verified_editorial(get_database(), pid, sha)
    if not row:
        return None
    return _editorial_from_row(row)


async def mark_no_official_editorial(pid: str, *, reason: str) -> None:
    reason = (reason or "").strip()
    if not reason:
        raise ValueError(f"cannot mark no editorial for {pid}: reason missing")
    sha = await _statement_sha(pid)
    if not sha:
        raise ValueError(f"cannot mark no editorial for {pid}: statement missing")
    await problems.set_no_editorial(get_database(), pid, sha, reason)


async def _commit_prepared_editorial(pid: str, prepared: PreparedEditorial) -> bool:
    """Publish source + translation to DB; verified status written last."""
    db = get_database()
    current_sha = await _statement_sha(pid)
    if not current_sha or current_sha != prepared.statement_sha256:
        logger.warning(
            "refusing to persist editorial for %s: statement changed during preparation",
            pid,
        )
        return False
    text = strip_leaked_thinking(prepared.translated_text).strip()
    if len(text) < MIN_EDITORIAL_LEN:
        logger.warning(
            "refusing to mark editorial translation verified for %s: content incomplete",
            pid,
        )
        return False
    editorial = prepared.editorial
    source_sha = _editorial_fingerprint(editorial)
    await problems.upsert_editorial_source(
        db,
        pid,
        tutorial_url=editorial.tutorial_url,
        tutorial_title=editorial.tutorial_title,
        sections_json=json.dumps(
            prepared.bundle.get("sections") or [], ensure_ascii=False
        ),
        source_sha256=source_sha,
        statement_sha256=prepared.statement_sha256,
    )
    await problems.set_editorial_translation(
        db, pid, text, "verified", source_sha256=source_sha,
    )
    return True


async def prefetch_editorial_zh(pid: str, *, run_agent: bool = True) -> None:
    """Prepare and commit a verified editorial ahead of first AC."""
    pid = (pid or "").strip()
    if not pid:
        return
    state = await get_editorial_state(pid)
    if state in {"verified", "no_editorial"}:
        return

    initial_bundle = await _initial_bundle(pid)
    result = await prepare_editorial(
        pid,
        initial_bundle=initial_bundle,
        run_agent=run_agent,
    )
    if (
        result.status is EditorialPreparationStatus.READY
        and result.prepared is not None
    ):
        if await _commit_prepared_editorial(pid, result.prepared):
            logger.info(
                "editorial prefetch verified candidate for %s url=%s",
                pid,
                result.prepared.editorial.tutorial_url or "(missing)",
            )
        return

    if result.status is EditorialPreparationStatus.EXHAUSTIVE_NO_MATCH:
        await mark_no_official_editorial(
            pid, reason=f"agent_exhaustive_no_match:{result.reason}"
        )
        return

    logger.info(
        "editorial prefetch remains incomplete for %s: %s", pid, result.reason
    )


async def _initial_bundle(pid: str) -> dict | None:
    """Reconstruct a candidate bundle from any persisted editorial source."""
    row = await get_database().fetch_one(
        "SELECT tutorial_url, tutorial_title, sections_json FROM editorials WHERE pid=?",
        (pid,),
    )
    if not row:
        return None
    sections = json.loads(row["sections_json"] or "[]")
    if not sections:
        return None
    return {
        "tutorial_url": row["tutorial_url"] or "",
        "tutorial_title": row["tutorial_title"] or "",
        "sections": sections,
    }


def format_editorial_for_review(editorial: OfficialEditorial) -> str:
    body = editorial.text
    if len(body) > _REVIEW_EDITORIAL_MAX_LEN:
        body = body[:_REVIEW_EDITORIAL_MAX_LEN] + "\n...(题解已截断)"
    lines = [
        "官方题解（仅你可见，群友不知道）：",
        body,
    ]
    if editorial.tutorial_url:
        lines.append(f"来源：{editorial.tutorial_url}")
    return "\n".join(lines)