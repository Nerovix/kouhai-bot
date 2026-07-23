#!/usr/bin/env python3
"""Codeforces 官方题解：批量爬取、质量检查、可选 LLM 校验。

子命令:
  crawl    statements/*.json → tutorials/<pid>.json（质量不合格重试）
  validate 扫描 tutorials/ 生成无效名单
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from kouhai_bot.config import get_config
from kouhai_bot.handlers.shared import robust_json_parse, translate_editorial_to_zh
from kouhai_bot.llm import chat_completion
from kouhai_bot.editorial_content import (
    MIN_EDITORIAL_LEN,
    extract_editorial,
    is_placeholder as _is_placeholder,
)

from cf_tutorial_agent import AgentNoMatch
from cf_tutorial_agent import DEFAULT_CANDIDATE_LIMIT
from cf_tutorial_agent import DEFAULT_CONFIDENCE_THRESHOLD
from cf_tutorial_agent import DEFAULT_DEADLINE_SEC
from cf_tutorial_agent import DEFAULT_LLM_TEXT_LIMIT
from cf_tutorial_agent import DEFAULT_SELECTOR_TIMEOUT_SEC
from cf_tutorial_agent import load_statement
from cf_tutorial_agent import run_agent_for_pid
from cf_tutorial_agent import statement_to_text
from scrape_cf_tutorial import ScrapeError
from scrape_cf_tutorial import list_statement_pids

DEFAULT_STATEMENTS_DIR = str(ROOT / "statements")
DEFAULT_TUTORIALS_DIR = str(ROOT / "tutorials")

_VALIDATE_SYSTEM = """你是 Codeforces 官方题解质量审核员。
判断每条抓取内容是否为「可用于竞赛学习的有效官方题解」。

有效：包含该题的算法思路、关键观察、做法步骤或复杂度分析（可含代码片段）。
无效：仅占位（Tutorial is loading）、仅作者/出题人信息、仅题目标题、仅「某某的 solution」无正文、
比赛公告/目录、与算法无关的闲聊、内容过短无法指导解题、明显抓错段落。

