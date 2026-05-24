"""Backfill command event logs from existing scoreboard history."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .config import get_config
from .eventlog import MAX_TEXT_PREVIEW, TZ, event_file, load_events
from .handlers.shared import load_scoreboard


BACKFILL_SOURCE = "backfill_scoreboard"


@dataclass
class BackfillSummary:
    groups: int = 0
    records_seen: int = 0
    records_written: int = 0
    events_written: int = 0


def _parse_timestamp(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value).astimezone(TZ)
    except Exception:
        return None


def _event_dates(start: datetime, end: datetime) -> set[str]:
    dates: set[str] = set()
    day = start.astimezone(TZ).date()
    last = end.astimezone(TZ).date()
    while day <= last:
        dates.add(day.isoformat())
        day += timedelta(days=1)
    return dates


def _preview(text: str) -> str:
    text = text.replace("\r", "\n")
    if len(text) <= MAX_TEXT_PREVIEW:
        return text
    return text[:MAX_TEXT_PREVIEW] + "..."


def _command_for_record(record: dict[str, Any]) -> str:
    result = str(record.get("result", ""))
    if result in {"correct", "incorrect"}:
        return "submit"
    if result in {"clarify", "review"}:
        return result
    return ""


def _status_for_record(record: dict[str, Any]) -> str:
    result = str(record.get("result", ""))
    if result in {"correct", "incorrect"}:
        return result
    if result in {"clarify", "review"}:
        return "ok"
    return result or "ok"


def _source_key(group_id: int, user_id: int, record: dict[str, Any]) -> str:
    payload = json.dumps({
        "group_id": group_id,
        "user_id": user_id,
        "timestamp": record.get("timestamp", ""),
        "result": record.get("result", ""),
        "problem": record.get("problem", ""),
        "content": record.get("content", ""),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _request_id(group_id: int, user_id: int, command: str, dt: datetime, source_key: str) -> str:
    ms = int(dt.timestamp() * 1000)
    return f"backfill-g{group_id}-u{user_id}-{command}-{ms}-{source_key[:8]}"


def _nickname_by_user(scoreboard: dict[str, Any]) -> dict[str, str]:
    names: dict[str, str] = {}
    for item in scoreboard.get("solves", []):
        uid = str(item.get("user_id", ""))
        nickname = str(item.get("nickname", "")) or uid
        if uid:
            names[uid] = nickname
    return names


def _existing_source_keys(group_id: int, dates: set[str]) -> set[str]:
    keys: set[str] = set()
    for date in dates:
        for item in load_events(group_id, date):
            source_key = item.get("source_key")
            if item.get("source") == BACKFILL_SOURCE and source_key:
                keys.add(str(source_key))
    return keys


def _append_event(group_id: int, item: dict[str, Any]) -> None:
    path = event_file(group_id, str(item["date"]))
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")


def _build_events(
    *,
    group_id: int,
    user_id: int,
    nickname: str,
    record: dict[str, Any],
) -> list[dict[str, Any]] | None:
    command = _command_for_record(record)
    if not command:
        return None

    dt = _parse_timestamp(str(record.get("timestamp", "")))
    if not dt:
        return None

    problem = str(record.get("problem", ""))
    content = str(record.get("content", ""))
    raw_text = f"/{command} {content}".rstrip()
    source_key = _source_key(group_id, user_id, record)
    request_id = _request_id(group_id, user_id, command, dt, source_key)
    date = dt.strftime("%Y-%m-%d")
    base = {
        "source": BACKFILL_SOURCE,
        "source_key": source_key,
        "synthetic": True,
    }
    received = {
        **base,
        "type": "received",
        "request_id": request_id,
        "timestamp": dt.isoformat(),
        "date": date,
        "group_id": group_id,
        "user_id": user_id,
        "nickname": nickname or str(user_id),
        "command": command,
        "message_id": "",
        "problem": problem,
        "raw_text_len": len(raw_text),
        "raw_text_preview": _preview(raw_text),
    }
    finished_at = dt + timedelta(milliseconds=1)
    finished = {
        **base,
        "type": "finished",
        "request_id": request_id,
        "timestamp": finished_at.isoformat(),
        "date": finished_at.strftime("%Y-%m-%d"),
        "group_id": group_id,
        "user_id": user_id,
        "command": command,
        "status": _status_for_record(record),
        "problem": problem,
        "elapsed_ms": 0,
    }
    return [received, finished]


def _group_ids_from_data_dir() -> list[int]:
    groups_dir = os.path.join(get_config().data_dir, "groups")
    if not os.path.isdir(groups_dir):
        return []
    result: list[int] = []
    for name in os.listdir(groups_dir):
        if name.isdigit():
            result.append(int(name))
    return sorted(result)


def backfill_command_events(
    *,
    group_ids: list[int] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    days: int = 2,
    dry_run: bool = False,
) -> BackfillSummary:
    """Backfill command_events JSONL from scoreboard user_submissions."""
    now = datetime.now(TZ)
    end = (until or now).astimezone(TZ)
    start = (since or (end - timedelta(days=days))).astimezone(TZ)
    dates = _event_dates(start, end)
    groups = group_ids or _group_ids_from_data_dir()
    summary = BackfillSummary(groups=len(groups))

    for group_id in groups:
        scoreboard = load_scoreboard(group_id)
        names = _nickname_by_user(scoreboard)
        existing = _existing_source_keys(group_id, dates)

        for uid_text, records in scoreboard.get("user_submissions", {}).items():
            try:
                user_id = int(uid_text)
            except Exception:
                continue
            if not isinstance(records, list):
                continue

            for record in records:
                if not isinstance(record, dict):
                    continue
                dt = _parse_timestamp(str(record.get("timestamp", "")))
                if not dt or not (start <= dt < end):
                    continue
                summary.records_seen += 1
                source_key = _source_key(group_id, user_id, record)
                if source_key in existing:
                    continue
                events = _build_events(
                    group_id=group_id,
                    user_id=user_id,
                    nickname=names.get(str(user_id), str(user_id)),
                    record=record,
                )
                if not events:
                    continue
                summary.records_written += 1
                summary.events_written += len(events)
                existing.add(source_key)
                if dry_run:
                    continue
                for event in events:
                    _append_event(group_id, event)

    return summary
