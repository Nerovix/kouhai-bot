#!/usr/bin/env python3
"""
每日一题 — 选题+发题+揭晓
Usage:
  python3 daily_picker.py pick          # 选题并打印题目描述
  python3 daily_picker.py post          # 选题并生成发群消息
  python3 daily_picker.py reveal        # 揭晓前一天的题目
"""

import hashlib
import html
import json
import os
import random
import re
import sys

# Import cf_statement for VL-powered formula processing
sys.path.insert(0, os.path.dirname(__file__))
import fetcher as cf_statement
import time
from datetime import datetime, timezone, timedelta

import cloudscraper

# ── Config ──────────────────────────────────────────────────────────────

STATE_DIR = os.path.expanduser("~/.kouhai-bot")
CACHE_DIR = os.path.join(STATE_DIR, "statements")
GROUPS_DIR = os.path.join(STATE_DIR, "groups")
os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(GROUPS_DIR, exist_ok=True)

# Per-group state: override with --group <id>
CURRENT_GROUP = "default"


def _set_group(group_id: str):
    global CURRENT_GROUP
    CURRENT_GROUP = str(group_id)


def _state_file() -> str:
    return os.path.join(GROUPS_DIR, CURRENT_GROUP, "state.json")


def _used_file() -> str:
    return os.path.join(GROUPS_DIR, CURRENT_GROUP, "used.json")


def _set_rating_range(min_rating: int, max_rating: int):
    global RATING_MIN, RATING_MAX
    RATING_MIN = int(min_rating)
    RATING_MAX = int(max_rating)


TZ = timezone(timedelta(hours=8))
RATING_MIN = 2000
RATING_MAX = 2600
CACHE_TTL = 3600 * 24  # refresh cache once per day

CF_API = "https://codeforces.com/api/problemset.problems"

# ── Used problems tracking ─────────────────────────────────────────────


def _load_used() -> set:
    if os.path.exists(_used_file()):
        with open(_used_file()) as f:
            return set(json.load(f))
    return set()


def _save_used(used: set):
    with open(_used_file(), "w") as f:
        json.dump(sorted(used), f)


def _problem_id(p: dict) -> str:
    return f"{p['contestId']}{p['index']}"


# ── Selection ───────────────────────────────────────────────────────────

def _cache_all_path() -> str:
    return os.path.join(STATE_DIR, f"cf_all_{RATING_MIN}_{RATING_MAX}.json")


_SCRAPER = None


def _get_scraper():
    """Lazy-init a shared cloudscraper instance."""
    global _SCRAPER
    if _SCRAPER is None:
        _SCRAPER = cloudscraper.create_scraper()
    return _SCRAPER


