#!/usr/bin/env python3
"""Lightweight LLM harness for Codeforces official editorial discovery.

This is intentionally not a general web agent. It fetches a small, bounded set
of Codeforces pages, asks the configured LLM to choose the matching candidate,
and writes the existing tutorials/{pid}.json schema only for high-confidence
matches.
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

from kouhai_bot.handlers.shared import robust_json_parse
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
from scrape_cf_tutorial import parse_legacy_title_sections
from scrape_cf_tutorial import parse_pid
from scrape_cf_tutorial import parse_sections


DEFAULT_DEADLINE_SEC = 150
DEFAULT_CANDIDATE_LIMIT = 3
DEFAULT_CONFIDENCE_THRESHOLD = 0.75
DEFAULT_SELECTOR_TIMEOUT_SEC = 40
DEFAULT_LLM_TEXT_LIMIT = 4500


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


def _section_from_block(label: str, title: str, block: str) -> Section:
    code_blocks = [
        code.strip()
        for code in re.findall(r"```(?:[^\n`]*)\n([\s\S]*?)\n```", block)
        if code.strip()
    ]
    text_without_code = re.sub(
        r"```(?:[^\n`]*)\n[\s\S]*?\n```", "", block
    ).strip()
    return Section(
        label=label,
        title=title.strip(),
        hint="",
        solution=text_without_code,
        code_blocks=code_blocks,
        raw_text=block.strip(),
    )


def parse_old_problem_sections(markdownish: str) -> list[Section]:
    heading_re = re.compile(
        r"(?im)(?:^|\n)\s*(?:Problem|Porblem)\s+([A-Z](?:\d+)?)\b\s*"
    )
    matches = list(heading_re.finditer(markdownish))
    sections: list[Section] = []
    for idx, match in enumerate(matches):
        label = match.group(1).upper()
        title_start = match.end()
        block_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(markdownish)
        block = markdownish[title_start:block_end].strip()
        if not block:
            continue
        lines = block.splitlines()
        first = lines[0].strip() if lines else ""
        if first and len(first) <= 120 and not first.endswith("."):
            title = first
            body = "\n".join(lines[1:]).strip()
        else:
            title = f"Problem {label}"
            body = block
        sections.append(_section_from_block(label, title, body or block))
    return sections


def _candidate_quality_key(candidate: EditorialCandidate) -> tuple[int, int]:
    text = candidate.extracted_text()
    preferred_source = 0 if candidate.source_kind == "dynamic" else 1
    enough_text = 0 if len(text) >= MIN_EDITORIAL_LEN else 1
    return (enough_text, preferred_source)


def _repair_concatenated_heading(section: Section, problem_title: str) -> Section:
    if section.raw_text.strip() or section.hint.strip() or section.solution.strip():
        return section
    expected_title = (problem_title or "").strip()
    parsed_title = section.title.strip()
    if not expected_title or not parsed_title.startswith(expected_title):
        return section
    body = parsed_title[len(expected_title):].strip()
    if len(body) < MIN_EDITORIAL_LEN:
        return section
    return Section(
        label=section.label,
        title=expected_title,
        hint="",
        solution=body,
        code_blocks=section.code_blocks,
        raw_text=body,
    )


def collect_candidates_for_blog(
    *,
    pid: str,
    problem_title: str,
    tutorial_url: str,
    fetcher: str,
    pw_wait_ms: int,
    per_blog_limit: int = 4,
) -> tuple[str, list[EditorialCandidate]]:
    _, target_index = parse_pid(pid)
    target_index = target_index.upper()
    tutorial_html = fetch_html(tutorial_url, fetcher=fetcher, pw_wait_ms=pw_wait_ms)
    tutorial_title = extract_page_title(tutorial_html)

    candidates: list[EditorialCandidate] = []
    dynamic_title, dynamic_text = fetch_dynamic_editorial(
        tutorial_url=tutorial_url,
        tutorial_html=tutorial_html,
        problem_code=pid,
    )
    if dynamic_text.strip():
        candidates.append(
            EditorialCandidate(
                candidate_id="",
                tutorial_url=tutorial_url,
                tutorial_title=tutorial_title,
                source_kind="dynamic",
                label=target_index,
                title=dynamic_title or problem_title,
                section=Section(
                    label=target_index,
                    title=dynamic_title or problem_title,
                    hint="",
                    solution=dynamic_text.strip(),
                    code_blocks=[],
                    raw_text=dynamic_text.strip(),
                ),
            )
        )

    body_html = extract_blog_body_html(tutorial_html)
    markdownish = html_to_markdownish(body_html)
    try:
        parsed_sections = parse_sections(markdownish)
    except ScrapeError:
        parsed_sections = []

    for section in parsed_sections:
        if section.label.upper() == target_index:
            section = _repair_concatenated_heading(section, problem_title)
            candidates.append(
                EditorialCandidate(
                    candidate_id="",
                    tutorial_url=tutorial_url,
                    tutorial_title=tutorial_title,
                    source_kind="section",
                    label=section.label,
                    title=section.title,
                    section=section,
                )
            )

    if not candidates:
        for section in parse_old_problem_sections(markdownish):
            if section.label.upper() != target_index:
                continue
            candidates.append(
                EditorialCandidate(
                    candidate_id="",
                    tutorial_url=tutorial_url,
                    tutorial_title=tutorial_title,
                    source_kind="old_problem_section",
                    label=section.label,
                    title=section.title,
                    section=section,
                )
            )
            if len(candidates) >= per_blog_limit:
                break

    if not candidates:
        for legacy_title, block in parse_legacy_title_sections(markdownish):
            candidates.append(
                EditorialCandidate(
                    candidate_id="",
                    tutorial_url=tutorial_url,
                    tutorial_title=tutorial_title,
                    source_kind="legacy_section",
                    label=target_index,
                    title=legacy_title,
                    section=_section_from_block(target_index, legacy_title, block),
                )
            )
            if len(candidates) >= per_blog_limit:
                break

    if not candidates and markdownish.strip():
        candidates.append(
            EditorialCandidate(
                candidate_id="",
                tutorial_url=tutorial_url,
                tutorial_title=tutorial_title,
                source_kind="whole_blog",
                label=target_index,
                title=tutorial_title or problem_title,
                section=_section_from_block(target_index, tutorial_title or problem_title, markdownish),
            )
        )

    candidates = sorted(candidates, key=_candidate_quality_key)
    return tutorial_title, candidates[:per_blog_limit]


def collect_candidates(
    *,
    pid: str,
    fetcher: str,
    pw_wait_ms: int,
    blog_limit: int,
) -> tuple[str, str, list[EditorialCandidate]]:
    problem_url = build_problem_url_from_pid(pid)
    problem_html = fetch_html(problem_url, fetcher=fetcher, pw_wait_ms=pw_wait_ms)
    problem_title = extract_problem_title(problem_html)
    blog_urls = extract_blog_links(problem_html, problem_url, limit=blog_limit)
    if not blog_urls:
        raise AgentNoMatch("problem_page_has_no_blog_entry_links")

    all_candidates: list[EditorialCandidate] = []
    for blog_url in blog_urls:
        try:
            _, candidates = collect_candidates_for_blog(
                pid=pid,
                problem_title=problem_title,
                tutorial_url=blog_url,
                fetcher=fetcher,
                pw_wait_ms=pw_wait_ms,
            )
        except ScrapeError:
            continue
        all_candidates.extend(candidates)

    if not all_candidates:
        raise AgentNoMatch("no_parseable_candidates_from_blog_entries")

    numbered: list[EditorialCandidate] = []
    for idx, candidate in enumerate(all_candidates, start=1):
        numbered.append(
            EditorialCandidate(
                candidate_id=f"c{idx}",
                tutorial_url=candidate.tutorial_url,
                tutorial_title=candidate.tutorial_title,
                source_kind=candidate.source_kind,
                label=candidate.label,
                title=candidate.title,
                section=candidate.section,
            )
        )
    return problem_url, problem_title, numbered


def _build_selector_messages(
    *,
    pid: str,
    problem_title: str,
    problem_text: str,
    candidates: list[EditorialCandidate],
    llm_text_limit: int,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for candidate in candidates:
        rows.append(
            {
                "id": candidate.candidate_id,
                "url": candidate.tutorial_url,
                "blog_title": candidate.tutorial_title,
                "source_kind": candidate.source_kind,
                "section_label": candidate.label,
                "section_title": candidate.title,
                "text": candidate.llm_text(llm_text_limit),
            }
        )

    payload = {
        "pid": pid,
        "problem_title": problem_title,
        "problem": problem_text,
        "candidates": rows,
    }
    return [
        {
            "role": "system",
            "content": (
                "你是 Codeforces 官方题解 oncall。你会收到一道题的题面和若干从 "
                "Codeforces blog 抓到的候选片段。你的任务只是选择哪个候选片段是在讲这道题。"
                "不要生成题解，不要改写候选内容。"
                "只输出 JSON 对象："
                "{\"match\":true,\"candidate_id\":\"c1\",\"confidence\":0.0到1.0,\"reason\":\"...\"} "
                "或 {\"match\":false,\"reason\":\"...\"}。"
                "判断时比较题意目标、输入输出、关键变量、算法对象、约束和候选中的结论。"
                "如果只是同一个题号字母但题意明显不同，必须 match=false。"
                "如果候选只是代码、作者信息、目录、占位或太短，必须 match=false。"
                "只有候选足以作为官方题解/教程正文时才 match=true。"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


async def select_candidate(
    *,
    pid: str,
    problem_title: str,
    problem_text: str,
    candidates: list[EditorialCandidate],
    timeout: int,
    llm_text_limit: int,
) -> tuple[EditorialCandidate, float, str]:
    result = await chat_completion(
        _build_selector_messages(
            pid=pid,
            problem_title=problem_title,
            problem_text=problem_text,
            candidates=candidates,
            llm_text_limit=llm_text_limit,
        ),
        task="summary",
        temperature=0.0,
        timeout=timeout,
        response_format={"type": "json_object"},
        send_reasoning_effort=False,
    )
    if not result.text:
        raise AgentNoMatch(f"selector_llm_failed:{result.failure_kind}")
    parsed = robust_json_parse(result.text)
    if parsed.get("match") is not True:
        raise AgentNoMatch(str(parsed.get("reason") or "selector_no_match"))
    candidate_id = str(parsed.get("candidate_id") or "").strip()
    try:
        confidence = float(parsed.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    reason = str(parsed.get("reason") or "").strip()
    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    candidate = by_id.get(candidate_id)
    if not candidate:
        raise AgentNoMatch(f"selector_unknown_candidate:{candidate_id}")
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
        problem_url, problem_title, candidates = await asyncio.to_thread(
            collect_candidates,
            pid=pid,
            fetcher=fetcher,
            pw_wait_ms=pw_wait_ms,
            blog_limit=blog_limit,
        )
        selected, confidence, reason = await select_candidate(
            pid=pid,
            problem_title=problem_title or str(stmt.get("name") or ""),
            problem_text=problem_text,
            candidates=candidates,
            timeout=selector_timeout_sec,
            llm_text_limit=llm_text_limit,
        )
        if confidence < confidence_threshold:
            raise AgentNoMatch(
                f"selector_low_confidence:{confidence:.2f}:{reason or selected.candidate_id}"
            )
        elapsed = time.monotonic() - started
        bundle = build_bundle(
            pid=pid,
            problem_url=problem_url,
            selected=selected,
            confidence=confidence,
            reason=reason,
            candidate_count=len(candidates),
            elapsed_sec=elapsed,
        )
        return AgentResult(
            pid=pid,
            bundle=bundle,
            selected_candidate_id=selected.candidate_id,
            confidence=confidence,
            reason=reason,
            elapsed_sec=elapsed,
            candidate_count=len(candidates),
        )

    try:
        return await asyncio.wait_for(_run(), timeout=max(1, deadline_sec))
    except asyncio.TimeoutError as exc:
        raise AgentNoMatch(f"deadline_exceeded:{deadline_sec}s") from exc

