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
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import get_config
from .editorial_followup import schedule_prefetch_editorial
from .handlers.shared import (
    statement_images,
    summarize_problem,
    translate_sample_notes,
)
from .llm import strip_leaked_thinking
from .problems.picker import _normalize_sample_block

logger = logging.getLogger("kouhai-bot.problem_preparation")

PICKER_PATH = Path(__file__).resolve().parent / "problems" / "picker.py"
STATEMENTS_FALLBACK_DIR = Path.home() / ".kouhai-bot" / "statements"
PICK_ATTEMPTS = 3
PICK_TIMEOUT_SEC = 120


@dataclass(frozen=True)
class PreparedProblem:
    """A problem whose blocking group-card preparation is complete."""

    state: dict[str, Any]
    summary: str
    model_tag: str
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
            "model_tag": self.model_tag,
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
            prepared = cls(
                state=dict(state),
                summary=str(value.get("summary", "") or ""),
                model_tag=str(value.get("model_tag", "") or ""),
                sample_messages=tuple(samples),
                notes_message=str(value.get("notes_message", "") or ""),
                min_rating=int(value["min_rating"]),
                max_rating=int(value["max_rating"]),
                prepared_at=int(value["prepared_at"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        return prepared if prepared.pid else None


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


def load_statement_json(group_id: int, pid: str) -> dict[str, Any]:
    """Load the configured statement cache, with the legacy fallback."""
    cfg = get_config()
    candidate_paths = [
        Path(cfg.data_dir) / "statements" / f"{pid}.json",
        STATEMENTS_FALLBACK_DIR / f"{pid}.json",
    ]
    for path in candidate_paths:
        if not path.exists():
            continue
        try:
            with path.open(encoding="utf-8") as f:
                statement = json.load(f)
            if isinstance(statement, dict):
                return statement
            logger.warning("[group_%s] Statement at %s is not a dict", group_id, path)
        except Exception as exc:
            logger.warning(
                "[group_%s] Failed to load statement %s: %s",
                group_id,
                path,
                exc,
            )
    return {}


def build_sample_messages(statement: dict[str, Any]) -> tuple[str, ...]:
    """Build the same one-message-per-sample payload used by /newproblem."""
    samples = statement.get("samples")
    if not isinstance(samples, list):
        return ()
    messages: list[str] = []
    for idx, sample in enumerate(samples, 1):
        if not isinstance(sample, dict):
            continue
        sample_input = sample.get("input")
        sample_output = sample.get("output")
        if sample_input is None and sample_output is None:
            continue
        normalized_input = _normalize_sample_block(sample_input).rstrip("\n")
        normalized_output = _normalize_sample_block(sample_output).rstrip("\n")
        messages.append(
            f"样例 {idx}\n"
            f"Input:\n{normalized_input}\n\n"
            f"Output:\n{normalized_output}"
        )
    return tuple(messages)


async def build_notes_message(statement: dict[str, Any]) -> str:
    """Translate Notes with the existing group-post fallback behavior."""
    normalized_notes = _normalize_sample_block(statement.get("notes"))
    if not normalized_notes:
        return ""
    try:
        translated_notes, _model_tag = await translate_sample_notes(
            normalized_notes,
            statement_images(statement),
        )
    except Exception as exc:
        logger.warning("Notes translation failed, skipping notes node: %s", exc)
        return ""
    final_notes = strip_leaked_thinking(translated_notes or normalized_notes)
    return f"样例解释：\n{final_notes}" if final_notes else ""


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

    summary = ""
    model_tag = ""
    sample_messages: tuple[str, ...] = ()
    notes_message = ""
    try:
        if pid:
            schedule_prefetch_editorial(pid)

        statement = load_statement_json(group_id, pid) if pid else {}
        statement_text = ""
        input_text = ""
        limits_text = ""
        if statement:
            statement_text = str(statement.get("description", "") or "")
            input_text = str(statement.get("input", "") or "")
            limits_text = (
                f"Time: {statement.get('time_limit', '?')}, "
                f"Memory: {statement.get('memory_limit', '?')}"
            )
            sample_messages = build_sample_messages(statement)
            notes_message = await build_notes_message(statement)

        images = statement_images(statement)
        generated, model_tag = await summarize_problem(
            statement_text,
            input_text,
            limits_text,
            images,
        )
        if not generated:
            logger.warning("[group_%s] Summary 1st attempt failed, retrying...", group_id)
            generated, model_tag = await summarize_problem(
                statement_text,
                input_text,
                limits_text,
                images,
            )
        if generated:
            summary = generated.strip()
        else:
            logger.warning("[group_%s] Summary failed after retry", group_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("[group_%s] Summary error: %s", group_id, exc)

    return PreparedProblem(
        state=dict(picked_state),
        summary=summary,
        model_tag=model_tag,
        sample_messages=sample_messages,
        notes_message=notes_message,
        min_rating=min_rating,
        max_rating=max_rating,
        prepared_at=int(time.time()),
    )


def format_previous_problem_reveal(problem: object) -> str:
    """Format the existing reveal message without spawning the picker CLI."""
    if not isinstance(problem, dict) or not problem:
        return "还没有发过题哦"
    parts = [f"CF{problem.get('today', '?')}"]
    name = str(problem.get("name", "") or "")
    rating = str(problem.get("rating", "") or "")
    if name:
        parts.append(name)
    if rating:
        parts.append(rating)
    return f"上一道题来自 {' '.join(parts)}✨"