def _fetch_and_cache_targets() -> list[dict]:
    """Fetch the full problem set from CF and cache problems in the active rating range."""
    scraper = _get_scraper()
    resp = scraper.get(CF_API, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data["status"] != "OK":
        raise RuntimeError(f"CF API error: {data}")

    targets = []
    for p in data["result"]["problems"]:
        r = p.get("rating")
        tags = p.get("tags", [])
        if r is not None and RATING_MIN <= r <= RATING_MAX and "*special" not in tags:
            targets.append(p)

    with open(_cache_all_path(), "w") as f:
        json.dump(targets, f)
    return targets


def _get_cached_targets() -> list[dict]:
    """Get cached target problems, fetch if stale or missing."""
    cache_all_path = _cache_all_path()
    if os.path.exists(cache_all_path):
        mtime = os.path.getmtime(cache_all_path)
        if time.time() - mtime < CACHE_TTL:
            with open(cache_all_path) as f:
                return json.load(f)
    return _fetch_and_cache_targets()


def select_problem() -> dict:
    """
    Randomly select an unused problem within rating range.
    If all problems have been used, resets the used set.
    """
    problems = _get_cached_targets()
    if not problems:
        raise RuntimeError(f"No problems found in range {RATING_MIN}-{RATING_MAX}")

    used = _load_used()

    # Filter to unused problems
    available = [p for p in problems if _problem_id(p) not in used]
    if not available:
        # All used — reset
        _save_used(set())
        used = set()
        available = problems

    return random.choice(available)


# ── Problem statement via CF website ────────────────────────────────────


def _cache_path(pid: str) -> str:
    return os.path.join(CACHE_DIR, f"{pid}.json")


def _normalize_sample_block(raw_html: str) -> str:
    """Convert CF sample HTML block to plain text with newlines."""
    text = raw_html or ""
    # CF has two sample styles:
    # - classic <br />
    # - split lines wrapped by <div class="test-example-line ...">
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</div>\s*<div[^>]*>", "\n", text)
    text = re.sub(r"(?i)<div[^>]*>", "", text)
    text = re.sub(r"(?i)</div>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\r", "")
    # Keep internal blank lines if any; trim only edges and trailing spaces.
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and lines[0] == "":
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _normalize_samples(samples: list[dict]) -> tuple[list[dict], bool]:
    """Normalize sample input/output blocks and report if changed."""
    changed = False
    normalized: list[dict] = []
    for sample in samples:
        raw_in = sample.get("input", "") if isinstance(sample, dict) else ""
        raw_out = sample.get("output", "") if isinstance(sample, dict) else ""
        clean_in = _normalize_sample_block(raw_in)
        clean_out = _normalize_sample_block(raw_out)
        if clean_in != raw_in or clean_out != raw_out:
            changed = True
        normalized.append({"input": clean_in, "output": clean_out})
    return normalized, changed


def fetch_statement(problem: dict) -> object:
    """
    Fetch full problem statement from Codeforces using cf_statement with Qwen-VL.
    Formula images are converted to LaTeX text via VL; non-formula diagrams are filtered.
    Returns dict with keys: name, time_limit, memory_limit, description,
    input, samples, notes. Or None on failure / diagram problem.
    Caches locally so we only process each problem once.
    """
    contest_id = problem.get("contestId")
    index = problem.get("index")
    pid = f"{contest_id}{index}"
    cache_file = _cache_path(pid)

    # Check cache first
    if os.path.exists(cache_file):
        with open(cache_file) as f:
            cached = json.load(f)
        # Backward compatibility: clean old cached sample HTML format.
        cached_changed = False
        cached_samples = cached.get("samples")
        if isinstance(cached_samples, list):
            normalized_samples, changed = _normalize_samples(cached_samples)
            if changed:
                cached["samples"] = normalized_samples
                cached_changed = True
        # Detect stale cache (created before VL pipeline existed) —
        # dry-run to check for images, re-process with VL if needed
        if not cached.get("_vl_processed"):
            try:
                dry = cf_statement.process_problem(contest_id, index, vl_backend="none")
                has_images = dry.get("formulas_found", 0) + dry.get("graphics_found", 0) > 0
                if has_images:
                    print(f"[{pid}] stale cache (no VL), re-fetching with Qwen-VL",
                          file=sys.stderr)
                    os.remove(cache_file)
                    # Fall through to re-process below
                else:
                    # Text-only problem, mark as processed and use as-is
                    cached["_vl_processed"] = True
                    with open(cache_file, "w") as f:
                        json.dump(cached, f, ensure_ascii=False)
                    return cached
            except Exception as e:
                print(f"[{pid}] dry-run failed ({e}), using cached version", file=sys.stderr)
                if cached_changed:
                    with open(cache_file, "w") as f:
                        json.dump(cached, f, ensure_ascii=False)
                return cached
        else:
            if cached_changed:
                with open(cache_file, "w") as f:
                    json.dump(cached, f, ensure_ascii=False)
            return cached

    # Step 1: Process problem with Qwen-VL for formula recognition
    cf_result = cf_statement.process_problem(contest_id, index, vl_backend="qwen")

    if "error" in cf_result:
        print(f"Warning: {pid} cf_statement error: {cf_result['error']}", file=sys.stderr)
        return None

    # Filter: skip problems with non-formula images (diagrams)
    if cf_result.get("has_non_formula_images"):
        print(f"Warning: {pid} has non-formula images (diagrams), skipping", file=sys.stderr)
        return None

    # Filter: skip if any formula failed after retries
    if cf_result.get("formulas_failed", 0) > 0:
        print(f"Warning: {pid} has {cf_result['formulas_failed']} failed formula(s), skipping",
              file=sys.stderr)
        return None

    # Step 2: Fetch raw HTML for metadata (title, limits, samples)
    scraper = _get_scraper()
    url = f"https://codeforces.com/problemset/problem/{contest_id}/{index}"
    try:
        resp = scraper.get(url, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"Warning: failed to fetch {url}: {e}", file=sys.stderr)
        return None
    html = resp.text

    result = {}

    # Problem name (from <div class="title">)
    m = re.search(r'<div class="title">([^<]+)</div>', html)
    if m:
        result["name"] = m.group(1).strip()

    # Time/memory limits
    m = re.search(r'(\d+\.?\d*)\s*seconds?\b', html, re.I)
    if m:
        result["time_limit"] = m.group(1) + "s"
    m = re.search(r'(\d+)\s*megabytes?\b', html, re.I)
    if m:
        result["memory_limit"] = m.group(1) + "MB"

    # Description: use VL-processed text (formulas converted to LaTeX inline)
    desc = cf_result.get("text", "")
    result["description"] = desc

    # Extract Input spec from raw HTML
    inp_m = re.search(
        r'<div class="section-title">Input</div>\s*(.*?)(?=<div class="section-title"|</div>\s*<script)',
        html,
        re.DOTALL,
    )
    if inp_m:
        inp = re.sub(r'<[^>]+>', '', inp_m.group(1))
        inp = re.sub(r'\s+', ' ', inp).strip()
        inp = re.sub(r'\$\$\$|\$\$|\$', '', inp)
        result["input"] = inp

    # Extract samples from <pre> blocks in raw HTML
    ps_m = re.search(
        r'<div class="problem-statement"[^>]*>([\s\S]*?)</div>\s*<script',
        html,
    )
    if ps_m:
        pres = re.findall(r'<pre>([\s\S]*?)</pre>', ps_m.group(1))
        samples = []
        for i in range(0, len(pres) - 1, 2):
            samples.append({
                "input": _normalize_sample_block(pres[i]),
                "output": _normalize_sample_block(pres[i + 1]),
            })
        if samples:
            result["samples"] = samples

    # Extract Note
    if ps_m:
        note_m = re.search(
            r'<div class="section-title">Note</div>([\s\S]*?)(?=<div class="section-title"|$)',
            ps_m.group(1),
            re.DOTALL,
        )
        if note_m:
            note = re.sub(r'<[^>]+>', '', note_m.group(1))
            note = re.sub(r'\s+', ' ', note).strip()
            note = re.sub(r'\$\$\$|\$\$|\$', '', note)
            result["notes"] = note

    # Sanity: reject if description contains raw image URLs
    if re.search(r'https?://\S+\.(png|jpg|jpeg|gif)', desc, re.I):
        print(f"Warning: {pid} contains image URL in rendered text, skipping", file=sys.stderr)
        return None

    # Cache and return
    result["_vl_processed"] = True
    with open(cache_file, "w") as f:
        json.dump(result, f, ensure_ascii=False)

    print(f"[{pid}] statement cached ({len(desc)} chars)", file=sys.stderr)
    return result


# ── Problem pick & format ───────────────────────────────────────────────

def _state_from_problem(problem: dict) -> dict:
    pid = _problem_id(problem)
    return {
        "today": pid,
        "contestId": problem["contestId"],
        "index": problem["index"],
        "rating": problem.get("rating", "?"),
        "name": problem.get("name", ""),
        "tags": problem.get("tags", []),
        "date": datetime.now(TZ).strftime("%Y-%m-%d"),
    }


def write_state_for_problem(problem: dict) -> dict:
    state = _state_from_problem(problem)
    with open(_state_file(), "w") as f:
        json.dump(state, f)
    return state


def pick(with_statement: bool = False, write_state: bool = True) -> dict:
    """Pick today's problem, mark as used, return the problem dict.
    If with_statement and fetch_statement returns None (image problem, fetch error),
    retry up to 10 times, marking failed picks as used."""
    MAX_RETRIES = 10
    for attempt in range(MAX_RETRIES):
        problem = select_problem()
        pid = _problem_id(problem)

        if with_statement:
            stmt = fetch_statement(problem)
            if stmt is None:
                used = _load_used()
                used.add(pid)
                _save_used(used)
                print(f"pick: retry {attempt+1}/{MAX_RETRIES}, skipping {pid}", file=sys.stderr)
                continue

        used = _load_used()
        used.add(pid)
        _save_used(used)
        break
    else:
        raise RuntimeError(f"Failed to find a valid problem after {MAX_RETRIES} retries")

    if write_state:
        write_state_for_problem(problem)

    return problem


def format_problem_for_qq(p: dict) -> str:
    """Format a problem as a brief QQ-friendly description (no markdown/latex)."""
    rating = p.get("rating", "?")
    tags = p.get("tags", [])
    name = p.get("name", "Unknown")
    contest_id = p.get("contestId", "?")
    index = p.get("index", "?")

    return f"CF{contest_id}{index} — {name} (rating {rating})"


def post() -> str:
    """Generate the daily post message (just greeting, no problem info)."""
    problem = pick(with_statement=True)
    msg = (
        f"中午好呀☀️ 今天的每日一题来咯～\n\n"
        f"欢迎@我交流做法～💪"
    )
    return msg


def reveal() -> str:
    """Reveal yesterday's problem."""
    if not os.path.exists(_state_file()):
        return "还没有发过题哦"

    with open(_state_file()) as f:
        state = json.load(f)

    cf_id = state.get("today", "?")
    name = state.get("name", "")
    rating = state.get("rating", "")
    parts = [f"CF{cf_id}"]
    if name:
        parts.append(name)
    if rating:
        parts.append(f"{rating}")
    return f"上一道题来自 {' '.join(parts)}✨"


# ── CLI ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Parse --group <id>, --min-rating <n>, --max-rating <n>, or --flag=value
    group_id = "default"
    min_rating = RATING_MIN
    max_rating = RATING_MAX
    skip_next = False
    cmd_args = []
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if a == "--group" and i + 1 < len(argv):
            group_id = argv[i + 1]
            skip_next = True
            continue
        if a == "--min-rating" and i + 1 < len(argv):
            min_rating = int(argv[i + 1])
            skip_next = True
            continue
        if a == "--max-rating" and i + 1 < len(argv):
            max_rating = int(argv[i + 1])
            skip_next = True
            continue
        if a.startswith("--group="):
            group_id = a.split("=", 1)[1]
            continue
        if a.startswith("--min-rating="):
            min_rating = int(a.split("=", 1)[1])
            continue
        if a.startswith("--max-rating="):
            max_rating = int(a.split("=", 1)[1])
            continue
        if a.startswith("--"):
            continue
        cmd_args.append(a)

    _set_group(group_id)
    _set_rating_range(min_rating, max_rating)
    os.makedirs(os.path.join(GROUPS_DIR, group_id), exist_ok=True)

    if len(cmd_args) < 1:
        print("Usage: daily_picker.py [--group <id>] [--min-rating <n>] [--max-rating <n>] pick|post|reveal")
        sys.exit(1)

    cmd = cmd_args[0]
    try:
        if cmd in {"pick", "pick-json"}:
            with_stmt = "--with-statement" in sys.argv
            write_state = "--no-write-state" not in sys.argv and cmd != "pick-json"
            p = pick(with_statement=with_stmt, write_state=write_state)
            if cmd == "pick-json":
                print(json.dumps(_state_from_problem(p), ensure_ascii=False))
                sys.exit(0)
            print(format_problem_for_qq(p))
            if with_stmt:
                pid = _problem_id(p)
                cache_file = _cache_path(pid)
                if os.path.exists(cache_file):
                    with open(cache_file) as f:
                        stmt = json.load(f)
                    print("\n" + "=" * 40)
                    print("Problem Statement:")
                    print("=" * 40)
                    if stmt.get("description"):
                        print(stmt["description"])
                    if stmt.get("input"):
                        print("\nInput:")
                        print(stmt["input"])
                    if stmt.get("output"):
                        print("\nOutput:")
                        print(stmt["output"])
                    if stmt.get("samples"):
                        print("\nSamples:")
                        for i, s in enumerate(stmt["samples"]):
                            print(f"  #{i+1} Input: {s['input'][:80]}")
                            print(f"     Output: {s['output'][:80]}")
                    if stmt.get("notes"):
                        print("\nNotes:")
                        print(stmt["notes"][:500])
        elif cmd == "post":
            print(post())
        elif cmd == "reveal":
            print(reveal())
        elif cmd == "statement":
            # Print cached statement for today's problem
            if not os.path.exists(_state_file()):
                print("还没有发过题哦")
                sys.exit(1)
            with open(_state_file()) as f:
                state = json.load(f)
            pid = state.get("today", "")
            cache_file = _cache_path(pid)
            if os.path.exists(cache_file):
                with open(cache_file) as f:
                    stmt = json.load(f)
                print(json.dumps(stmt, indent=2, ensure_ascii=False))
            else:
                print(f"题目 {pid} 的题面还未缓存")
        else:
            print(f"Unknown command: {cmd}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
