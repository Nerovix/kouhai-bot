"""Prepare the complete user-visible content for the next group problem.

The picker remains a subprocess boundary on purpose: it contains synchronous
Codeforces/Playwright work and process-global path/rating configuration. This
module gives callers a typed async interface without leaking those CLI details
into command handlers or the prefetch coordinator.

Identity is openid-native and multi-group. Selection exclusion is DB-driven
(used ∪ posted ∪ solved in this group) and passed to the picker via an exclude
file; the picker still writes a mirrored disk statement cache (also consumed by
the editorial crawler). Preparation upserts the DB problem catalog and marks
the pick used; publication owns group_posts + the verified summary.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import get_config
from .db.database import get_database
from .db.repos import posts, problems, users
from .handlers.shared import (
    summarize_problem,
    translate_figure_passage,
    translate_sample_notes,
)
from .problem_content import (
    build_notes_message as build_shared_notes_message,
    build_sample_messages,
    load_statement_json,
    statement_figure_passage,
    statement_figures,
    statement_fingerprint,
    statement_images,
)
from .render.figures import figure_data_uris
from .problem_summary import (
    SummaryPreparationStatus,
    prepare_problem_summary,
)

logger = logging.getLogger("kouhai-bot.problem_preparation")

PICKER_PATH = Path(__file__).resolve().parent / "problems" / "picker.py"
PICK_ATTEMPTS = 3
PICK_TIMEOUT_SEC = 120


@dataclass(frozen=True)
class PreparedProblem:
    """A problem whose blocking group-card preparation is complete."""

    state: dict[str, Any]
    summary: str
    summary_status: SummaryPreparationStatus
    model_tag: str
    statement_sha256: str
    sample_messages: tuple[str, ...]
    notes_message: str
    min_rating: int
    max_rating: int
    prepared_at: int
    figure_srcs: tuple[str, ...] = ()
    figure_captions: tuple[str, ...] = ()
    figure_passage: str = ""

    @property
    def pid(self) -> str:
        return str(self.state.get("today", "") or "")

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "summary": self.summary,
            "summary_status": self.summary_status.value,
            "model_tag": self.model_tag,
            "statement_sha256": self.statement_sha256,
            "sample_messages": list(self.sample_messages),
            "notes_message": self.notes_message,
            "min_rating": self.min_rating,
            "max_rating": self.max_rating,
            "prepared_at": self.prepared_at,
            "figure_srcs": list(self.figure_srcs),
            "figure_captions": list(self.figure_captions),
            "figure_passage": self.figure_passage,
        }

    @classmethod
    def from_json(cls, value: object) -> PreparedProblem | None:
        if not isinstance(value, dict):
            return None
        state = value.get("state")
        samples = value.get("sample_messages")
        if not isinstance(state, dict) or not isinstance(samples, list):
            return None
        if not all(isinstance(item, str) for item in samples):
            return None
        figure_srcs = value.get("figure_srcs") or []
        if not isinstance(figure_srcs, list) or not all(
            isinstance(item, str) for item in figure_srcs
        ):
            figure_srcs = []
        figure_captions = value.get("figure_captions") or []
        if not isinstance(figure_captions, list) or not all(
            isinstance(item, str) for item in figure_captions
        ):
            figure_captions = []
        try:
            summary = str(value.get("summary", "") or "")
            model_tag = str(value.get("model_tag", "") or "")
            summary_status = SummaryPreparationStatus(value["summary_status"])
            prepared = cls(
                state=dict(state),
                summary=summary,
                summary_status=summary_status,
                model_tag=model_tag,
                statement_sha256=str(value["statement_sha256"]),
                sample_messages=tuple(samples),
                notes_message=str(value.get("notes_message", "") or ""),
                min_rating=int(value["min_rating"]),
                max_rating=int(value["max_rating"]),
                prepared_at=int(value["prepared_at"]),
                figure_srcs=tuple(figure_srcs),
                figure_captions=tuple(figure_captions),
                figure_passage=str(value.get("figure_passage", "") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if not prepared.pid or len(prepared.statement_sha256) != 64:
            return None
        if summary_status is SummaryPreparationStatus.READY:
            return prepared if summary else None
        return prepared if not summary and not model_tag else None


class ProblemPreparationError(RuntimeError):
    """Terminal result of one three-attempt picker/preparation run."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class ProblemPoolExhausted(ProblemPreparationError):
    """Every candidate in range is excluded; caller may relax the used cycle."""

    def __init__(self) -> None:
        super().__init__("题库里暂时没有合适的未解题了，过段时间再来试试吧😢")


