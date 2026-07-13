#!/usr/bin/env python3
"""LLM harness for Codeforces official editorial discovery.

The harness fetches a bounded set of Codeforces blog posts linked from the
problem page, sends each blog body to the configured general model, and asks it
to extract the subsection that actually explains the target problem.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin

from kouhai_bot.handlers.shared import parse_json_with_llm_repair
from kouhai_bot.llm import chat_completion
from kouhai_bot.tutorials import MIN_EDITORIAL_LEN, extract_editorial

from scrape_cf_tutorial import ScrapeError
from scrape_cf_tutorial import Section
from scrape_cf_tutorial import build_problem_url_from_pid
from scrape_cf_tutorial import extract_blog_body_html
from scrape_cf_tutorial import extract_page_title
from scrape_cf_tutorial import extract_problem_title
from scrape_cf_tutorial import fetch_dynamic_editorial
from scrape_cf_tutorial import fetch_html
from scrape_cf_tutorial import html_to_markdownish
from scrape_cf_tutorial import parse_pid


DEFAULT_DEADLINE_SEC = 150
DEFAULT_CANDIDATE_LIMIT = 3
DEFAULT_CONFIDENCE_THRESHOLD = 0.75
DEFAULT_SELECTOR_TIMEOUT_SEC = 40
DEFAULT_LLM_TEXT_LIMIT = 200000


class AgentNoMatch(RuntimeError):
    """Expected no-result outcome. The caller should not write a tutorial JSON."""


@dataclass(frozen=True)
class EditorialCandidate:
    candidate_id: str
    tutorial_url: str
    tutorial_title: str
    source_kind: str
    label: str
    title: str
    section: Section

    def extracted_text(self) -> str:
        return extract_editorial(self.section.as_dict())

    def llm_text(self, limit: int) -> str:
        text = self.extracted_text() or self.section.raw_text
        text = re.sub(r"```[\s\S]*?```", "\n(代码块略)\n", text).strip()
        if len(text) <= limit:
            return text
        return text[: limit - 20] + "\n...(已截断)"


@dataclass(frozen=True)
class BlogDocument:
    blog_id: str
    tutorial_url: str
    tutorial_title: str
    body: str


@dataclass(frozen=True)
class AgentResult:
    pid: str
    bundle: dict[str, Any]
    selected_candidate_id: str
    confidence: float
    reason: str
    elapsed_sec: float
    candidate_count: int


def load_statement(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScrapeError(f"无法读取题面 JSON: {path} ({exc})", 7) from exc
    if not isinstance(data, dict):
        raise ScrapeError(f"题面 JSON 不是对象: {path}", 7)
    return data


def statement_to_text(stmt: dict[str, Any], *, limit: int = 12000) -> str:
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
        ("Notes", "notes"),
    ]:
        value = str(stmt.get(key) or "").strip()
        if value:
            parts.append(f"\n{label}:\n{value}")
    samples = stmt.get("samples") or []
    if isinstance(samples, list):
        for sample in samples[:2]:
            if not isinstance(sample, dict):
                continue
            parts.append(
                "\nSample:\n"
                f"Input:\n{sample.get('input', '')}\n"
                f"Output:\n{sample.get('output', '')}"
            )
    text = "\n".join(parts).strip()
    if len(text) > limit:
        return text[: limit - 20] + "\n...(题面已截断)"
    return text


def extract_blog_links(problem_html: str, base_url: str, *, limit: int) -> list[str]:
    anchor_re = re.compile(
        r"<a[^>]+href\s*=\s*['\"](?P<href>[^'\"]+)['\"][^>]*>(?P<text>[\s\S]*?)</a>",
        re.I,
    )
    seen: set[str] = set()
    scored: list[tuple[int, int, str]] = []
    for pos, match in enumerate(anchor_re.finditer(problem_html)):
        href = match.group("href")
        if "/blog/entry/" not in href.lower():
            continue
        text = re.sub(r"<[^>]+>", " ", match.group("text"))
        text = html.unescape(re.sub(r"\s+", " ", text)).strip().lower()
        url = urldefrag(urljoin(base_url, href))[0]
        if url in seen:
            continue
        seen.add(url)
        score = 0
        if "tutorial" in text or "editorial" in text:
            score += 100
        if "announcement" in text or "standings" in text:
            score -= 20
        scored.append((-score, pos, url))
    scored.sort()
    return [url for _, _, url in scored[: max(1, limit)]]


def _section_from_text(label: str, title: str, text: str) -> Section:
    code_blocks = [
        code.strip()
        for code in re.findall(r"```(?:[^\n`]*)\n([\s\S]*?)\n```", text)
        if code.strip()
    ]
    text_without_code = re.sub(
        r"```(?:[^\n`]*)\n[\s\S]*?\n```", "", text
    ).strip()
    return Section(
        label=label,
        title=title.strip(),
        hint="",
        solution=text_without_code,
        code_blocks=code_blocks,
        raw_text=text.strip(),
    )


def _clip_for_llm(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 30)] + "\n...(blog body truncated)"


def collect_blog_documents(
    *,
    pid: str,
    fetcher: str,
    pw_wait_ms: int,
    blog_limit: int,
) -> tuple[str, str, list[BlogDocument]]:
    problem_url = build_problem_url_from_pid(pid)
    problem_html = fetch_html(problem_url, fetcher=fetcher, pw_wait_ms=pw_wait_ms)
    problem_title = extract_problem_title(problem_html)
    blog_urls = extract_blog_links(problem_html, problem_url, limit=blog_limit)
    if not blog_urls:
        raise AgentNoMatch("problem_page_has_no_blog_entry_links")

    blogs: list[BlogDocument] = []
    for idx, blog_url in enumerate(blog_urls, start=1):
        try:
            tutorial_html = fetch_html(blog_url, fetcher=fetcher, pw_wait_ms=pw_wait_ms)
            tutorial_title = extract_page_title(tutorial_html)
            body_html = extract_blog_body_html(tutorial_html)
            body = html_to_markdownish(body_html)
            dynamic_title, dynamic_text = fetch_dynamic_editorial(
                tutorial_url=blog_url,
                tutorial_html=tutorial_html,
                problem_code=pid,
            )
            if dynamic_text.strip():
                title = dynamic_title or problem_title or pid
                body = (
                    f"{body.strip()}\n\n"
                    f"[Codeforces dynamic tutorial fragment for {pid} - {title}]\n"
                    f"{dynamic_text.strip()}"
                ).strip()
        except ScrapeError:
            continue
        if body.strip():
            blogs.append(
                BlogDocument(
                    blog_id=f"b{idx}",
                    tutorial_url=blog_url,
                    tutorial_title=tutorial_title,
                    body=body,
                )
            )

    if not blogs:
        raise AgentNoMatch("no_readable_blog_bodies")
    return problem_url, problem_title, blogs


def _build_extractor_messages(
    *,
    pid: str,
    problem_title: str,
    problem_text: str,
    blog: BlogDocument,
    llm_text_limit: int,
) -> list[dict[str, str]]:
    payload = {
        "pid": pid,
        "problem_title": problem_title,
        "problem": problem_text,
        "blog": {
            "url": blog.tutorial_url,
            "title": blog.tutorial_title,
            "body": _clip_for_llm(blog.body, llm_text_limit),
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "你是 Codeforces 官方题解正文提取器。你会收到一道题的题面，以及一篇 "
                "Codeforces blog 的主体全文。你的任务不是解题，也不是总结，而是从 blog 主体中"
                "找出这道题对应的官方题解/教程正文。\n"
                "只输出 JSON 对象。匹配成功时输出："
                "{\"match\":true,\"section_title\":\"...\","
                "\"start_text\":\"...\",\"end_text\":\"...\","
                "\"confidence\":0.0到1.0,\"reason\":\"...\"}。"
                "匹配失败时输出：{\"match\":false,\"reason\":\"...\"}。\n"
                "要求：start_text 和 end_text 必须是从 blog.body 原文中逐字复制的短片段，"
                "用于标记本题题解正文的开始和结束，且尽量选择唯一、不易混淆的片段；"
                "不要输出完整题解正文，不要编写新题解，"
                "不要补全缺失公式，不要夹带其他题目的题解、公告、作者信息、评论或标签。"
                "如果 blog 中有多题题解，必须用题号、标题、题意、输入输出、变量和算法对象共同判断。"
                "如果只有占位、公告、榜单、代码片段或无法确认是本题，必须 match=false。"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _find_excerpt_span(text: str, excerpt: str, *, start: int = 0) -> tuple[int, int] | None:
    excerpt = (excerpt or "").strip()
    if not excerpt:
        return None
    pos = text.find(excerpt, start)
    if pos >= 0:
        return pos, pos + len(excerpt)

    parts = [part for part in re.split(r"\s+", excerpt) if part]
    if not parts:
        return None
    pattern = r"\s+".join(re.escape(part) for part in parts)
    match = re.search(pattern, text[start:])
    if not match:
        return None
    return start + match.start(), start + match.end()


def _extract_body_span(body: str, start_text: str, end_text: str) -> str:
    start_span = _find_excerpt_span(body, start_text)
    if not start_span:
        return ""
    end_span = _find_excerpt_span(body, end_text, start=start_span[0])
    if not end_span:
        return ""
    if end_span[1] <= start_span[0]:
        return ""
    return body[start_span[0]:end_span[1]].strip()


async def extract_editorial_from_blog(
    *,
    pid: str,
    problem_title: str,
    problem_text: str,
    blog: BlogDocument,
    timeout: int,
    llm_text_limit: int,
) -> tuple[EditorialCandidate, float, str]:
    result = await chat_completion(
        _build_extractor_messages(
            pid=pid,
            problem_title=problem_title,
            problem_text=problem_text,
            blog=blog,
            llm_text_limit=llm_text_limit,
        ),
        task="summary",
        temperature=0.0,
        timeout=timeout,
        response_format={"type": "json_object"},
        send_reasoning_effort=False,
    )
    if not result.text:
        raise AgentNoMatch(f"extractor_llm_failed:{result.failure_kind}")

    parsed, _repair_tag = await parse_json_with_llm_repair(
        result.text,
        expected_schema=(
            '{"match": true, "section_title": "...", '
            '"start_text": "...", "end_text": "...", '
            '"confidence": 0.0, "reason": "..."}'
            ' or {"match": false, "reason": "..."}'
        ),
        task="summary",
        timeout=timeout,
    )
    if parsed.get("match") is not True:
        raise AgentNoMatch(str(parsed.get("reason") or "extractor_no_match"))

    start_text = str(parsed.get("start_text") or "").strip()
    end_text = str(parsed.get("end_text") or "").strip()
    editorial_text = _extract_body_span(blog.body, start_text, end_text)
    if len(editorial_text) < MIN_EDITORIAL_LEN:
        raise AgentNoMatch("extractor_span_not_found_or_too_short")

    try:
        confidence = float(parsed.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    reason = str(parsed.get("reason") or "").strip()
    section_title = str(parsed.get("section_title") or problem_title or pid).strip()
    _, target_index = parse_pid(pid)
    candidate = EditorialCandidate(
        candidate_id=blog.blog_id,
        tutorial_url=blog.tutorial_url,
        tutorial_title=blog.tutorial_title,
        source_kind="llm_blog_extract",
        label=target_index.upper(),
        title=section_title,
        section=_section_from_text(target_index.upper(), section_title, editorial_text),
    )
    return candidate, confidence, reason


def build_bundle(
    *,
    pid: str,
    problem_url: str,
    selected: EditorialCandidate,
    confidence: float,
    reason: str,
    candidate_count: int,
    elapsed_sec: float,
) -> dict[str, Any]:
    section = selected.section.as_dict()
    return {
        "problem_url": problem_url,
        "problem_id": pid,
        "tutorial_url": selected.tutorial_url,
        "tutorial_title": selected.tutorial_title,
        "sections": [section],
        "agent_meta": {
            "tool": "cf_tutorial_agent",
            "selected_candidate_id": selected.candidate_id,
            "source_kind": selected.source_kind,
            "confidence": confidence,
            "reason": reason,
            "candidate_count": candidate_count,
            "elapsed_sec": round(elapsed_sec, 3),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    }


async def run_agent_for_pid(
    *,
    pid: str,
    statements_dir: Path,
    fetcher: str = "auto",
    pw_wait_ms: int = 7000,
    blog_limit: int = DEFAULT_CANDIDATE_LIMIT,
    deadline_sec: int = DEFAULT_DEADLINE_SEC,
    selector_timeout_sec: int = DEFAULT_SELECTOR_TIMEOUT_SEC,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    llm_text_limit: int = DEFAULT_LLM_TEXT_LIMIT,
) -> AgentResult:
    started = time.monotonic()
    stmt = load_statement(statements_dir / f"{pid}.json")
    problem_text = statement_to_text(stmt)

    async def _run() -> AgentResult:
        problem_url, problem_title, blogs = await asyncio.to_thread(
            collect_blog_documents,
            pid=pid,
            fetcher=fetcher,
            pw_wait_ms=pw_wait_ms,
            blog_limit=blog_limit,
        )
        failures: list[str] = []
        selected: EditorialCandidate | None = None
        confidence = 0.0
        reason = ""
        effective_problem_title = problem_title or str(stmt.get("name") or "")
        for blog in blogs:
            try:
                candidate, candidate_confidence, candidate_reason = await extract_editorial_from_blog(
                    pid=pid,
                    problem_title=effective_problem_title,
                    problem_text=problem_text,
                    blog=blog,
                    timeout=selector_timeout_sec,
                    llm_text_limit=llm_text_limit,
                )
            except AgentNoMatch as exc:
                failures.append(f"{blog.blog_id}:{exc}")
                continue
            if candidate_confidence < confidence_threshold:
                failures.append(
                    f"{blog.blog_id}:extractor_low_confidence:{candidate_confidence:.2f}:"
                    f"{candidate_reason or candidate.title}"
                )
                continue
            selected = candidate
            confidence = candidate_confidence
            reason = candidate_reason
            break

        if selected is None:
            detail = "; ".join(failures) if failures else "extractor_no_match"
            raise AgentNoMatch(detail)

        elapsed = time.monotonic() - started
        bundle = build_bundle(
            pid=pid,
            problem_url=problem_url,
            selected=selected,
            confidence=confidence,
            reason=reason,
            candidate_count=len(blogs),
            elapsed_sec=elapsed,
        )
        return AgentResult(
            pid=pid,
            bundle=bundle,
            selected_candidate_id=selected.candidate_id,
            confidence=confidence,
            reason=reason,
            elapsed_sec=elapsed,
            candidate_count=len(blogs),
        )

    try:
        return await asyncio.wait_for(_run(), timeout=max(1, deadline_sec))
    except asyncio.TimeoutError as exc:
        raise AgentNoMatch(f"deadline_exceeded:{deadline_sec}s") from exc
