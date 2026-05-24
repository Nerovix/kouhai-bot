"""Load and extract Codeforces official editorials from scraped tutorial JSON.

Extraction rules align with tools/scrape_cf_tutorial.py normalization (hint/solution/raw_text).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from .config import get_config
from .handlers.shared import translate_editorial_to_zh

MIN_EDITORIAL_LEN = 80
_REVIEW_EDITORIAL_MAX_LEN = 12000

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


def load_tutorial(pid: str) -> dict | None:
    path = os.path.join(get_config().data_dir, "tutorials", f"{pid}.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


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


def _load_cached_translation(pid: str) -> str:
    path = _translation_cache_path(pid)
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _save_cached_translation(pid: str, text: str) -> None:
    path = _translation_cache_path(pid)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    marker = _no_editorial_marker_path(pid)
    if os.path.isfile(marker):
        os.remove(marker)


def is_no_official_editorial(pid: str) -> bool:
    return os.path.isfile(_no_editorial_marker_path(pid))


def mark_no_official_editorial(pid: str) -> None:
    marker = _no_editorial_marker_path(pid)
    with open(marker, "w", encoding="utf-8"):
        pass
    path = _translation_cache_path(pid)
    if os.path.isfile(path):
        os.remove(path)


def clear_no_official_editorial_marker(pid: str) -> None:
    marker = _no_editorial_marker_path(pid)
    if os.path.isfile(marker):
        os.remove(marker)


def has_cached_editorial_zh(pid: str) -> bool:
    return len(_load_cached_translation(pid)) >= MIN_EDITORIAL_LEN


def load_cached_editorial_zh(pid: str) -> str:
    return _load_cached_translation(pid)


async def prefetch_editorial_zh(pid: str) -> None:
    """Translate editorial ahead of first AC; does not send to the group."""
    if not pid or has_cached_editorial_zh(pid):
        return
    if is_no_official_editorial(pid):
        if get_official_editorial(pid) is None:
            return
        clear_no_official_editorial_marker(pid)
    editorial = get_official_editorial(pid)
    if not editorial:
        mark_no_official_editorial(pid)
        return
    await get_editorial_zh_for_group(editorial, pid)


async def get_editorial_zh_for_group(editorial: OfficialEditorial, pid: str) -> tuple[str | None, str]:
    """Return Chinese editorial for group card; uses disk cache keyed by pid.

    Returns (translated_text, model_tag). model_tag is empty for cache hits.
    """
    cached = _load_cached_translation(pid)
    if len(cached) >= MIN_EDITORIAL_LEN:
        return cached, ""

    translated, model_tag = await translate_editorial_to_zh(editorial.text, pid=pid)
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