async def effective_rating_range(group_openid: str) -> tuple[int, int]:
    """Global default range with an optional per-group /range override."""
    cfg = get_config()
    min_rating = int(cfg.min_rating)
    max_rating = int(cfg.max_rating)
    try:
        override_min, override_max = await users.get_group_rating(get_database(), group_openid)
        if override_min is not None:
            min_rating = int(override_min)
        if override_max is not None:
            max_rating = int(override_max)
    except Exception:
        logger.warning(
            "[group_%s] Failed to load group rating override; using config range",
            group_openid, exc_info=True,
        )
    return min_rating, max_rating


def picker_args(
    command: str,
    group_openid: str,
    *extra: str,
    rating_range: tuple[int, int] | None = None,
    exclude_file: str | Path | None = None,
) -> list[str]:
    min_rating, max_rating = rating_range if rating_range is not None else (
        int(get_config().min_rating), int(get_config().max_rating)
    )
    args = [
        str(PICKER_PATH),
        command,
        "--group",
        str(group_openid),
        "--data-dir",
        str(get_config().data_dir),
        "--min-rating",
        str(min_rating),
        "--max-rating",
        str(max_rating),
    ]
    if exclude_file is not None:
        args += ["--exclude-file", str(exclude_file)]
    args += list(extra)
    return args


def classify_pick_error(stderr_text: str) -> str:
    """Classify picker stderr into the existing user-facing error message."""
    lower = stderr_text.lower()
    if "no unsolved problems found" in lower:
        return "EXHAUSTED"
    if any(kw in stderr_text for kw in (
        "codeforces.com", "SSL", "SSLEOFError", "SSLError",
        "ConnectionError", "Max retries exceeded", "RemoteDisconnected",
    )):
        return "Codeforces 连接失败"
    if any(kw in lower for kw in ("timeout", "timed out")):
        return "Codeforces 请求超时"
    if any(kw in lower for kw in (
        "permission", "access denied", "ioerror", "filenotfound",
    )):
        return "本地数据读取异常"
    return "题目选取失败，稍后再试"


async def build_notes_message(statement: dict[str, Any]) -> str:
    """Translate Notes with the existing group-post fallback behavior."""
    return await build_shared_notes_message(
        statement,
        translate_notes=translate_sample_notes,
        images=statement_images(statement),
        on_translate_exception="skip",
        log_context="group problem preparation",
    )


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.kill()
    except ProcessLookupError:
        pass
    try:
        await process.wait()
    except ProcessLookupError:
        pass


def _exclude_file_path(group_openid: str) -> Path:
    return Path(get_config().data_dir) / "groups" / str(group_openid) / "pick_exclude.json"


def _write_exclude_file(group_openid: str, excluded: set[str]) -> Path:
    path = _exclude_file_path(group_openid)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(sorted(excluded), f)
    return path


async def _run_picker(
    group_openid: str,
    *,
    rating_range: tuple[int, int],
    excluded: set[str],
) -> dict[str, Any]:
    exclude_file = _write_exclude_file(group_openid, excluded)
    pick_error_msg = ""
    for attempt in range(1, PICK_ATTEMPTS + 1):
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                *picker_args(
                    "pick-json",
                    group_openid,
                    "--with-statement",
                    rating_range=rating_range,
                    exclude_file=exclude_file,
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=PICK_TIMEOUT_SEC,
                )
            except BaseException:
                await _stop_process(process)
                raise
            if process.returncode != 0:
                err_full = stderr.decode(errors="replace")
                logger.error(
                    "Pick failed (group %s, attempt %s/%s): %s",
                    group_openid, attempt, PICK_ATTEMPTS, err_full[:200],
                )
                # Classify on the FULL stderr: the picker prints its terminal
                # "No unsolved problems found" marker last, after per-fetch
                # warnings and retry lines, so a head truncation misses it and
                # the pool-exhaustion relaxation would never fire.
                pick_error_msg = classify_pick_error(err_full)
                if pick_error_msg == "EXHAUSTED":
                    raise ProblemPoolExhausted()
            else:
                picked_state = json.loads(stdout.decode())
                if (
                    isinstance(picked_state, dict)
                    and str(picked_state.get("today", "") or "").strip()
                ):
                    return picked_state
                logger.error(
                    "Pick failed (group %s, attempt %s/%s): invalid picker payload",
                    group_openid, attempt, PICK_ATTEMPTS,
                )
                pick_error_msg = "题目选取结果异常"
        except asyncio.CancelledError:
            raise
        except ProblemPoolExhausted:
            raise
        except Exception as exc:
            logger.error(
                "Pick error (group %s, attempt %s/%s): %s",
                group_openid, attempt, PICK_ATTEMPTS, exc,
            )
            pick_error_msg = "Codeforces 连接失败"

        if attempt < PICK_ATTEMPTS:
            delay = 2 * attempt
            logger.info("Retrying pick in %ss...", delay)
            await asyncio.sleep(delay)

    raise ProblemPreparationError(pick_error_msg or "题目选取失败，稍后再试")