对每条返回 valid=true/false 和简短中文 reason（无效时说明原因）。"""


# ---------------------------------------------------------------------------
# Quality (matches runtime get_official_editorial)
# ---------------------------------------------------------------------------


def tutorial_quality_reason(bundle: dict | None) -> str | None:
    if not bundle or not isinstance(bundle, dict):
        return "invalid_bundle"
    sections = bundle.get("sections") or []
    if not sections or not isinstance(sections[0], dict):
        return "no_sections"
    sec = sections[0]
    text = extract_editorial(sec)
    if len(text) >= MIN_EDITORIAL_LEN:
        return None
    hint = (sec.get("hint") or "").strip()
    sol = (sec.get("solution") or "").strip()
    raw = (sec.get("raw_text") or "").strip()
    fields = (hint, sol, raw)
    if any(_is_placeholder(x) for x in fields if x):
        if any(
            "tutorial is loading" in x.lower() or "will be added soon" in x.lower()
            for x in fields
            if x
        ):
            return "loading_placeholder"
        return "placeholder_only"
    return "too_short"


def is_tutorial_quality_ok(bundle: dict | None) -> bool:
    return tutorial_quality_reason(bundle) is None


def load_tutorial_bundle(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Crawl
# ---------------------------------------------------------------------------


@dataclass
class CrawlOutcome:
    pid: str
    status: str
    attempts: int = 0
    quality_reason: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {"pid": self.pid, "status": self.status, "attempts": self.attempts}
        if self.quality_reason:
            row["quality_reason"] = self.quality_reason
        if self.error:
            row["error"] = self.error
        return row


def _write_tutorial(path: Path, bundle: dict, *, pretty: bool) -> None:
    payload = json.dumps(bundle, ensure_ascii=False, indent=2 if pretty else None)
    path.write_text(payload + ("\n" if pretty else ""), encoding="utf-8")


async def _translate_agent_bundle(
    *,
    pid: str,
    bundle: dict,
    statements_dir: Path,
    tutorials_dir: Path,
    timeout: int,
) -> tuple[bool, str]:
    sections = bundle.get("sections") or []
    if not sections or not isinstance(sections[0], dict):
        return False, "no_section"
    editorial_text = extract_editorial(sections[0])
    if len(editorial_text) < MIN_EDITORIAL_LEN:
        return False, "editorial_too_short"
    stmt = load_statement(statements_dir / f"{pid}.json")
    problem_text = statement_to_text(stmt)
    try:
        translated, model_tag, matched = await asyncio.wait_for(
            translate_editorial_to_zh(
                editorial_text,
                pid=pid,
                problem_text=problem_text,
            ),
            timeout=max(1, timeout),
        )
    except asyncio.TimeoutError:
        return False, "translation_timeout"
    if matched is False:
        return False, "translation_mismatch"
    if not translated or len(translated.strip()) < MIN_EDITORIAL_LEN:
        return False, "translation_empty"

    cache_dir = tutorials_dir.parent / "tutorial_translations"
    cache_dir.mkdir(parents=True, exist_ok=True)
    full_text = (translated.strip() + model_tag).strip() if model_tag else translated.strip()
    (cache_dir / f"{pid}.txt").write_text(full_text, encoding="utf-8")
    (cache_dir / f"{pid}.verified").write_text("", encoding="utf-8")
    no_editorial = cache_dir / f"{pid}.no_editorial"
    if no_editorial.is_file():
        no_editorial.unlink()
    return True, "translated"


def _run_agent_sync(
    pid: str,
    *,
    statements_dir: Path,
    tutorials_dir: Path,
    fetcher: str,
    pw_wait_ms: int,
    pretty: bool,
    blog_limit: int,
    deadline_sec: int,
    selector_timeout_sec: int,
    confidence_threshold: float,
    llm_text_limit: int,
    translate: bool,
    translate_timeout_sec: int,
) -> CrawlOutcome:
    out_path = tutorials_dir / f"{pid}.json"
    try:
        result = asyncio.run(
            run_agent_for_pid(
                pid=pid,
                statements_dir=statements_dir,
                fetcher=fetcher,
                pw_wait_ms=max(0, pw_wait_ms),
                blog_limit=max(1, blog_limit),
                deadline_sec=max(1, deadline_sec),
                selector_timeout_sec=max(1, selector_timeout_sec),
                confidence_threshold=max(0.0, min(1.0, confidence_threshold)),
                llm_text_limit=max(800, llm_text_limit),
            )
        )
    except AgentNoMatch as exc:
        if out_path.is_file():
            out_path.unlink()
        return CrawlOutcome(pid=pid, status="agent_no_match", attempts=1, error=str(exc))
    except ScrapeError as exc:
        if out_path.is_file():
            out_path.unlink()
        return CrawlOutcome(pid=pid, status="scrape_failed", attempts=1, error=str(exc))

    reason = tutorial_quality_reason(result.bundle) or ""
    if reason:
        if out_path.is_file():
            out_path.unlink()
        return CrawlOutcome(
            pid=pid,
            status="quality_failed",
            attempts=1,
            quality_reason=reason,
        )

    _write_tutorial(out_path, result.bundle, pretty=pretty)

    status = "agent_ok"
    if translate:
        translated, translate_reason = asyncio.run(
            _translate_agent_bundle(
                pid=pid,
                bundle=result.bundle,
                statements_dir=statements_dir,
                tutorials_dir=tutorials_dir,
                timeout=translate_timeout_sec,
            )
        )
        if translated:
            status = "agent_ok_translated"
        else:
            print(f"[TRANSLATE_SKIP] {pid} reason={translate_reason}")

    print(
        f"[AGENT_SELECT] {pid} candidate={result.selected_candidate_id} "
        f"confidence={result.confidence:.2f} candidates={result.candidate_count} "
        f"elapsed={result.elapsed_sec:.1f}s reason={result.reason}"
    )
    return CrawlOutcome(pid=pid, status=status, attempts=1)



def cmd_crawl(args: argparse.Namespace) -> None:
    statements_dir = Path(args.statements_dir)
    tutorials_dir = Path(args.tutorials_dir)
    tutorials_dir.mkdir(parents=True, exist_ok=True)

    os.environ["SCRAPE_REQUEST_WAIT_SECONDS"] = str(max(0.0, args.request_wait_seconds))

    try:
        pids = list_statement_pids(str(statements_dir))
    except ScrapeError as exc:
        raise SystemExit(str(exc)) from exc

    if args.limit > 0:
        pids = pids[: args.limit]

    outcomes: list[CrawlOutcome] = []
    counts: dict[str, int] = {}

    print(
        f"[INFO] statements={statements_dir} tutorials={tutorials_dir} "
        f"total={len(pids)} mode=agent "
        f"skip_existing={args.skip_existing} force={args.force}"
    )

    for idx, pid in enumerate(pids):
        out_path = tutorials_dir / f"{pid}.json"
        print(f"[PROGRESS] {idx + 1}/{len(pids)} pid={pid}")

        if out_path.is_file() and not args.force:
            if args.skip_existing:
                outcome = CrawlOutcome(pid=pid, status="skipped_exists", attempts=0)
                outcomes.append(outcome)
                counts[outcome.status] = counts.get(outcome.status, 0) + 1
                print(f"[SKIP] {pid} (file exists)")
                continue
            existing = load_tutorial_bundle(out_path)
            if is_tutorial_quality_ok(existing):
                outcome = CrawlOutcome(pid=pid, status="skipped_ok", attempts=0)
                outcomes.append(outcome)
                counts[outcome.status] = counts.get(outcome.status, 0) + 1
                print(f"[SKIP] {pid} (quality ok)")
                continue

        try:
            outcome = _run_agent_sync(
                pid,
                statements_dir=statements_dir,
                tutorials_dir=tutorials_dir,
                fetcher=args.fetcher,
                pw_wait_ms=max(0, args.pw_wait_ms),
                pretty=args.pretty,
                blog_limit=args.candidate_limit,
                deadline_sec=args.deadline_sec,
                selector_timeout_sec=args.selector_timeout_sec,
                confidence_threshold=args.confidence_threshold,
                llm_text_limit=args.llm_text_limit,
                translate=args.translate,
                translate_timeout_sec=args.translate_timeout_sec,
            )
        except Exception as exc:
            outcome = CrawlOutcome(
                pid=pid,
                status="scrape_failed",
                attempts=0,
                error=str(exc),
            )
            print(f"[FAIL] {pid}: {exc}")

        outcomes.append(outcome)
        counts[outcome.status] = counts.get(outcome.status, 0) + 1

        tag = outcome.status.upper()
        if outcome.status.startswith("ok") or outcome.status.startswith("agent_ok"):
            print(f"[{tag}] {pid} -> {out_path} attempts={outcome.attempts}")
        else:
            detail = outcome.quality_reason or outcome.error
            print(f"[{tag}] {pid} {detail}")

        if idx < len(pids) - 1 and args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

    summary = {
        "statements_dir": str(statements_dir.resolve()),
        "tutorials_dir": str(tutorials_dir.resolve()),
        "total": len(pids),
        "counts": counts,
        "quality_failed": [o.as_dict() for o in outcomes if o.status == "quality_failed"],
        "agent_no_match": [o.as_dict() for o in outcomes if o.status == "agent_no_match"],
        "scrape_failed": [o.as_dict() for o in outcomes if o.status == "scrape_failed"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def cmd_agent(args: argparse.Namespace) -> None:
    statements_dir = Path(args.statements_dir)
    tutorials_dir = Path(args.tutorials_dir)
    tutorials_dir.mkdir(parents=True, exist_ok=True)
    os.environ["SCRAPE_REQUEST_WAIT_SECONDS"] = str(max(0.0, args.request_wait_seconds))

    outcome = _run_agent_sync(
        args.pid,
        statements_dir=statements_dir,
        tutorials_dir=tutorials_dir,
        fetcher=args.fetcher,
        pw_wait_ms=max(0, args.pw_wait_ms),
        pretty=args.pretty,
        blog_limit=args.candidate_limit,
        deadline_sec=args.deadline_sec,
        selector_timeout_sec=args.selector_timeout_sec,
        confidence_threshold=args.confidence_threshold,
        llm_text_limit=args.llm_text_limit,
        translate=args.translate,
        translate_timeout_sec=args.translate_timeout_sec,
    )
    out_path = tutorials_dir / f"{args.pid}.json"
    print(json.dumps({
        **outcome.as_dict(),
        "output": str(out_path) if out_path.is_file() else "",
    }, ensure_ascii=False, indent=2))
    if not outcome.status.startswith("agent_ok"):
        raise SystemExit(2)


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TutorialItem:
    pid: str
    path: Path
    bundle: dict

    @property
    def section(self) -> dict | None:
        sections = self.bundle.get("sections") or []
        return sections[0] if sections else None

    def extracted_text(self) -> str:
        sec = self.section
        if not sec:
            return ""
        return extract_editorial(sec)

    def heuristic_reason(self) -> str | None:
        if "_load_error" in self.bundle:
            return f"json_error: {self.bundle['_load_error']}"
        return tutorial_quality_reason(self.bundle)


def _load_validate_items(tutorials_dir: Path) -> list[TutorialItem]:
    items: list[TutorialItem] = []
    for fp in sorted(tutorials_dir.glob("*.json")):
        try:
            bundle = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            items.append(
                TutorialItem(pid=fp.stem, path=fp, bundle={"_load_error": str(e)})
            )
            continue
        items.append(TutorialItem(pid=fp.stem, path=fp, bundle=bundle))
    return items


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n...(已截断)"


def _build_batch_user_msg(batch: list[TutorialItem], text_limit: int) -> str:
    blocks: list[str] = []
    for item in batch:
        sec = item.section or {}
        title = (sec.get("title") or item.bundle.get("tutorial_title") or "").strip()
        label = (sec.get("label") or item.pid).strip()
        body = _truncate(item.extracted_text(), text_limit)
        blocks.append(
            f"### {item.pid} (section {label})\n"
            f"title: {title or '(none)'}\n"
            f"problem_url: {item.bundle.get('problem_url', '')}\n"
            f"---\n{body or '(empty)'}"
        )
    return (
        "请审核以下抓取题解，输出 JSON："
        '{"results":[{"pid":"...","valid":true|false,"reason":"..."}, ...]}\n\n'
        + "\n\n".join(blocks)
    )


async def _llm_validate_batch(
    batch: list[TutorialItem],
    *,
    text_limit: int,
    timeout: int,
) -> dict[str, tuple[bool, str]]:
    user_msg = _build_batch_user_msg(batch, text_limit)
    result = await chat_completion(
        [
            {"role": "system", "content": _VALIDATE_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        task="summary",
        temperature=0.0,
        timeout=timeout,
        response_format={"type": "json_object"},
    )
    if not result.text:
        raise RuntimeError(f"LLM failed: {result.failure_kind}")
    parsed = robust_json_parse(result.text)
    rows = parsed.get("results")
    if not isinstance(rows, list):
        raise ValueError(f"unexpected LLM JSON: {result.text[:300]}")
    out: dict[str, tuple[bool, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        pid = str(row.get("pid", "")).strip()
        if not pid:
            continue
        valid = bool(row.get("valid"))
        reason = str(row.get("reason", "")).strip() or ("ok" if valid else "invalid")
        out[pid] = (valid, reason)
    return out


async def _run_llm_validation(
    candidates: list[TutorialItem],
    *,
    batch_size: int,
    concurrency: int,
    text_limit: int,
    timeout: int,
    progress_path: Path | None,
) -> dict[str, tuple[bool, str]]:
    sem = asyncio.Semaphore(max(1, concurrency))
    results: dict[str, tuple[bool, str]] = {}
    if progress_path and progress_path.is_file():
        try:
            saved = json.loads(progress_path.read_text(encoding="utf-8"))
            for row in saved.get("llm_done", []):
                if isinstance(row, dict) and row.get("pid"):
                    results[str(row["pid"])] = (
                        bool(row.get("valid")),
                        str(row.get("reason", "")),
                    )
        except (OSError, json.JSONDecodeError):
            pass

    pending = [it for it in candidates if it.pid not in results]
    batches = [pending[i : i + batch_size] for i in range(0, len(pending), batch_size)]

    async def _one(batch: list[TutorialItem]) -> None:
        async with sem:
            verdicts = await _llm_validate_batch(
                batch, text_limit=text_limit, timeout=timeout
            )
            for item in batch:
                valid, reason = verdicts.get(item.pid, (False, "llm_missing_verdict"))
                results[item.pid] = (valid, reason)
            if progress_path:
                progress_path.write_text(
                    json.dumps(
                        {
                            "llm_done": [
                                {"pid": p, "valid": v, "reason": r}
                                for p, (v, r) in sorted(results.items())
                            ]
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )

    await asyncio.gather(*[_one(b) for b in batches])
    return results


def cmd_validate(args: argparse.Namespace) -> None:
    tutorials_dir = Path(args.tutorials_dir)
    if not tutorials_dir.is_dir():
        raise SystemExit(f"目录不存在: {tutorials_dir}")

    get_config()
    items = _load_validate_items(tutorials_dir)
    invalid: list[dict] = []
    heuristic_valid: list[TutorialItem] = []

    for item in items:
        if "_load_error" in item.bundle:
            invalid.append(
                {
                    "pid": item.pid,
                    "source": "heuristic",
                    "reason": f"json_error: {item.bundle['_load_error']}",
                }
            )
            continue
        reason = item.heuristic_reason()
        if reason:
            invalid.append({"pid": item.pid, "source": "heuristic", "reason": reason})
        else:
            heuristic_valid.append(item)

    llm_invalid: list[dict] = []
    if not args.heuristic_only and heuristic_valid:
        subset = heuristic_valid
        if args.llm_limit > 0:
            subset = subset[: args.llm_limit]
        print(
            f"LLM 校验 {len(subset)} / {len(heuristic_valid)} 条启发式通过项 "
            f"(batch={args.batch_size}, concurrency={args.concurrency})..."
        )
        llm_results = asyncio.run(
            _run_llm_validation(
                subset,
                batch_size=max(1, args.batch_size),
                concurrency=max(1, args.concurrency),
                text_limit=max(500, args.text_limit),
                timeout=max(30, args.timeout),
                progress_path=Path(args.progress) if args.progress else None,
            )
        )
        for item in subset:
            valid, reason = llm_results.get(item.pid, (False, "llm_missing_verdict"))
            if not valid:
                llm_invalid.append({"pid": item.pid, "source": "llm", "reason": reason})
        invalid.extend(llm_invalid)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tutorials_dir": str(tutorials_dir.resolve()),
        "total": len(items),
        "heuristic_invalid_count": sum(1 for x in invalid if x["source"] == "heuristic"),
        "llm_invalid_count": len(llm_invalid),
        "invalid_count": len(invalid),
        "valid_count": len(items) - len(invalid),
        "invalid": sorted(invalid, key=lambda x: x["pid"]),
    }
    out_path = Path(args.output)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"完成: total={report['total']} invalid={report['invalid_count']} "
        f"(heuristic={report['heuristic_invalid_count']} llm={report['llm_invalid_count']})"
    )
    print(f"名单已写入: {out_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _add_crawl_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("crawl", help="根据 statements 题号用 LLM harness 爬取题解到 tutorials")
    p.add_argument("--statements-dir", default=DEFAULT_STATEMENTS_DIR)
    p.add_argument("--tutorials-dir", default=DEFAULT_TUTORIALS_DIR)
    p.add_argument(
        "--skip-existing",
        action="store_true",
        help="若 tutorials 中已有题解文件则跳过（不论质量）",
    )
    p.add_argument("--force", action="store_true", help="即使已有文件也重新爬取")
    p.add_argument("--sleep-seconds", type=float, default=10.0)
    p.add_argument("--request-wait-seconds", type=float, default=10.0)
    p.add_argument("--fetcher", choices=["auto", "http", "playwright"], default="auto")
    p.add_argument("--pw-wait-ms", type=int, default=7000)
    p.add_argument("--pretty", action="store_true")
    p.add_argument("--limit", type=int, default=0, help="仅处理前 N 题（0=全部）")
    _add_agent_options(p)
    p.set_defaults(func=cmd_crawl)


def _add_agent_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--candidate-limit", type=int, default=DEFAULT_CANDIDATE_LIMIT)
    p.add_argument("--deadline-sec", type=int, default=DEFAULT_DEADLINE_SEC)
    p.add_argument("--selector-timeout-sec", type=int, default=DEFAULT_SELECTOR_TIMEOUT_SEC)
    p.add_argument("--translate-timeout-sec", type=int, default=75)
    p.add_argument("--confidence-threshold", type=float, default=DEFAULT_CONFIDENCE_THRESHOLD)
    p.add_argument("--llm-text-limit", type=int, default=DEFAULT_LLM_TEXT_LIMIT)
    p.add_argument("--translate", action="store_true", help="题解 JSON 成功后额外写中文翻译缓存")


def _add_agent_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("agent", help="单题轻量 LLM harness 爬取官方题解")
    p.add_argument("--pid", required=True, help="题号，如 542D")
    p.add_argument("--statements-dir", default=DEFAULT_STATEMENTS_DIR)
    p.add_argument("--tutorials-dir", default=DEFAULT_TUTORIALS_DIR)
    p.add_argument("--request-wait-seconds", type=float, default=0.0)
    p.add_argument("--fetcher", choices=["auto", "http", "playwright"], default="auto")
    p.add_argument("--pw-wait-ms", type=int, default=7000)
    p.add_argument("--pretty", action="store_true")
    _add_agent_options(p)
    p.set_defaults(func=cmd_agent)


def _add_validate_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("validate", help="校验 tutorials 目录，输出无效名单")
    p.add_argument("--tutorials-dir", default=DEFAULT_TUTORIALS_DIR)
    p.add_argument(
        "--output",
        default=str(ROOT / "tutorial_validation_invalid.json"),
    )
    p.add_argument(
        "--progress",
        default=str(ROOT / "tutorial_validation_progress.json"),
        help="LLM 进度缓存（可断点续跑）",
    )
    p.add_argument("--heuristic-only", action="store_true")
    p.add_argument("--batch-size", type=int, default=4)
    p.add_argument("--concurrency", type=int, default=6)
    p.add_argument("--text-limit", type=int, default=3500)
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--llm-limit", type=int, default=0)
    p.set_defaults(func=cmd_validate)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Codeforces 官方题解爬取与校验",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    _add_crawl_parser(sub)
    _add_agent_parser(sub)
    _add_validate_parser(sub)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
