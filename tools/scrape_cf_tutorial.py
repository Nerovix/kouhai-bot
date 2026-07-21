#!/usr/bin/env python3
"""Low-level Codeforces HTML helpers used by cf_tutorial_agent.py.

This module intentionally does not choose the final tutorial anymore. The LLM
harness owns candidate selection; helpers here only fetch pages, normalize HTML,
load dynamic Codeforces tutorial fragments, and parse common section shapes.
"""

from __future__ import annotations

import html
import json
import os
import random
import re
import string
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import cloudscraper
except ImportError:
    cloudscraper = None
try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None
from kouhai_bot.problems import cf_fetcher

CF_ROOT = "https://codeforces.com"
class ScrapeError(RuntimeError):
    """Expected scrape/parsing error with a stable exit code."""

    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


@dataclass
class Section:
    label: str
    title: str
    hint: str
    solution: str
    code_blocks: list[str]
    raw_text: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "title": self.title,
            "hint": self.hint,
            "solution": self.solution,
            "code_blocks": self.code_blocks,
            "raw_text": self.raw_text,
        }


_SCRAPER = None
_CURL_SESSION = None
_LAST_FETCH_AT = 0.0


def get_scraper():
    if cloudscraper is None:
        return None
    global _SCRAPER
    if _SCRAPER is None:
        _SCRAPER = cloudscraper.create_scraper()
    return _SCRAPER


def get_curl_session():
    if curl_requests is None:
        return None
    global _CURL_SESSION
    if _CURL_SESSION is None:
        _CURL_SESSION = curl_requests.Session(impersonate="chrome124")
    return _CURL_SESSION


def _fetch_with_shared_transport(
    url: str,
    *,
    fetcher: str,
    pw_wait_ms: int = 7000,
) -> str:
    try:
        return cf_fetcher.fetch_html(
            url,
            fetcher=fetcher,
            pw_wait_ms=pw_wait_ms,
        )
    except cf_fetcher.CFFetchError as exc:
        code = 10 if exc.kind == "dependency" else 9 if exc.kind == "content" else 2
        raise ScrapeError(f"抓取失败: {url} ({exc})", code) from exc


def fetch_html_http(url: str) -> str:
    """Compatibility wrapper around the shared HTTP-only transport."""
    return _fetch_with_shared_transport(url, fetcher="http")


def fetch_html_playwright(url: str, wait_ms: int = 7000) -> str:
    """Compatibility wrapper around the shared Chromium transport."""
    return _fetch_with_shared_transport(
        url,
        fetcher="playwright",
        pw_wait_ms=wait_ms,
    )


def fetch_html(url: str, fetcher: str = "auto", pw_wait_ms: int = 7000) -> str:
    global _LAST_FETCH_AT
    wait_s = 0.0
    try:
        wait_s = float(os.getenv("SCRAPE_REQUEST_WAIT_SECONDS", "0") or 0)
    except ValueError:
        wait_s = 0.0
    if wait_s > 0:
        now = time.time()
        elapsed = now - _LAST_FETCH_AT
        if elapsed < wait_s:
            time.sleep(wait_s - elapsed)
        _LAST_FETCH_AT = time.time()

    return _fetch_with_shared_transport(
        url,
        fetcher=fetcher,
        pw_wait_ms=pw_wait_ms,
    )


def _extract_csrf_token(page_html: str) -> str:
    m = re.search(
        r'<meta\s+name="X-Csrf-Token"\s+content="([0-9a-f]{32})"',
        page_html,
        re.I,
    )
    if m:
        return m.group(1)
    return ""


