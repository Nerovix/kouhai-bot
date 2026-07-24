"""Load and extract Codeforces official editorials from scraped tutorial JSON.

Extraction rules align with tools/scrape_cf_tutorial.py normalization (hint/solution/raw_text).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

from .config import get_config
from .editorial_content import (
    MIN_EDITORIAL_LEN,
    OfficialEditorial,
    bundle_for_editorial,
    editorial_from_bundle,
    extract_editorial,
    is_placeholder as _is_placeholder,
)
from .editorial_preparation import (
    EditorialPreparationStatus,
    PreparedEditorial,
    _load_tutorial_agent,
    prepare_editorial,
)
from .llm import strip_leaked_thinking
from .problem_content import load_statement_json, statement_fingerprint

NO_EDITORIAL_MARKER_VERSION = 3
VERIFIED_EDITORIAL_MARKER_VERSION = 2
_REVIEW_EDITORIAL_MAX_LEN = 12000

logger = logging.getLogger("kouhai-bot.tutorials")


def load_tutorial(pid: str) -> dict | None:
    path = os.path.join(get_config().data_dir, "tutorials", f"{pid}.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_tutorial_bundle(pid: str, bundle: dict) -> None:
    tutorials_dir = Path(get_config().data_dir) / "tutorials"
    tutorials_dir.mkdir(parents=True, exist_ok=True)
    out_path = tutorials_dir / f"{pid}.json"
    tmp_path = out_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, out_path)


def _remove_tutorial_bundle(pid: str) -> None:
    path = Path(get_config().data_dir) / "tutorials" / f"{pid}.json"
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("failed to remove rejected tutorial JSON for %s: %s", pid, exc)


async def ensure_tutorial_json(pid: str) -> bool:
    """Compatibility entry point: prepare and persist only a verified result."""
    await prefetch_editorial_zh(pid, run_agent=True)
    return has_cached_editorial_zh(pid)


def get_official_editorial(pid: str) -> OfficialEditorial | None:
    return editorial_from_bundle(load_tutorial(pid))


def _translation_cache_dir() -> str:
    cache_dir = os.path.join(get_config().data_dir, "tutorial_translations")
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def _translation_cache_path(pid: str) -> str:
    return os.path.join(_translation_cache_dir(), f"{pid}.txt")


def _no_editorial_marker_path(pid: str) -> str:
    return os.path.join(_translation_cache_dir(), f"{pid}.no_editorial")


def _verified_editorial_marker_path(pid: str) -> str:
    return os.path.join(_translation_cache_dir(), f"{pid}.verified")


def _editorial_fingerprint(editorial: OfficialEditorial) -> str:
    payload = "\0".join(
        [editorial.tutorial_url, editorial.tutorial_title, editorial.text]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _current_statement_fingerprint(pid: str) -> str:
    statement = load_statement_json(pid, data_dir=get_config().data_dir)
    return statement_fingerprint(statement)


def _load_cached_translation(pid: str) -> str:
    path = _translation_cache_path(pid)
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return strip_leaked_thinking(f.read().strip())
    except OSError:
        return ""


def _save_cached_translation(
    pid: str,
    text: str,
    editorial: OfficialEditorial,
    *,
    statement_sha256: str = "",
) -> bool:
    current_statement_sha256 = _current_statement_fingerprint(pid)
    if (
        not current_statement_sha256
        or (
            statement_sha256
            and statement_sha256 != current_statement_sha256
        )
    ):
        logger.warning(
            "refusing to mark editorial translation verified for %s: "
            "statement source does not match",
            pid,
        )
        return False
    persisted = get_official_editorial(pid)
    if (
        persisted is None
        or _editorial_fingerprint(persisted) != _editorial_fingerprint(editorial)
    ):
        logger.warning(
            "refusing to mark editorial translation verified for %s: "
            "persisted source does not match",
            pid,
        )
        return False
    text = strip_leaked_thinking(text).strip()
    if len(text) < MIN_EDITORIAL_LEN:
        logger.warning(
            "refusing to mark editorial translation verified for %s: "
            "translated content is incomplete",
            pid,
        )
        return False
    path = _translation_cache_path(pid)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    verified = _verified_editorial_marker_path(pid)
    with open(verified, "w", encoding="utf-8") as f:
        json.dump(
            {
                "format_version": VERIFIED_EDITORIAL_MARKER_VERSION,
                "status": "verified",
                "source_sha256": _editorial_fingerprint(editorial),
                "statement_sha256": current_statement_sha256,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    marker = _no_editorial_marker_path(pid)
    if os.path.isfile(marker):
        os.remove(marker)
    return True


def is_no_official_editorial(pid: str) -> bool:
    marker = _no_editorial_marker_path(pid)
    if not os.path.isfile(marker):
        return False
    try:
        with open(marker, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        # Legacy zero-byte markers may represent transient failures from the
        # old boolean pipeline, so they are deliberately not trusted.
        return False
    return (
        isinstance(payload, dict)
        and payload.get("format_version") == NO_EDITORIAL_MARKER_VERSION
        and payload.get("status") == "no_editorial"
        and payload.get("search_complete") is True
        and payload.get("statement_sha256") == _current_statement_fingerprint(pid)
        and bool(str(payload.get("reason", "") or "").strip())
    )


def mark_no_official_editorial(
    pid: str,
    *,
    reason: str,
    statement_sha256: str = "",
) -> None:
    """Persist only a completed, exhaustive no-editorial result."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError(f"cannot mark no editorial for {pid}: reason missing")
    current_statement_sha256 = _current_statement_fingerprint(pid)
    if not current_statement_sha256:
        raise ValueError(f"cannot mark no editorial for {pid}: statement missing")
    if statement_sha256 and statement_sha256 != current_statement_sha256:
        raise ValueError(f"cannot mark no editorial for stale statement {pid}")
    marker = _no_editorial_marker_path(pid)
    with open(marker, "w", encoding="utf-8") as f:
        json.dump(
            {
                "format_version": NO_EDITORIAL_MARKER_VERSION,
                "status": "no_editorial",
                "search_complete": True,
                "reason": reason,
                "statement_sha256": current_statement_sha256,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    path = _translation_cache_path(pid)
    if os.path.isfile(path):
        os.remove(path)
    verified = _verified_editorial_marker_path(pid)
    if os.path.isfile(verified):
        os.remove(verified)
    _remove_tutorial_bundle(pid)


def clear_no_official_editorial_marker(pid: str) -> None:
    marker = _no_editorial_marker_path(pid)
    if os.path.isfile(marker):
        os.remove(marker)


def has_cached_editorial_zh(pid: str) -> bool:
    if is_no_official_editorial(pid):
        return False
    translated = _load_cached_translation(pid)
    if len(translated) < MIN_EDITORIAL_LEN:
        return False
    editorial = get_official_editorial(pid)
    if editorial is None:
        return False
    try:
        with open(_verified_editorial_marker_path(pid), encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("format_version") == VERIFIED_EDITORIAL_MARKER_VERSION
        and payload.get("status") == "verified"
        and payload.get("source_sha256") == _editorial_fingerprint(editorial)
        and payload.get("statement_sha256") == _current_statement_fingerprint(pid)
    )


def load_cached_editorial_zh(pid: str) -> str:
    return _load_cached_translation(pid)


def get_verified_official_editorial(pid: str) -> OfficialEditorial | None:
    if not has_cached_editorial_zh(pid):
        return None
    return get_official_editorial(pid)


def _commit_prepared_editorial(pid: str, prepared: PreparedEditorial) -> bool:
    """Publish prepared assets, writing the verified marker strictly last."""
    if _current_statement_fingerprint(pid) != prepared.statement_sha256:
        logger.warning(
            "refusing to persist editorial for %s: statement changed during preparation",
            pid,
        )
        return False
    try:
        _write_tutorial_bundle(pid, prepared.bundle)
        return _save_cached_translation(
            pid,
            prepared.translated_text,
            prepared.editorial,
            statement_sha256=prepared.statement_sha256,
        )
    except OSError as exc:
        logger.warning(
            "failed to persist verified editorial for %s: %s",
            pid,
            exc,
            exc_info=True,
        )
        return False


async def prefetch_editorial_zh(pid: str, *, run_agent: bool = True) -> None:
    """Prepare and commit a verified editorial ahead of first AC."""
    pid = (pid or "").strip()
    if not pid or has_cached_editorial_zh(pid) or is_no_official_editorial(pid):
        return

    initial_bundle = load_tutorial(pid)
    result = await prepare_editorial(
        pid,
        initial_bundle=initial_bundle,
        run_agent=run_agent,
        data_dir=get_config().data_dir,
    )
    if (
        result.status is EditorialPreparationStatus.READY
        and result.prepared is not None
    ):
        if _commit_prepared_editorial(pid, result.prepared):
            logger.info(
                "editorial prefetch verified candidate for %s url=%s",
                pid,
                result.prepared.editorial.tutorial_url or "(missing)",
            )
        return

    if result.status is EditorialPreparationStatus.EXHAUSTIVE_NO_MATCH:
        mark_no_official_editorial(
            pid,
            reason=f"agent_exhaustive_no_match:{result.reason}",
            statement_sha256=result.statement_sha256,
        )
        return

    if result.rejected_initial_candidate:
        _remove_tutorial_bundle(pid)
        logger.info(
            "removed rejected legacy editorial candidate for %s",
            pid,
        )
    logger.info(
        "editorial prefetch remains incomplete for %s: %s",
        pid,
        result.reason,
    )


async def get_editorial_zh_for_group(
    editorial: OfficialEditorial,
    pid: str,
) -> tuple[str | None, str]:
    """Compatibility entry point for validating one supplied editorial.

    Returns (translated_text, model_tag). model_tag is empty for cache hits.
    """
    if has_cached_editorial_zh(pid):
        return _load_cached_translation(pid), ""

    persisted_bundle = load_tutorial(pid)
    persisted_editorial = editorial_from_bundle(persisted_bundle)
    if (
        persisted_editorial is not None
        and _editorial_fingerprint(persisted_editorial)
        == _editorial_fingerprint(editorial)
    ):
        initial_bundle = persisted_bundle
    else:
        initial_bundle = bundle_for_editorial(editorial)
    result = await prepare_editorial(
        pid,
        initial_bundle=initial_bundle,
        run_agent=False,
        data_dir=get_config().data_dir,
    )
    if (
        result.status is not EditorialPreparationStatus.READY
        or result.prepared is None
        or not _commit_prepared_editorial(pid, result.prepared)
    ):
        return None, ""
    return result.prepared.translated_text, ""


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
