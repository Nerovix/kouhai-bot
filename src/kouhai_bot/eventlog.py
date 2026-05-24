"""Structured command event logging.

The event log is append-only JSONL, partitioned by the event's real local date.
Logical reporting windows such as 04:00-to-04:00 should be computed by readers.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

from .config import get_config
from .context import get_display_name

logger = logging.getLogger("kouhai-bot.eventlog")
TZ = timezone(timedelta(hours=8))
MAX_TEXT_PREVIEW = 200
EVENT_META_KEY = "_command_event_log"


def now_tz() -> datetime:
    return datetime.now(TZ)


def event_date(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%Y-%m-%d")


def _events_dir(group_id: int) -> str:
    cfg = get_config()
    d = os.path.join(cfg.data_dir, "groups", str(group_id), "command_events")
    os.makedirs(d, exist_ok=True)
    return d


def event_file(group_id: int, date: str) -> str:
    return os.path.join(_events_dir(group_id), f"{date}.jsonl")


def _append(group_id: int, item: dict[str, Any]) -> None:
    path = event_file(group_id, item["date"])
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")


def _preview(text: str) -> str:
    text = text.replace("\r", "\n")
    if len(text) <= MAX_TEXT_PREVIEW:
        return text
    return text[:MAX_TEXT_PREVIEW] + "..."


def _request_id(group_id: int, user_id: int, command: str, dt: datetime) -> str:
    ms = int(dt.timestamp() * 1000)
    suffix = uuid.uuid4().hex[:8]
    return f"g{group_id}-u{user_id}-{command}-{ms}-{suffix}"


def _current_problem_id(group_id: int) -> str:
    if not group_id:
        return ""
    try:
        from .handlers.shared import get_today_problem
        problem = get_today_problem(group_id)
        return problem.get("today", "") if problem else ""
    except Exception:
        return ""


def log_command_received(
    *,
    group_id: int,
    user_id: int,
    sender: dict,
    command: str,
    message_id: str,
    raw_text: str,
) -> dict[str, Any]:
    dt = now_tz()
    date = event_date(dt)
    request_id = _request_id(group_id, user_id, command, dt)
    item = {
        "type": "received",
        "request_id": request_id,
        "timestamp": dt.isoformat(),
        "date": date,
        "group_id": group_id,
        "user_id": user_id,
        "nickname": get_display_name(sender),
        "command": command,
        "message_id": str(message_id),
        "problem": _current_problem_id(group_id),
        "raw_text_len": len(raw_text),
        "raw_text_preview": _preview(raw_text),
    }
    try:
        _append(group_id, item)
    except Exception as e:
        logger.warning("failed to append received command event: %s", e)
    return {
        "request_id": request_id,
        "received_at": dt.isoformat(),
        "received_monotonic": time.monotonic(),
        "date": date,
        "group_id": group_id,
        "user_id": user_id,
        "command": command,
        "problem": item["problem"],
        "finished_logged": False,
    }


def log_command_finished(
    event_meta: dict[str, Any] | None,
    *,
    status: str,
    problem: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    if not event_meta or event_meta.get("finished_logged"):
        return

    dt = now_tz()
    received_monotonic = event_meta.get("received_monotonic")
    elapsed_ms = None
    if isinstance(received_monotonic, (int, float)):
        elapsed_ms = int((time.monotonic() - float(received_monotonic)) * 1000)

    item: dict[str, Any] = {
        "type": "finished",
        "request_id": event_meta["request_id"],
        "timestamp": dt.isoformat(),
        "date": event_date(dt),
        "group_id": event_meta["group_id"],
        "user_id": event_meta["user_id"],
        "command": event_meta["command"],
        "status": status,
        "problem": problem or event_meta.get("problem", ""),
    }
    if elapsed_ms is not None:
        item["elapsed_ms"] = elapsed_ms
    if extra:
        item.update(extra)

    try:
        _append(int(event_meta["group_id"]), item)
    except Exception as e:
        logger.warning("failed to append finished command event: %s", e)
    event_meta["finished_logged"] = True


def load_events(group_id: int, date: str) -> list[dict[str, Any]]:
    path = event_file(group_id, date)
    if not os.path.exists(path):
        return []
    items: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return items
