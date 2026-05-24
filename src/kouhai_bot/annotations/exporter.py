"""Build structured annotation bundles from solved judge history."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from ..config import get_config
from ..handlers.shared import get_problem_summary, load_scoreboard
from .store import PENDING, bundle_exists, save_bundle

logger = logging.getLogger("kouhai-bot.annotations")


def _now_iso() -> str:
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).isoformat()


def _statements_dir() -> Path:
    return Path(get_config().data_dir) / "statements"


def _groups_dir() -> Path:
    return Path(get_config().data_dir) / "groups"


def _load_statement(pid: str) -> dict[str, Any]:
    stmt_file = _statements_dir() / f"{pid}.json"
    if not stmt_file.exists():
        return {}
    with open(stmt_file, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _user_nicknames(scoreboard: dict[str, Any]) -> dict[str, str]:
    nicknames: dict[str, str] = {}
    for entry in scoreboard.get("solves", []):
        uid = str(entry.get("user_id"))
        if uid and uid not in nicknames:
            nicknames[uid] = entry.get("nickname", uid) or uid
    return nicknames


def _history_item(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": record.get("timestamp", ""),
        "result": record.get("result", ""),
        "submission": record.get("content", ""),
        "reason": record.get("reason", ""),
        "reply": record.get("reply", ""),
        "problem": record.get("problem", ""),
    }


def _collect_rounds_for_problem(scoreboard: dict[str, Any], pid: str) -> list[dict[str, Any]]:
    nicknames = _user_nicknames(scoreboard)
    rounds: list[dict[str, Any]] = []

    for uid, records in scoreboard.get("user_submissions", {}).items():
        history_for_problem: list[dict[str, Any]] = []
        round_index = 0
        for record in records:
            if record.get("problem") != pid:
                continue

            history_before = [_history_item(item) for item in history_for_problem]
            history_for_problem.append(record)

            verdict = record.get("result", "")
            if verdict not in {"correct", "incorrect"}:
                continue

            round_index += 1
            rounds.append({
                "round_id": f"{pid}:{uid}:{round_index}",
                "user_id": int(uid) if str(uid).isdigit() else uid,
                "nickname": nicknames.get(str(uid), str(uid)),
                "timestamp": record.get("timestamp", ""),
                "submission": record.get("content", ""),
                "model_verdict": verdict,
                "reason": record.get("reason", ""),
                "reply": record.get("reply", ""),
                "history_before": history_before,
                "human_label": {
                    "expected_verdict": None,
                    "comment": "",
                    "labeler": "",
                    "labeled_at": "",
                },
            })

    rounds.sort(key=lambda item: (item.get("timestamp", ""), str(item.get("user_id", "")), item.get("round_id", "")))
    return rounds


def _first_solve_entry(scoreboard: dict[str, Any], pid: str) -> dict[str, Any] | None:
    matches = [entry for entry in scoreboard.get("solves", []) if entry.get("problem") == pid]
    if not matches:
        return None
    matches.sort(key=lambda item: item.get("order", 1 << 30))
    return matches[0]


def collect_problem_annotation_bundle(group_id: int, problem_id: str, source: str = "manual") -> dict[str, Any] | None:
    scoreboard = load_scoreboard(group_id)
    first_solve = _first_solve_entry(scoreboard, problem_id)
    if not first_solve:
        return None

    rounds = _collect_rounds_for_problem(scoreboard, problem_id)
    if not rounds:
        return None

    statement = _load_statement(problem_id)
    summary_zh = get_problem_summary(group_id, problem_id)
    if summary_zh and not statement.get("summary_zh"):
        statement = dict(statement)
        statement["summary_zh"] = summary_zh
    first_correct = next((item for item in rounds if item.get("model_verdict") == "correct"), None)

    return {
        "version": 1,
        "group_id": int(group_id),
        "problem_id": problem_id,
        "problem_name": statement.get("name", problem_id),
        "source": source,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "first_solver": {
            "user_id": first_solve.get("user_id", ""),
            "nickname": first_solve.get("nickname", ""),
            "order": first_solve.get("order", 0),
            "date": first_solve.get("date", ""),
        },
        "first_correct_timestamp": first_correct.get("timestamp", "") if first_correct else "",
        "statement": statement,
        "rounds": rounds,
    }


def export_problem_annotation_bundle(group_id: int, problem_id: str, source: str = "auto_on_first_solve") -> Path | None:
    if bundle_exists(group_id, problem_id):
        return None

    bundle = collect_problem_annotation_bundle(group_id, problem_id, source=source)
    if not bundle:
        return None

    return save_bundle(bundle, PENDING)


def _iter_group_ids() -> list[int]:
    base = _groups_dir()
    if not base.exists():
        return []
    group_ids: list[int] = []
    for path in base.iterdir():
        if path.is_dir() and path.name.isdigit():
            group_ids.append(int(path.name))
    return sorted(group_ids)


def _solved_problem_ids(group_id: int) -> list[str]:
    scoreboard = load_scoreboard(group_id)
    ordered: list[str] = []
    seen: set[str] = set()
    solves = sorted(scoreboard.get("solves", []), key=lambda item: item.get("order", 1 << 30))
    for entry in solves:
        pid = entry.get("problem", "")
        if pid and pid not in seen:
            seen.add(pid)
            ordered.append(pid)
    return ordered


def sync_annotation_bundles(group_id: int | None = None) -> list[Path]:
    created: list[Path] = []
    group_ids = [group_id] if group_id is not None else _iter_group_ids()
    for gid in group_ids:
        for pid in _solved_problem_ids(gid):
            try:
                path = export_problem_annotation_bundle(gid, pid, source="backfill")
            except Exception as e:
                logger.warning("failed to export annotation bundle for group=%s pid=%s: %s", gid, pid, e)
                continue
            if path is not None:
                created.append(path)
    return created
