"""Complete, side-effect-free preparation of an audited problem summary."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

from .problem_content import statement_fingerprint, statement_images

logger = logging.getLogger("kouhai-bot.problem_summary")

SummaryGenerator = Callable[
    [str, str, str, list[dict]],
    Awaitable[tuple[str | None, str]],
]


class SummaryPreparationStatus(str, Enum):
    READY = "ready"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class PreparedSummary:
    """A summary already accepted by the generator's semantic audit."""

    text: str
    model_tag: str
    source_sha256: str


@dataclass(frozen=True)
class SummaryPreparationResult:
    status: SummaryPreparationStatus
    prepared: PreparedSummary | None = None
    reason: str = ""


async def prepare_problem_summary(
    statement: dict,
    *,
    generate: SummaryGenerator,
    attempts: int,
) -> SummaryPreparationResult:
    """Run the complete summary action without writing cache or publication state."""
    source_sha256 = statement_fingerprint(statement)
    if not source_sha256:
        return SummaryPreparationResult(
            SummaryPreparationStatus.INCOMPLETE,
            reason="statement_missing",
        )

    statement_text = str(statement.get("description", "") or "")
    input_text = str(statement.get("input", "") or "")
    limits_text = (
        f"Time: {statement.get('time_limit', '?')}, "
        f"Memory: {statement.get('memory_limit', '?')}"
    )
    images = statement_images(statement)
    attempt_count = max(1, int(attempts))
    last_reason = "summary_unavailable"
    for attempt in range(attempt_count):
        try:
            summary, model_tag = await generate(
                statement_text,
                input_text,
                limits_text,
                images,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_reason = f"summary_error:{type(exc).__name__}"
            logger.warning(
                "summary preparation attempt %s/%s failed: %s",
                attempt + 1,
                attempt_count,
                exc,
                exc_info=True,
            )
            continue
        text = (summary or "").strip()
        if text:
            return SummaryPreparationResult(
                SummaryPreparationStatus.READY,
                prepared=PreparedSummary(
                    text=text,
                    model_tag=(model_tag or "").strip(),
                    source_sha256=source_sha256,
                ),
            )
        last_reason = "summary_generator_returned_no_verified_result"

    return SummaryPreparationResult(
        SummaryPreparationStatus.INCOMPLETE,
        reason=last_reason,
    )
