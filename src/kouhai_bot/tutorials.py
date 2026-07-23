"""Load and extract Codeforces official editorials from scraped tutorial JSON.

Extraction rules align with tools/scrape_cf_tutorial.py normalization (hint/solution/raw_text).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import get_config
from .handlers.shared import translate_editorial_to_zh
from .llm import strip_leaked_thinking

MIN_EDITORIAL_LEN = 80
NO_EDITORIAL_MARKER_VERSION = 1
_REVIEW_EDITORIAL_MAX_LEN = 12000

logger = logging.getLogger("kouhai-bot.tutorials")

_PLACEHOLDER_RE = re.compile(
    r"Tutorial is loading|Will be added soon",
    re.I,
)
_SHORT_SOLUTION_RE = re.compile(r"^.+solution$", re.I | re.S)
_SECTION_MARKER_RE = re.compile(
    r"Hint(?:\s*\d+)?|Solution(?:\s+with[\s\S]*)?|Solution\s*\([^)]*\)"
    r"|Tutorial|Editorial|Code(?:\s*\([^)]+\))?",
    re.I,
)
_AUTHOR_LINE_RE = re.compile(r"^authors?\s*&\s*preparation", re.I)


@dataclass(frozen=True)
class OfficialEditorial:
    text: str
    tutorial_url: str
    tutorial_title: str


def _is_placeholder(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    if _PLACEHOLDER_RE.search(stripped):
        return True
    if len(stripped) < 60 and _SHORT_SOLUTION_RE.fullmatch(stripped):
        return True
    return False


def _is_section_marker(line: str) -> bool:
    token = line.strip().strip("*").strip()
    return bool(_SECTION_MARKER_RE.fullmatch(token))


def _clean_raw_text(raw_text: str) -> str:
    lines = raw_text.splitlines()
    out: list[str] = []
    for ln in lines:
        t = ln.strip()
        if not out and (_AUTHOR_LINE_RE.match(t) or t.lower() == "editorial"):
            continue
        if _is_section_marker(t):
            continue
        out.append(ln)
    return "\n".join(out).strip()


def _append_code_blocks(body: str, code_blocks: list[str]) -> str:
    codes = [c.strip() for c in code_blocks if c and c.strip()]
    if not codes:
        return body
    code_part = "\n\n".join(f"```\n{c}\n```" for c in codes)
    if body:
        return f"{body}\n\n{code_part}".strip()
    return code_part


def extract_editorial(section: dict) -> str:
    """Extract editorial body from one tutorial section dict."""
    hint = (section.get("hint") or "").strip()
    solution = (section.get("solution") or "").strip()
    raw_text = (section.get("raw_text") or "").strip()
    code_blocks = section.get("code_blocks") or []

    body = ""
    if solution and not _is_placeholder(solution):
        body = solution
    elif hint and not _is_placeholder(hint):
        body = hint
    elif raw_text:
        body = _clean_raw_text(raw_text)
        if _is_placeholder(body):
            body = ""

    return _append_code_blocks(body, code_blocks)


def _load_problem_statement_for_editorial(pid: str) -> str:
    path = os.path.join(get_config().data_dir, "statements", f"{pid}.json")
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            stmt = json.load(f)
    except (OSError, json.JSONDecodeError):
        return ""

    parts: list[str] = []
    if stmt.get("name"):
        parts.append(f"Problem: {stmt['name']}")
    if stmt.get("time_limit"):
        parts.append(f"Time limit: {stmt['time_limit']}")
    if stmt.get("memory_limit"):
        parts.append(f"Memory limit: {stmt['memory_limit']}")
    for label, key in [
        ("Description", "description"),
        ("Input", "input"),
        ("Output", "output"),
        ("Note", "notes"),
    ]:
        value = (stmt.get(key) or "").strip()
        if value:
            parts.append(f"\n{label}:\n{value}")
    samples = stmt.get("samples") or []
    if isinstance(samples, list):
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            parts.append(
                f"\nInput:\n{sample.get('input', '')}\n"
                f"Output:\n{sample.get('output', '')}"
            )
    return "\n".join(parts)


def _load_problem_images_for_editorial(pid: str) -> list[dict]:
    path = os.path.join(get_config().data_dir, "statements", f"{pid}.json")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            stmt = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    images = stmt.get("images", []) if isinstance(stmt, dict) else []
    return [item for item in images if isinstance(item, dict) and item.get("src")]


def load_tutorial(pid: str) -> dict | None:
    path = os.path.join(get_config().data_dir, "tutorials", f"{pid}.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


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


def _write_tutorial_bundle(pid: str, bundle: dict) -> None:
    tutorials_dir = Path(get_config().data_dir) / "tutorials"
    tutorials_dir.mkdir(parents=True, exist_ok=True)
    out_path = tutorials_dir / f"{pid}.json"
    tmp_path = out_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, out_path)


async def ensure_tutorial_json(pid: str) -> bool:
    """Run the CF tutorial agent if no usable tutorial JSON is cached."""
    pid = (pid or "").strip()
    if not pid:
        return False
    if get_official_editorial(pid) is not None:
        clear_no_official_editorial_marker(pid)
        return True

    cfg = get_config()
    statements_dir = Path(cfg.data_dir) / "statements"
    statement_path = statements_dir / f"{pid}.json"
    if not statement_path.is_file():
        logger.info("tutorial agent skipped for %s: statement cache missing", pid)
        return False

    try:
        (
            AgentNoMatch,
            AgentIncomplete,
            ScrapeError,
            run_agent_for_pid,
        ) = _load_tutorial_agent()
    except Exception as e:
        logger.warning("tutorial agent unavailable for %s: %s", pid, e, exc_info=True)
        return False

    try:
        result = await run_agent_for_pid(pid=pid, statements_dir=statements_dir)
    except AgentNoMatch as e:
        logger.info("tutorial agent found no official editorial for %s: %s", pid, e)
        mark_no_official_editorial(pid, reason=f"agent_no_match:{e}")
        return False
    except AgentIncomplete as e:
        logger.warning("tutorial agent did not finish reliably for %s: %s", pid, e)
        return False
    except ScrapeError as e:
        logger.warning("tutorial agent scrape failed for %s: %s", pid, e)
        return False
    except Exception as e:
        logger.warning("tutorial agent failed for %s: %s", pid, e, exc_info=True)
        return False

    try:
        _write_tutorial_bundle(pid, result.bundle)
    except OSError as e:
        logger.warning("failed to write tutorial JSON for %s: %s", pid, e, exc_info=True)
        return False

    if get_official_editorial(pid) is None:
        logger.warning("tutorial agent wrote unusable tutorial JSON for %s", pid)
        return False

    clear_no_official_editorial_marker(pid)
    logger.info(
        "tutorial agent cached official editorial for %s "
        "candidate=%s confidence=%.2f elapsed=%.1fs",
        pid,
        result.selected_candidate_id,
        result.confidence,
        result.elapsed_sec,
    )
    return True


def get_official_editorial(pid: str) -> OfficialEditorial | None:
    bundle = load_tutorial(pid)
    if not bundle:
        return None
    sections = bundle.get("sections") or []
    if not sections:
        return None
    text = extract_editorial(sections[0])
    if len(text) < MIN_EDITORIAL_LEN:
        return None
    return OfficialEditorial(
        text=text,
        tutorial_url=(bundle.get("tutorial_url") or "").strip(),
        tutorial_title=(bundle.get("tutorial_title") or "").strip(),
    )


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


def _load_cached_translation(pid: str) -> str:
    path = _translation_cache_path(pid)
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return strip_leaked_thinking(f.read().strip())
    except OSError:
        return ""


def _save_cached_translation(pid: str, text: str) -> None:
    text = strip_leaked_thinking(text)
    path = _translation_cache_path(pid)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    verified = _verified_editorial_marker_path(pid)
    with open(verified, "w", encoding="utf-8"):
        pass
    marker = _no_editorial_marker_path(pid)
    if os.path.isfile(marker):
        os.remove(marker)


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
        and bool(str(payload.get("reason", "") or "").strip())
    )


def mark_no_official_editorial(pid: str, *, reason: str = "confirmed_no_match") -> None:
    marker = _no_editorial_marker_path(pid)
    with open(marker, "w", encoding="utf-8") as f:
        json.dump(
            {
                "format_version": NO_EDITORIAL_MARKER_VERSION,
                "status": "no_editorial",
                "reason": reason,
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


def clear_no_official_editorial_marker(pid: str) -> None:
    marker = _no_editorial_marker_path(pid)
    if os.path.isfile(marker):
        os.remove(marker)


def has_cached_editorial_zh(pid: str) -> bool:
    if not os.path.isfile(_verified_editorial_marker_path(pid)):
        return False
    return len(_load_cached_translation(pid)) >= MIN_EDITORIAL_LEN


def load_cached_editorial_zh(pid: str) -> str:
    return _load_cached_translation(pid)


async def prefetch_editorial_zh(pid: str, *, run_agent: bool = True) -> None:
    """Translate editorial ahead of first AC; does not send to the group."""
    if not pid or has_cached_editorial_zh(pid):
        return
    editorial = get_official_editorial(pid)
    if is_no_official_editorial(pid) and not run_agent:
        if editorial is None:
            return
        clear_no_official_editorial_marker(pid)
    if not run_agent and editorial is None:
        return
    if run_agent and editorial is None:
        await ensure_tutorial_json(pid)
        editorial = get_official_editorial(pid)
    if is_no_official_editorial(pid):
        if editorial is None:
            return
        clear_no_official_editorial_marker(pid)
    if not editorial:
        logger.info("editorial prefetch remains incomplete for %s", pid)
        return
    await get_editorial_zh_for_group(editorial, pid)


async def get_editorial_zh_for_group(editorial: OfficialEditorial, pid: str) -> tuple[str | None, str]:
    """Return Chinese editorial for group card; uses disk cache keyed by pid.

    Returns (translated_text, model_tag). model_tag is empty for cache hits.
    """
    if has_cached_editorial_zh(pid):
        return _load_cached_translation(pid), ""

    problem_text = _load_problem_statement_for_editorial(pid)
    problem_images = _load_problem_images_for_editorial(pid)
    translated, model_tag, matched = await translate_editorial_to_zh(
        editorial.text,
        pid=pid,
        problem_text=problem_text,
        images=problem_images,
    )
    if matched is False:
        mark_no_official_editorial(
            pid,
            reason="translation_explicit_mismatch",
        )
        return None, ""
    if not translated:
        return None, ""
    translated = translated.strip()
    if len(translated) < MIN_EDITORIAL_LEN:
        return None, ""

    full_text = (translated + model_tag).strip() if model_tag else translated
    _save_cached_translation(pid, full_text)
    return full_text, ""


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
