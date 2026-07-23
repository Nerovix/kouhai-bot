"""Shared problem-cache loading and user-visible problem-card content helpers."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from .config import get_config
from .llm import strip_leaked_thinking
from .problems.content import normalize_sample_block

logger = logging.getLogger("kouhai-bot.problem_content")

LEGACY_STATEMENTS_DIR = Path.home() / ".kouhai-bot" / "statements"


def statement_fingerprint(statement: object) -> str:
    """Return a stable identity for the user-visible statement payload."""
    if not isinstance(statement, dict) or not statement:
        return ""
    semantic_statement = {
        key: value
        for key, value in statement.items()
        if not str(key).startswith("_")
    }
    if not semantic_statement:
        return ""
    payload = json.dumps(
        semantic_statement,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def statement_images(statement: object) -> list[dict]:
    if not isinstance(statement, dict):
        return []
    images = statement.get("images", [])
    return [
        item
        for item in images
        if isinstance(item, dict) and item.get("src")
    ]


def format_problem_statement_for_llm(statement: object) -> str:
    """Format one cached statement as stable LLM grounding text."""
    if not isinstance(statement, dict):
        return ""

    parts: list[str] = []
    if statement.get("name"):
        parts.append(f"Problem: {statement['name']}")
    if statement.get("time_limit"):
        parts.append(f"Time limit: {statement['time_limit']}")
    if statement.get("memory_limit"):
        parts.append(f"Memory limit: {statement['memory_limit']}")

    for label, key in (
        ("Description", "description"),
        ("Input", "input"),
        ("Output", "output"),
    ):
        value = statement.get(key, "")
        if value:
            parts.append(f"\n{label}:\n{value}")

    samples = statement.get("samples", [])
    if isinstance(samples, list):
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            parts.append(
                f"\nInput:\n{sample.get('input', '')}\n"
                f"Output:\n{sample.get('output', '')}"
            )

    notes = statement.get("notes", "")
    if notes:
        parts.append(f"\nNote:\n{notes}")
    return "\n".join(parts)


def load_statement_json(
    pid: str,
    *,
    data_dir: str | Path | None = None,
    include_legacy_fallback: bool = False,
    log_context: str = "",
) -> dict[str, Any]:
    """Load one cached statement from the configured data directory."""
    pid = (pid or "").strip()
    if not pid:
        return {}
    root = Path(data_dir) if data_dir is not None else Path(get_config().data_dir)
    paths = [root / "statements" / f"{pid}.json"]
    if include_legacy_fallback and LEGACY_STATEMENTS_DIR not in {
        path.parent for path in paths
    }:
        paths.append(LEGACY_STATEMENTS_DIR / f"{pid}.json")

    for path in paths:
        if not path.is_file():
            continue
        try:
            with path.open(encoding="utf-8") as f:
                statement = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "%sfailed to load statement %s: %s",
                f"{log_context}: " if log_context else "",
                path,
                exc,
            )
            continue
        if isinstance(statement, dict):
            return statement
        logger.warning(
            "%sstatement at %s is not an object",
            f"{log_context}: " if log_context else "",
            path,
        )
    return {}


def build_sample_messages(
    statement: dict[str, Any],
    *,
    include_explicit_empty: bool = False,
) -> tuple[str, ...]:
    """Build the canonical one-message-per-sample QQ payload."""
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
        normalized_input = normalize_sample_block(sample_input).rstrip("\n")
        normalized_output = normalize_sample_block(sample_output).rstrip("\n")
        if (
            not include_explicit_empty
            and not normalized_input
            and not normalized_output
        ):
            continue
        messages.append(
            f"样例 {idx}\n"
            f"Input:\n{normalized_input}\n\n"
            f"Output:\n{normalized_output}"
        )
    return tuple(messages)


async def build_notes_message(
    statement: dict[str, Any],
    *,
    translate_notes: Callable[
        [str, list[dict]],
        Awaitable[tuple[str | None, str]],
    ],
    images: list[dict],
    on_translate_exception: Literal["skip", "source"],
    log_context: str = "",
) -> str:
    """Build the canonical Notes node while preserving caller fallback policy."""
    normalized_notes = normalize_sample_block(statement.get("notes"))
    if not normalized_notes:
        return ""
    try:
        translated_notes, _model_tag = await translate_notes(
            normalized_notes,
            images,
        )
    except Exception as exc:
        if on_translate_exception == "skip":
            logger.warning(
                "%sNotes translation failed, skipping notes node: %s",
                f"{log_context}: " if log_context else "",
                exc,
            )
            return ""
        translated_notes = None

    final_notes = strip_leaked_thinking(translated_notes or normalized_notes)
    return f"样例解释：\n{final_notes}" if final_notes else ""
