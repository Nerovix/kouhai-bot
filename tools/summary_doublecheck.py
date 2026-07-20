#!/usr/bin/env python3
"""Run the general-model semantic checker against a saved problem summary."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from kouhai_bot.config import get_config
from kouhai_bot.handlers.shared import (
    doublecheck_problem_summary,
    load_problem_statement_json,
)
from kouhai_bot.llm import strip_leaked_thinking


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise RuntimeError(f"expected a JSON object in {path}")
    return data


def _statement_inputs(statement: dict) -> tuple[str, str, str]:
    return (
        str(statement.get("description", "") or ""),
        str(statement.get("input", "") or ""),
        (
            f"Time: {statement.get('time_limit', '?')}, "
            f"Memory: {statement.get('memory_limit', '?')}"
        ),
    )


async def _run(args: argparse.Namespace) -> int:
    cfg = get_config()
    group_id = args.group if args.group is not None else cfg.current_group
    group_dir = Path(cfg.data_dir) / "groups" / str(group_id)
    state = _load_json(group_dir / "state.json")
    pid = str(args.pid or state.get("today", "") or "").strip().upper()
    if not pid:
        raise RuntimeError(f"group {group_id} has no current problem; pass --pid")

    statement = load_problem_statement_json(pid)
    if not statement:
        raise RuntimeError(f"statement cache not found for {pid}")

    if args.summary_file:
        summary = args.summary_file.read_text(encoding="utf-8").strip()
    else:
        saved = _load_json(group_dir / "problem_summaries.json").get(pid)
        if isinstance(saved, dict):
            summary = str(saved.get("summary_zh", "") or "").strip()
        else:
            summary = str(saved or "").strip()
        summary = strip_leaked_thinking(summary)
    if not summary:
        raise RuntimeError(
            f"saved summary not found for group {group_id}, problem {pid}; "
            "pass --summary-file"
        )

    result = await doublecheck_problem_summary(*_statement_inputs(statement), summary)
    payload = {
        "group_id": group_id,
        "pid": pid,
        "accurate": result.accurate,
        "issues": list(result.issues),
        "failure_kind": result.failure_kind,
        "provider_name": result.provider_name,
        "model": result.model,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if result.accurate is None:
        return 2
    if args.expect == "accurate":
        return 0 if result.accurate else 1
    if args.expect == "inaccurate":
        return 0 if not result.accurate else 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Use llm.general_model to compare a saved Chinese summary with its "
            "cached Codeforces statement. No runtime data is modified."
        )
    )
    parser.add_argument("--group", type=int, help="group id (default: current_group)")
    parser.add_argument("--pid", help="problem id (default: the group's current problem)")
    parser.add_argument(
        "--summary-file",
        type=Path,
        help="UTF-8 candidate summary file (default: group's saved summary)",
    )
    parser.add_argument(
        "--expect",
        choices=("accurate", "inaccurate"),
        help="exit nonzero unless the checker returns this verdict",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"summary double-check harness failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