async def _posted_or_solved_pids(group_openid: str) -> set[str]:
    rows = await get_database().fetch_all(
        "SELECT pid FROM group_posts WHERE group_openid=? "
        "UNION SELECT pid FROM group_solves WHERE group_openid=?",
        (group_openid, group_openid),
    )
    return {r["pid"] for r in rows}


def _rating_value(value: object) -> int | None:
    if value in (None, "", "?"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


async def _no_passage() -> tuple[str | None, str]:
    return None, ""


async def prepare_problem(group_openid: str) -> PreparedProblem:
    """Run the blocking group-card preparation pipeline ahead of time."""
    db = get_database()
    min_rating, max_rating = await effective_rating_range(group_openid)
    rating_range = (min_rating, max_rating)

    excluded = await posts.get_excluded_pids(db, group_openid)
    try:
        picked_state = await _run_picker(
            group_openid, rating_range=rating_range, excluded=excluded,
        )
    except ProblemPoolExhausted:
        # Relax the used cycle (keep posted/solved excluded), mirroring the
        # legacy picker reset when every in-range problem has been used.
        reduced = await _posted_or_solved_pids(group_openid)
        picked_state = await _run_picker(
            group_openid, rating_range=rating_range, excluded=reduced,
        )

    pid = str(picked_state.get("today", "") or "")
    statement = (
        load_statement_json(
            pid,
            data_dir=get_config().data_dir,
            include_legacy_fallback=True,
            log_context=f"group_{group_openid}",
        )
        if pid
        else {}
    )
    source_sha256 = statement_fingerprint(statement)
    if not source_sha256:
        raise ProblemPreparationError("题面缓存读取失败")

    await problems.upsert_problem(
        db, pid,
        contest_id=int(picked_state["contestId"]) if picked_state.get("contestId") else None,
        idx=str(picked_state.get("index", "") or ""),
        name=str(picked_state.get("name", "") or ""),
        rating=_rating_value(picked_state.get("rating")),
        tags=list(picked_state.get("tags", []) or []),
        statement_sha256=source_sha256,
        statement_json=json.dumps(statement, ensure_ascii=False),
    )
    await posts.mark_used(db, group_openid, pid)

    sample_messages = build_sample_messages(statement, include_explicit_empty=True)
    notes_message = await build_notes_message(statement)
    figures = statement_figures(statement)
    figure_srcs = [figure["src"] for figure in figures]
    figure_captions = [figure["caption"] for figure in figures]
    source_passage = statement_figure_passage(statement)
    if figure_srcs:
        # Warm the on-disk figure cache during background preparation so the
        # publication render embeds data URIs without waiting on the network.
        await figure_data_uris(figure_srcs)
    summary_result, passage_result = await asyncio.gather(
        prepare_problem_summary(statement, generate=summarize_problem, attempts=2),
        translate_figure_passage(source_passage) if source_passage else _no_passage(),
    )
    figure_passage = source_passage
    if passage_result[0]:
        figure_passage = passage_result[0]
    else:
        logger.warning(
            "[group_%s] Figure passage translation unavailable; using source text",
            group_openid,
        )
    if (
        summary_result.status is SummaryPreparationStatus.READY
        and summary_result.prepared is not None
    ):
        summary = summary_result.prepared.text
        model_tag = summary_result.prepared.model_tag
    else:
        summary = ""
        model_tag = ""
        logger.warning(
            "[group_%s] Summary unavailable after retries: %s",
            group_openid, summary_result.reason,
        )

    return PreparedProblem(
        state=dict(picked_state),
        summary=summary,
        summary_status=summary_result.status,
        model_tag=model_tag,
        statement_sha256=source_sha256,
        sample_messages=sample_messages,
        notes_message=notes_message,
        min_rating=min_rating,
        max_rating=max_rating,
        prepared_at=int(time.time()),
        figure_srcs=tuple(figure_srcs),
        figure_captions=tuple(figure_captions),
        figure_passage=figure_passage,
    )