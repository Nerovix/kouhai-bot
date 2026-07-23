"""Pure models and extraction helpers for normalized official editorials."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

MIN_EDITORIAL_LEN = 80

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


def is_placeholder(text: str) -> bool:
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
    for line in lines:
        token = line.strip()
        if not out and (
            _AUTHOR_LINE_RE.match(token)
            or token.lower() == "editorial"
        ):
            continue
        if _is_section_marker(token):
            continue
        out.append(line)
    return "\n".join(out).strip()


def _append_code_blocks(body: str, code_blocks: list[str]) -> str:
    codes = [code.strip() for code in code_blocks if code and code.strip()]
    if not codes:
        return body
    code_part = "\n\n".join(f"```\n{code}\n```" for code in codes)
    return f"{body}\n\n{code_part}".strip() if body else code_part


def extract_editorial(section: dict) -> str:
    """Extract editorial body from one normalized tutorial section."""
    hint = (section.get("hint") or "").strip()
    solution = (section.get("solution") or "").strip()
    raw_text = (section.get("raw_text") or "").strip()
    code_blocks = section.get("code_blocks") or []

    body = ""
    if solution and not is_placeholder(solution):
        body = solution
    elif hint and not is_placeholder(hint):
        body = hint
    elif raw_text:
        body = _clean_raw_text(raw_text)
        if is_placeholder(body):
            body = ""
    return _append_code_blocks(body, code_blocks)


def editorial_from_bundle(bundle: object) -> OfficialEditorial | None:
    if not isinstance(bundle, dict):
        return None
    sections = bundle.get("sections") or []
    if not sections or not isinstance(sections[0], dict):
        return None
    text = extract_editorial(sections[0])
    if len(text) < MIN_EDITORIAL_LEN:
        return None
    return OfficialEditorial(
        text=text,
        tutorial_url=(bundle.get("tutorial_url") or "").strip(),
        tutorial_title=(bundle.get("tutorial_title") or "").strip(),
    )


def bundle_for_editorial(editorial: OfficialEditorial) -> dict[str, Any]:
    """Build a normalized bundle for an externally supplied candidate."""
    return {
        "tutorial_url": editorial.tutorial_url,
        "tutorial_title": editorial.tutorial_title,
        "sections": [
            {
                "hint": "",
                "solution": editorial.text,
                "raw_text": editorial.text,
                "code_blocks": [],
            }
        ],
    }
