#!/usr/bin/env python3
"""
Scrape Codeforces tutorial/editorial by problem URL.

Current supported tutorial style (v1):
- Editorial page split by problem sections like:
  #### A - ...
  #### B - ...
- Inside each section, parse Hint / Solution / Code.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import string
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
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
try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None
    PlaywrightError = Exception
    PlaywrightTimeoutError = TimeoutError

CF_ROOT = "https://codeforces.com"
DEFAULT_STATE_DIR = os.path.expanduser("./")
DEFAULT_STATEMENTS_DIR = os.path.join(DEFAULT_STATE_DIR, "statements")
DEFAULT_TUTORIALS_DIR = os.path.join(DEFAULT_STATE_DIR, "tutorials")


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


def _is_cf_challenge_page(text: str) -> bool:
    return bool(re.search(r"browser is being checked|Please wait\.", text, re.I))


def fetch_html_http(url: str) -> str:
    try:
        scraper = get_scraper()
        if scraper is not None:
            resp = scraper.get(url, timeout=30)
            resp.raise_for_status()
            body = resp.text
            if _is_cf_challenge_page(body):
                raise ScrapeError(f"被 Codeforces 反爬挑战拦截: {url}", 9)
            return body

        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=30) as resp:
            body = resp.read()
            text = body.decode("utf-8", errors="replace")
            if _is_cf_challenge_page(text):
                raise ScrapeError(f"被 Codeforces 反爬挑战拦截: {url}", 9)
            return text
    except Exception as exc:
        parsed = urlparse(url)
        if (
            "403" in str(exc)
            and parsed.netloc == "codeforces.com"
            and parsed.path
        ):
            mirror_url = f"https://m1.codeforces.com{parsed.path}"
            if parsed.query:
                mirror_url = f"{mirror_url}?{parsed.query}"
            try:
                req = Request(mirror_url, headers={"User-Agent": "Mozilla/5.0"})
                with urlopen(req, timeout=30) as resp:
                    body = resp.read()
                    text = body.decode("utf-8", errors="replace")
                    if _is_cf_challenge_page(text):
                        raise ScrapeError(f"被 Codeforces 反爬挑战拦截: {mirror_url}", 9)
                    return text
            except Exception:
                pass
        raise ScrapeError(f"抓取失败: {url} ({exc})", 2) from exc


def fetch_html_playwright(url: str, wait_ms: int = 7000) -> str:
    if sync_playwright is None:
        raise ScrapeError(
            "未安装 playwright。请先运行: python -m pip install playwright && python -m playwright install chromium",
            10,
        )
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            if wait_ms > 0:
                page.wait_for_timeout(wait_ms)
            text = page.content()
            browser.close()
        if _is_cf_challenge_page(text):
            raise ScrapeError(f"Playwright 抓取后仍命中反爬挑战页: {url}", 9)
        return text
    except ScrapeError:
        raise
    except PlaywrightTimeoutError as exc:
        raise ScrapeError(f"Playwright 超时: {url} ({exc})", 2) from exc
    except PlaywrightError as exc:
        raise ScrapeError(f"Playwright 抓取失败: {url} ({exc})", 2) from exc


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

    if fetcher == "http":
        return fetch_html_http(url)
    if fetcher == "playwright":
        return fetch_html_playwright(url, wait_ms=pw_wait_ms)

    # auto: HTTP first, fallback to Playwright if anti-bot challenge detected.
    try:
        return fetch_html_http(url)
    except ScrapeError as exc:
        if exc.code == 9:
            return fetch_html_playwright(url, wait_ms=pw_wait_ms)
        raise


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


def parse_problem_id(problem_url: str) -> str:
    m = re.search(r"/problem(?:set)?/problem/(\d+)/([A-Za-z0-9]+)", problem_url)
    if m:
        return f"{m.group(1)}{m.group(2)}"
    m2 = re.search(r"/contest/(\d+)/problem/([A-Za-z0-9]+)", problem_url)
    if m2:
        return f"{m2.group(1)}{m2.group(2)}"
    return ""


def extract_tutorial_url(problem_html: str, base_url: str) -> str:
    anchor_pattern = re.compile(
        r"<a[^>]+href\s*=\s*['\"](?P<href>[^'\"]+)['\"][^>]*>(?P<text>[\s\S]*?)</a>",
        re.I,
    )
    candidates: list[tuple[str, str]] = []
    for m in anchor_pattern.finditer(problem_html):
        href = m.group("href")
        text = re.sub(r"<[^>]+>", " ", m.group("text"))
        text = html.unescape(re.sub(r"\s+", " ", text)).strip().lower()
        normalized_href = href.lower()
        # Some problems provide tutorial only as an external PDF.
        if (
            ("tutorial" in text or "editorial" in text)
            and ("pdf" in text or normalized_href.endswith(".pdf") or ".pdf?" in normalized_href)
        ):
            raise ScrapeError(f"Tutorial 为 PDF，跳过提取: {urljoin(base_url, href)}", 11)
        if "/blog/entry/" not in href.lower():
            continue
        candidates.append((href, text))
        if "tutorial" in text or "editorial" in text:
            return urljoin(base_url, href)

    # Fallback: if multiple blog entries exist on the page, prefer the largest entry id.
    if candidates:
        def _entry_id(h: str) -> int:
            m = re.search(r"/blog/entry/(\d+)", h, re.I)
            return int(m.group(1)) if m else -1
        href, _ = max(candidates, key=lambda x: _entry_id(x[0]))
        return urljoin(base_url, href)

    m2 = re.search(r"/blog/entry/\d+", problem_html, re.I)
    if m2:
        return urljoin(base_url, m2.group(0))

    raise ScrapeError("在题目页中未找到 Tutorial 链接", 3)


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


def build_result(problem_url: str, fetcher: str = "auto", pw_wait_ms: int = 7000) -> dict[str, Any]:
    parsed = urlparse(problem_url)
    if not parsed.scheme or not parsed.netloc:
        raise ScrapeError(f"非法题目链接: {problem_url}", 1)

    normalized_problem_url = problem_url.strip()
    problem_html = fetch_html(normalized_problem_url, fetcher=fetcher, pw_wait_ms=pw_wait_ms)
    problem_title = extract_problem_title(problem_html)
    tutorial_url = extract_tutorial_url(problem_html, normalized_problem_url)
    tutorial_html = fetch_html(tutorial_url, fetcher=fetcher, pw_wait_ms=pw_wait_ms)

    body_html = extract_blog_body_html(tutorial_html)
    markdownish = html_to_markdownish(body_html)
    pid = parse_problem_id(normalized_problem_url)
    dynamic_title = ""
    dynamic_hint = ""
    if pid:
        dynamic_title, dynamic_hint = fetch_dynamic_editorial(
            tutorial_url=tutorial_url,
            tutorial_html=tutorial_html,
            problem_code=pid,
        )

    try:
        sections = parse_sections(markdownish)
    except ScrapeError:
        sections = []
        if pid:
            _, problem_index = parse_pid(pid)
            if dynamic_hint:
                sections = [
                    Section(
                        label=problem_index.upper(),
                        title=dynamic_title,
                        hint=dynamic_hint,
                        solution="",
                        code_blocks=[],
                        raw_text=markdownish.strip(),
                    )
                ]
            elif problem_title:
                legacy_sections = parse_legacy_title_sections(markdownish)
                target_key = _normalize_title_key(problem_title)
                for legacy_title, legacy_block in legacy_sections:
                    if _normalize_title_key(legacy_title) == target_key:
                        code_blocks = [
                            code.strip()
                            for code in re.findall(
                                r"```(?:[^\n`]*)\n([\s\S]*?)\n```", legacy_block
                            )
                            if code.strip()
                        ]
                        text_without_code = re.sub(
                            r"```(?:[^\n`]*)\n[\s\S]*?\n```", "", legacy_block
                        ).strip()
                        sections = [
                            Section(
                                label=problem_index.upper(),
                                title=legacy_title.strip(),
                                hint="",
                                solution=text_without_code,
                                code_blocks=code_blocks,
                                raw_text=legacy_block,
                            )
                        ]
                        break
        if not sections:
            raise

    # Keep only the target problem section (e.g. 1000E -> E).
    if pid:
        _, problem_index = parse_pid(pid)
        target = problem_index.upper()
        matched = [s for s in sections if s.label.upper() == target]
        if matched:
            sections = matched

    # Some tutorial pages load Editorial via AJAX (/data/problemTutorial).
    # If we only got placeholder text, try fetching the dynamic editorial.
    if pid:
        for sec in sections:
            needs_dynamic = (
                re.search(r"Tutorial is loading", sec.raw_text, flags=re.I)
                or re.search(r"Tutorial is loading", sec.hint, flags=re.I)
                or re.search(r"Tutorial is loading", sec.solution, flags=re.I)
                or (
                    not sec.hint.strip()
                    and not sec.solution.strip()
                    and len(sec.code_blocks) == 0
                    and dynamic_hint.strip() != ""
                )
            )
            if needs_dynamic:
                if dynamic_hint:
                    if re.search(r"\bTutorial\b", sec.raw_text, flags=re.I):
                        if not sec.solution.strip() or re.search(
                            r"Tutorial is loading", sec.solution, flags=re.I
                        ):
                            sec.solution = dynamic_hint
                        if re.search(r"Tutorial is loading", sec.hint, flags=re.I):
                            sec.hint = ""
                    elif not sec.hint.strip() and not sec.solution.strip():
                        # If static section parse produced an empty block, treat dynamic
                        # payload as the main solution text instead of a hint.
                        sec.solution = dynamic_hint
                    else:
                        sec.hint = dynamic_hint
                if dynamic_title and not sec.title:
                    sec.title = dynamic_title

    # Normalize: for tutorial-styled pages, keep explanation in solution field.
    for sec in sections:
        if (
            sec.hint.strip()
            and (
                not sec.solution.strip()
                or re.search(r"Tutorial is loading", sec.solution, flags=re.I)
            )
            and re.search(r"\bTutorial\b", sec.raw_text, flags=re.I)
        ):
            sec.solution = sec.hint
            sec.hint = ""

    # Always drop known loading placeholders from final fields.
    for sec in sections:
        sec.hint = _strip_loading_placeholder(sec.hint)
        sec.solution = _strip_loading_placeholder(sec.solution)

    return {
        "problem_url": normalized_problem_url,
        "problem_id": pid,
        "tutorial_url": tutorial_url,
        "tutorial_title": extract_page_title(tutorial_html),
        "sections": [s.as_dict() for s in sections],
    }


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


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Scrape one Codeforces tutorial by problem URL (low-level). "
            "Batch crawl from statements/ → tutorials/ use tools/tutorial_tools.py crawl."
        )
    )
    parser.add_argument("--problem-url", required=True, help="Codeforces problem URL")
    parser.add_argument("--output", help="Write JSON output to a file")
    parser.add_argument(
        "--fetcher",
        choices=["auto", "http", "playwright"],
        default="auto",
        help="Fetch backend. auto=HTTP then Playwright fallback when anti-bot challenged",
    )
    parser.add_argument(
        "--pw-wait-ms",
        type=int,
        default=7000,
        help="Extra wait time in milliseconds when using Playwright",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    args = parser.parse_args()

    try:
        result = build_result(
            args.problem_url,
            fetcher=args.fetcher,
            pw_wait_ms=max(0, args.pw_wait_ms),
        )
    except ScrapeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(exc.code)

    payload = json.dumps(
        result,
        ensure_ascii=False,
        indent=2 if args.pretty else None,
    )
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(payload)
            if args.pretty:
                f.write("\n")
    else:
        try:
            print(payload)
        except UnicodeEncodeError:
            sys.stdout.buffer.write((payload + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