def _post_problem_tutorial_json(
    tutorial_url: str, tutorial_html: str, problem_code: str
) -> dict[str, Any] | None:
    csrf = _extract_csrf_token(tutorial_html)
    if not csrf:
        return None

    rv = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(9))
    endpoint = urljoin(tutorial_url, f"/data/problemTutorial?rv={rv}")
    payload = {
        "problemCode": problem_code,
        "csrf_token": csrf,
    }
    headers = {
        "Referer": tutorial_url,
        "X-Requested-With": "XMLHttpRequest",
        "X-Csrf-Token": csrf,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }

    # First try curl_cffi (usually better at handling Cloudflare-protected endpoints).
    try:
        session = get_curl_session()
        if session is not None:
            session.get(tutorial_url, timeout=30)
            resp = session.post(endpoint, data=payload, headers=headers, timeout=30)
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass

    try:
        scraper = get_scraper()
        if scraper is not None:
            resp = scraper.post(endpoint, data=payload, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp.json()

        body = urlencode(payload).encode("utf-8")
        req = Request(endpoint, data=body, headers=headers, method="POST")
        with urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        return json.loads(text)
    except Exception:
        return None


def fetch_dynamic_editorial(
    tutorial_url: str, tutorial_html: str, problem_code: str
) -> tuple[str, str]:
    data = _post_problem_tutorial_json(tutorial_url, tutorial_html, problem_code)
    if not data:
        return "", ""
    if str(data.get("success", "")).lower() != "true":
        return "", ""
    html_fragment = str(data.get("html", "")).strip()
    if not html_fragment:
        return "", ""

    # Convert returned HTML fragment into plain markdown-ish text and strip heading line.
    text = html_to_markdownish(html_fragment)
    lines = [ln for ln in text.split("\n") if ln.strip()]
    section_title = ""
    if lines and re.fullmatch(
        rf"(?:#+\s*)?{re.escape(problem_code)}\s*[—\-].*",
        lines[0].strip(),
        flags=re.I,
    ):
        m = re.match(
            rf"(?:#+\s*)?{re.escape(problem_code)}\s*[—\-]\s*(.+)$",
            lines[0].strip(),
            flags=re.I,
        )
        if m:
            section_title = m.group(1).strip()
        lines = lines[1:]
    return section_title, "\n".join(lines).strip()



def extract_page_title(html_text: str) -> str:
    m = re.search(r"<title>([\s\S]*?)</title>", html_text, re.I)
    if not m:
        return ""
    title = re.sub(r"\s+", " ", html.unescape(m.group(1))).strip()
    return title


def extract_problem_title(problem_html: str) -> str:
    m = re.search(r'<div[^>]*class="[^"]*\btitle\b[^"]*"[^>]*>([\s\S]*?)</div>', problem_html, re.I)
    if not m:
        return ""
    title = re.sub(r"<[^>]+>", " ", m.group(1))
    title = html.unescape(re.sub(r"\s+", " ", title)).strip()
    # Typical CF format: "C. Arrangement" / "C - Arrangement"
    title = re.sub(r"^[A-Za-z0-9]+\s*[.\-]\s*", "", title).strip()
    return title


def _extract_balanced_div(html_text: str, start_idx: int) -> str:
    open_match = re.search(r"<div\b[^>]*>", html_text[start_idx:], re.I)
    if not open_match:
        return ""
    abs_open_start = start_idx + open_match.start()
    abs_open_end = start_idx + open_match.end()

    depth = 1
    cursor = abs_open_end
    tag_re = re.compile(r"</?div\b[^>]*>", re.I)
    for m in tag_re.finditer(html_text, cursor):
        token = m.group(0).lower()
        if token.startswith("</div"):
            depth -= 1
            if depth == 0:
                return html_text[abs_open_end:m.start()]
        else:
            depth += 1
    return ""


def extract_blog_body_html(tutorial_html: str) -> str:
    # Codeforces blog body is usually in this class.
    m = re.search(r'<div[^>]*class="[^"]*\bttypography\b[^"]*"[^>]*>', tutorial_html, re.I)
    if m:
        body = _extract_balanced_div(tutorial_html, m.start())
        if body.strip():
            return body
    raise ScrapeError("未找到博客正文容器（ttypography）", 4)


def html_to_markdownish(body_html: str) -> str:
    s = body_html
    s = re.sub(r"<!--[\s\S]*?-->", "", s)
    s = re.sub(r"<script[\s\S]*?</script>", "", s, flags=re.I)
    s = re.sub(r"<style[\s\S]*?</style>", "", s, flags=re.I)
    s = re.sub(r"</(strong|b)>", "\n", s, flags=re.I)
    s = re.sub(r"<(strong|b)[^>]*>", "", s, flags=re.I)

    code_store: dict[str, str] = {}

    def repl_pre(m):
        key = f"@@CF_CODE_{len(code_store)}@@"
        raw = re.sub(r"<[^>]+>", "", m.group(1))
        raw = html.unescape(raw).strip("\n")
        code_store[key] = f"\n```\n{raw}\n```\n"
        return f"\n{key}\n"

    s = re.sub(r"<pre[^>]*>([\s\S]*?)</pre>", repl_pre, s, flags=re.I)

    def repl_heading(m):
        level = int(m.group(1))
        content = re.sub(r"<[^>]+>", "", m.group(2))
        content = html.unescape(re.sub(r"\s+", " ", content)).strip()
        return "\n" + ("#" * level) + " " + content + "\n"

    s = re.sub(r"<h([1-6])[^>]*>([\s\S]*?)</h\1>", repl_heading, s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(p|div|li|tr|table|ul|ol)>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)

    for key, block in code_store.items():
        s = s.replace(key, block)

    lines = [ln.rstrip() for ln in s.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    out: list[str] = []
    prev_blank = False
    for ln in lines:
        blank = len(ln.strip()) == 0
        if blank:
            if not prev_blank:
                out.append("")
            prev_blank = True
            continue
        out.append(ln.strip())
        prev_blank = False
    return "\n".join(out).strip()


def _extract_text_between(lines: list[str], start: int, end: int) -> str:
    body = "\n".join(lines[start:end]).strip()
    return body


def _strip_loading_placeholder(text: str) -> str:
    if not text:
        return text
    lines = text.split("\n")
    cleaned: list[str] = []
    prev_blank = True
    for ln in lines:
        s = ln.strip().lower()
        if s == "tutorial is loading..." or s == "tutorial is loading":
            continue
        if not ln.strip():
            if not prev_blank:
                cleaned.append("")
            prev_blank = True
            continue
        cleaned.append(ln.rstrip())
        prev_blank = False
    return "\n".join(cleaned).strip()


def _normalize_title_key(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "", text)
    return text


def parse_legacy_title_sections(markdownish: str) -> list[tuple[str, str]]:
    heading_re = re.compile(r"(?m)^#{2,6}\s+(.+?)\s*$")
    matches = list(heading_re.finditer(markdownish))
    sections: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        if not title:
            continue
        # Skip obviously non-problem headings from very old editorials.
        if re.fullmatch(r"editorial.*|codeforces.*", title, flags=re.I):
            continue
        block_start = m.end()
        block_end = matches[i + 1].start() if i + 1 < len(matches) else len(markdownish)
        block = markdownish[block_start:block_end].strip()
        if block:
            sections.append((title, block))
    return sections


def _find_section_headings(markdownish: str) -> list[re.Match[str]]:
    patterns = [
        # Markdown heading styles: ## A - Title / #### F1 - Title / ## 1000E - Title
        re.compile(
            r"(?m)^#{1,6}\s+(?:\d+\s*)?([A-Z](?:\d+)?)\s*[—\-]\s*(.+?)\s*$"
        ),
        # This editorial style: 1000A - Title (no markdown heading prefix)
        re.compile(
            r"(?m)^(?:\d+\s*)?([A-Z](?:\d+)?)\s*[—\-]\s*(.+?)\s*$"
        ),
    ]
    for pattern in patterns:
        matches = list(pattern.finditer(markdownish))
        if matches:
            return matches
    return []


def parse_sections(markdownish: str) -> list[Section]:
    matches = _find_section_headings(markdownish)
    if not matches:
        raise ScrapeError("未识别到题目分段（形如 1000A - Title 或 #### A - Title）", 5)

    sections: list[Section] = []
    for i, m in enumerate(matches):
        block_start = m.end()
        block_end = matches[i + 1].start() if i + 1 < len(matches) else len(markdownish)
        block = markdownish[block_start:block_end].strip()

        code_blocks = [
            code.strip()
            for code in re.findall(r"```(?:[^\n`]*)\n([\s\S]*?)\n```", block)
            if code.strip()
        ]
        block_without_code = re.sub(r"```(?:[^\n`]*)\n[\s\S]*?\n```", "", block)

        lines = [ln.rstrip() for ln in block_without_code.split("\n")]
        markers: list[tuple[int, str]] = []
        for idx, ln in enumerate(lines):
            token = ln.strip().strip("*").strip()
            if re.fullmatch(r"Hint(?:\s*\d+)?", token, flags=re.I):
                markers.append((idx, "hint"))
            elif re.fullmatch(r"Tutorial", token, flags=re.I):
                markers.append((idx, "solution"))
            elif re.fullmatch(r"Editorial", token, flags=re.I):
                markers.append((idx, "hint"))
            elif re.fullmatch(r"Solution", token, flags=re.I):
                markers.append((idx, "solution"))
            elif re.fullmatch(r"Solution\s*\([^)]*\)", token, flags=re.I):
                markers.append((idx, "solution"))
            elif re.fullmatch(r"Solution(?:\s+with[\s\S]*)?", token, flags=re.I):
                markers.append((idx, "solution"))
            elif re.fullmatch(r"Code(?:\s*\([^)]+\))?", token, flags=re.I):
                markers.append((idx, "code"))

        hint_chunks: list[str] = []
        solution_chunks: list[str] = []
        if markers:
            for j, (idx, kind) in enumerate(markers):
                nxt = markers[j + 1][0] if j + 1 < len(markers) else len(lines)
                segment = _extract_text_between(lines, idx + 1, nxt)
                if not segment:
                    continue
                if kind == "hint":
                    hint_chunks.append(segment)
                elif kind == "solution":
                    solution_chunks.append(segment)
        else:
            # Keep fallback for unexpected but still similar content.
            solution_chunks.append(block_without_code.strip())

        sections.append(
            Section(
                label=m.group(1).upper(),
                title=m.group(2).strip(),
                hint="\n\n".join(hint_chunks).strip(),
                solution="\n\n".join(solution_chunks).strip(),
                code_blocks=code_blocks,
                raw_text=block.strip(),
            )
        )
    return sections



def parse_pid(pid: str) -> tuple[str, str]:
    m = re.fullmatch(r"(\d+)([A-Za-z0-9]+)", pid.strip())
    if not m:
        raise ScrapeError(f"非法题号: {pid}", 6)
    return m.group(1), m.group(2)


def build_problem_url_from_pid(pid: str) -> str:
    contest_id, index = parse_pid(pid)
    return f"{CF_ROOT}/problemset/problem/{contest_id}/{index}"


def list_statement_pids(statements_dir: str) -> list[str]:
    p = Path(statements_dir)
    if not p.exists() or not p.is_dir():
        raise ScrapeError(f"statements 目录不存在: {statements_dir}", 7)

    pids: list[str] = []
    for file in sorted(p.glob("*.json")):
        pid = file.stem
        if re.fullmatch(r"\d+[A-Za-z0-9]+", pid):
            pids.append(pid)
    if not pids:
        raise ScrapeError(f"statements 目录下未找到题号 JSON: {statements_dir}", 8)
    return pids
