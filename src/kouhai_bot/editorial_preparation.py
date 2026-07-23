"""Complete, side-effect-free preparation of one verified official editorial.

This module owns the whole remote/LLM action: discover candidate Codeforces
blogs, reject mismatched candidates, validate the source against the statement,
and translate the first verified match.  It never writes tutorial/cache status.
Callers commit only a ``READY`` result.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .config import get_config
from .editorial_content import (
    MIN_EDITORIAL_LEN,
    OfficialEditorial,
    editorial_from_bundle,
)
from .handlers.shared import translate_editorial_to_zh
from .problem_content import (
    format_problem_statement_for_llm,
    load_statement_json,
    statement_fingerprint,
    statement_images,
)

logger = logging.getLogger("kouhai-bot.editorial_preparation")


class EditorialPreparationStatus(str, Enum):
    READY = "ready"
    EXHAUSTIVE_NO_MATCH = "exhaustive_no_match"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class PreparedEditorial:
    bundle: dict[str, Any]
    editorial: OfficialEditorial
    translated_text: str
    statement_sha256: str


@dataclass(frozen=True)
class EditorialPreparationResult:
    status: EditorialPreparationStatus
    prepared: PreparedEditorial | None = None
    reason: str = ""
    rejected_initial_candidate: bool = False
    statement_sha256: str = ""


@dataclass(frozen=True)
class _TutorialSearch:
    outcome: str
    result: Any | None = None
    reason: str = ""


@dataclass(frozen=True)
class _StatementContext:
    text: str
    images: list[dict]
    source_sha256: str


def _load_statement_context(
    pid: str,
    *,
    data_dir: str | Path,
) -> _StatementContext | None:
    statement = load_statement_json(pid, data_dir=data_dir)
    source_sha256 = statement_fingerprint(statement)
    if not source_sha256:
        return None
    return _StatementContext(
        text=format_problem_statement_for_llm(statement),
        images=statement_images(statement),
        source_sha256=source_sha256,
    )


def _tools_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "tools"


def _load_tutorial_agent() -> tuple[
    type[Exception],
    type[Exception],
    type[Exception],
    Any,
]:
    tools_dir = _tools_dir()
    if tools_dir.is_dir():
        tools_dir_str = str(tools_dir)
        if tools_dir_str not in sys.path:
            sys.path.insert(0, tools_dir_str)

    from cf_tutorial_agent import AgentIncomplete, AgentNoMatch, run_agent_for_pid
    from scrape_cf_tutorial import ScrapeError

    return AgentNoMatch, AgentIncomplete, ScrapeError, run_agent_for_pid


async def _search_tutorial(
    pid: str,
    *,
    data_dir: str | Path,
    excluded_tutorial_urls: frozenset[str],
) -> _TutorialSearch:
    statements_dir = Path(data_dir) / "statements"
    statement_path = statements_dir / f"{pid}.json"
    if not statement_path.is_file():
        return _TutorialSearch("incomplete", reason="statement_cache_missing")

    try:
        (
            AgentNoMatch,
            AgentIncomplete,
            ScrapeError,
            run_agent_for_pid,
        ) = _load_tutorial_agent()
    except Exception as exc:
        logger.warning(
            "tutorial agent unavailable for %s: %s",
            pid,
            exc,
            exc_info=True,
        )
        return _TutorialSearch("incomplete", reason=f"agent_unavailable:{exc}")

    try:
        result = await run_agent_for_pid(
            pid=pid,
            statements_dir=statements_dir,
            # A terminal "no match" marker is only valid after every linked
            # candidate has been examined. Timeouts remain retryable incomplete
            # work, so the outer lifecycle can safely try again later.
            blog_limit=0,
            excluded_tutorial_urls=excluded_tutorial_urls,
        )
    except AgentNoMatch as exc:
        return _TutorialSearch("no_match", reason=str(exc))
    except AgentIncomplete as exc:
        return _TutorialSearch("incomplete", reason=str(exc))
    except ScrapeError as exc:
        return _TutorialSearch("incomplete", reason=str(exc))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "tutorial agent failed for %s: %s",
            pid,
            exc,
            exc_info=True,
        )
        return _TutorialSearch("incomplete", reason=str(exc))
    return _TutorialSearch("candidate", result=result)


async def _validate_and_translate(
    pid: str,
    editorial: OfficialEditorial,
    *,
    statement: _StatementContext,
) -> tuple[str | None, bool | None]:
    translated, model_tag, matched = await translate_editorial_to_zh(
        editorial.text,
        pid=pid,
        problem_text=statement.text,
        images=statement.images,
    )
    if matched is False:
        return None, False
    if matched is not True or not translated:
        return None, None
    translated = translated.strip()
    if len(translated) < MIN_EDITORIAL_LEN:
        return None, None
    full_text = (translated + model_tag).strip() if model_tag else translated
    return full_text, True


async def prepare_editorial(
    pid: str,
    *,
    initial_bundle: dict[str, Any] | None = None,
    run_agent: bool = True,
    data_dir: str | Path | None = None,
) -> EditorialPreparationResult:
    """Return a verified editorial result without mutating persistent state."""
    pid = (pid or "").strip()
    if not pid:
        return EditorialPreparationResult(
            EditorialPreparationStatus.INCOMPLETE,
            reason="empty_pid",
        )
    resolved_data_dir = (
        Path(data_dir) if data_dir is not None else Path(get_config().data_dir)
    )
    statement = _load_statement_context(pid, data_dir=resolved_data_dir)
    if statement is None:
        return EditorialPreparationResult(
            EditorialPreparationStatus.INCOMPLETE,
            reason="statement_cache_missing",
        )

    rejected_urls: set[str] = set()
    rejected_initial = False
    if initial_bundle is not None:
        initial = editorial_from_bundle(initial_bundle)
        if initial is None:
            rejected_initial = True
        else:
            translated, matched = await _validate_and_translate(
                pid,
                initial,
                statement=statement,
            )
            if matched is True and translated:
                return EditorialPreparationResult(
                    EditorialPreparationStatus.READY,
                    prepared=PreparedEditorial(
                        bundle=initial_bundle,
                        editorial=initial,
                        translated_text=translated,
                        statement_sha256=statement.source_sha256,
                    ),
                    statement_sha256=statement.source_sha256,
                )
            if matched is None:
                return EditorialPreparationResult(
                    EditorialPreparationStatus.INCOMPLETE,
                    reason="initial_candidate_validation_incomplete",
                    statement_sha256=statement.source_sha256,
                )
            rejected_initial = True
            if initial.tutorial_url:
                rejected_urls.add(initial.tutorial_url)

    if not run_agent:
        return EditorialPreparationResult(
            EditorialPreparationStatus.INCOMPLETE,
            reason="agent_disabled",
            rejected_initial_candidate=rejected_initial,
            statement_sha256=statement.source_sha256,
        )

    while True:
        search = await _search_tutorial(
            pid,
            data_dir=resolved_data_dir,
            excluded_tutorial_urls=frozenset(rejected_urls),
        )
        if search.outcome == "incomplete":
            return EditorialPreparationResult(
                EditorialPreparationStatus.INCOMPLETE,
                reason=search.reason,
                rejected_initial_candidate=rejected_initial,
                statement_sha256=statement.source_sha256,
            )
        if search.outcome == "no_match":
            return EditorialPreparationResult(
                EditorialPreparationStatus.EXHAUSTIVE_NO_MATCH,
                reason=search.reason,
                rejected_initial_candidate=rejected_initial,
                statement_sha256=statement.source_sha256,
            )

        result = search.result
        bundle = getattr(result, "bundle", None)
        candidate = editorial_from_bundle(bundle)
        if candidate is None:
            return EditorialPreparationResult(
                EditorialPreparationStatus.INCOMPLETE,
                reason="agent_returned_unusable_candidate",
                rejected_initial_candidate=rejected_initial,
                statement_sha256=statement.source_sha256,
            )
        candidate_url = candidate.tutorial_url
        if not candidate_url or candidate_url in rejected_urls:
            return EditorialPreparationResult(
                EditorialPreparationStatus.INCOMPLETE,
                reason="agent_returned_non_unique_candidate",
                rejected_initial_candidate=rejected_initial,
                statement_sha256=statement.source_sha256,
            )

        translated, matched = await _validate_and_translate(
            pid,
            candidate,
            statement=statement,
        )
        if matched is None:
            return EditorialPreparationResult(
                EditorialPreparationStatus.INCOMPLETE,
                reason="candidate_validation_incomplete",
                rejected_initial_candidate=rejected_initial,
                statement_sha256=statement.source_sha256,
            )
        if matched is False:
            rejected_urls.add(candidate_url)
            continue

        return EditorialPreparationResult(
            EditorialPreparationStatus.READY,
            prepared=PreparedEditorial(
                bundle=bundle,
                editorial=candidate,
                translated_text=translated or "",
                statement_sha256=statement.source_sha256,
            ),
            rejected_initial_candidate=rejected_initial,
            statement_sha256=statement.source_sha256,
        )
