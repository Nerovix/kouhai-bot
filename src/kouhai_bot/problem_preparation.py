"""Prepare the complete user-visible content for the next group problem.

The picker remains a subprocess boundary on purpose: it contains synchronous
Codeforces/Playwright work and process-global path/rating configuration.  This
module gives callers a typed async interface without leaking those CLI details
into command handlers or the prefetch coordinator.
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
from .handlers.shared import (
    summarize_problem,
    translate_sample_notes,
)
from .problem_content import (
    build_notes_message as build_shared_notes_message,
    build_sample_messages,
    load_statement_json,
    statement_fingerprint,
    statement_images,
)
from .problem_summary import (
    SummaryPreparationStatus,
    prepare_problem_summary,
)
from .problems.picker import format_reveal as format_previous_problem_reveal

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


def effective_rating_range(group_id: int) -> tuple[int, int]:
    """Return config ratings with the existing scheduler override semantics."""
    cfg = get_config()
    min_rating = int(cfg.min_rating)
    max_rating = int(cfg.max_rating)
    try:
        from .scheduler.engine import load_group_configs

        group_cfg = load_group_configs().get(group_id)
        if group_cfg:
            if group_cfg.min_rating is not None:
                min_rating = int(group_cfg.min_rating)
            if group_cfg.max_rating is not None:
                max_rating = int(group_cfg.max_rating)
    except Exception:
        logger.warning(
            "[group_%s] Failed to load scheduler rating overrides; using config range",
            group_id,
            exc_info=True,
        )
    return min_rating, max_rating


def picker_args(
    command: str,
    group_id: int,
    *extra: str,
    rating_range: tuple[int, int] | None = None,
) -> list[str]:
    min_rating, max_rating = (
        rating_range
        if rating_range is not None
        else effective_rating_range(group_id)
    )
    return [
        str(PICKER_PATH),
        command,
        "--group",
        str(group_id),
        "--data-dir",
        str(get_config().data_dir),
        "--min-rating",
        str(min_rating),
        "--max-rating",
        str(max_rating),
        *extra,
    ]


def classify_pick_error(stderr_text: str) -> str:
    """Classify picker stderr into the existing user-facing error message."""
    lower = stderr_text.lower()
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


async def _run_picker(
    group_id: int,
    *,
    rating_range: tuple[int, int] | None = None,
) -> dict[str, Any]:
    pick_error_msg = ""
    for attempt in range(1, PICK_ATTEMPTS + 1):
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                *picker_args(
                    "pick-json",
                    group_id,
                    "--with-statement",
                    rating_range=rating_range,
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
                err_text = stderr.decode(errors="replace")[:200]
                logger.error(
                    "Pick failed (group %s, attempt %s/%s): %s",
                    group_id,
                    attempt,
                    PICK_ATTEMPTS,
                    err_text,
                )
                pick_error_msg = classify_pick_error(err_text)
            else:
                picked_state = json.loads(stdout.decode())
                if (
                    isinstance(picked_state, dict)
                    and str(picked_state.get("today", "") or "").strip()
                ):
                    return picked_state
                logger.error(
                    "Pick failed (group %s, attempt %s/%s): invalid picker payload",
                    group_id,
                    attempt,
                    PICK_ATTEMPTS,
                )
                pick_error_msg = "题目选取结果异常"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "Pick error (group %s, attempt %s/%s): %s",
                group_id,
                attempt,
                PICK_ATTEMPTS,
                exc,
            )
            pick_error_msg = "Codeforces 连接失败"

        if attempt < PICK_ATTEMPTS:
            delay = 2 * attempt
            logger.info("Retrying pick in %ss...", delay)
            await asyncio.sleep(delay)

    raise ProblemPreparationError(pick_error_msg or "题目选取失败，稍后再试")


async def prepare_problem(group_id: int) -> PreparedProblem:
    """Run the old blocking /newproblem preparation pipeline ahead of time."""
    min_rating, max_rating = effective_rating_range(group_id)
    picked_state = await _run_picker(
        group_id,
        rating_range=(min_rating, max_rating),
    )
    pid = str(picked_state.get("today", "") or "")

    statement = (
        load_statement_json(
            pid,
            data_dir=get_config().data_dir,
            include_legacy_fallback=True,
            log_context=f"group_{group_id}",
        )
        if pid
        else {}
    )
    source_sha256 = statement_fingerprint(statement)
    if not source_sha256:
        raise ProblemPreparationError("题面缓存读取失败")

    sample_messages = build_sample_messages(
        statement,
        include_explicit_empty=True,
    )
    notes_message = await build_notes_message(statement)
    summary_result = await prepare_problem_summary(
        statement,
        generate=summarize_problem,
        attempts=2,
    )
    if (
        summary_result.status is SummaryPreparationStatus.READY
        and summary_result.prepared is not None
    ):
        summary = summary_result.prepared.text
        model_tag = summary_result.prepared.model_tag
    else:
        # Preserve the established availability policy: a fully fetched
        # statement can still be posted without an LLM summary.  The explicit
        # status prevents this degraded card from masquerading as a summary hit.
        summary = ""
        model_tag = ""
        logger.warning(
            "[group_%s] Summary unavailable after retries: %s",
            group_id,
            summary_result.reason,
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
    )
