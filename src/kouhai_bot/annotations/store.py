"""Persistent storage for human-annotation bundles."""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from ..config import get_config

ANNOTATION_VERSION = 1
PENDING = "pending"
LABELED = "labeled"
VALID_STATUSES = {PENDING, LABELED}


def _now_iso() -> str:
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).isoformat()


def annotation_root() -> Path:
    return Path(get_config().data_dir) / "annotations"


def _status_dir(status: str) -> Path:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid annotation status: {status}")
    return annotation_root() / status


def bundle_path(status: str, group_id: int, problem_id: str) -> Path:
    return _status_dir(status) / str(group_id) / f"{problem_id}.json"


def bundle_exists(group_id: int, problem_id: str) -> bool:
    return any(
        bundle_path(status, group_id, problem_id).exists()
        for status in VALID_STATUSES
    )


def load_bundle(group_id: int, problem_id: str, status: str | None = None) -> tuple[dict[str, Any] | None, str | None]:
    statuses = [status] if status else [PENDING, LABELED]
    for candidate in statuses:
        path = bundle_path(candidate, group_id, problem_id)
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("version", ANNOTATION_VERSION)
        data["status"] = candidate
        return data, candidate
    return None, None


def save_bundle(bundle: dict[str, Any], status: str) -> Path:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid annotation status: {status}")

    group_id = int(bundle["group_id"])
    problem_id = str(bundle["problem_id"])
    out = dict(bundle)
    out["version"] = ANNOTATION_VERSION
    out["status"] = status
    out["updated_at"] = _now_iso()

    path = bundle_path(status, group_id, problem_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    other_status = LABELED if status == PENDING else PENDING
    other_path = bundle_path(other_status, group_id, problem_id)
    if other_path.exists():
        other_path.unlink()

    return path


def _label_progress(bundle: dict[str, Any]) -> tuple[int, int]:
    rounds = bundle.get("rounds", [])
    labeled = 0
    for item in rounds:
        label = item.get("human_label", {})
        if label.get("expected_verdict") in {"correct", "incorrect"}:
            labeled += 1
    return labeled, len(rounds)


def _statement_preview(statement: dict[str, Any]) -> str:
    desc = statement.get("description", "") or ""
    desc = desc.strip()
    if not desc:
        return ""
    if len(desc) > 140:
        return desc[:140] + "…"
    return desc


def summarize_bundle(bundle: dict[str, Any], status: str) -> dict[str, Any]:
    labeled, total = _label_progress(bundle)
    statement = bundle.get("statement", {})
    return {
        "group_id": int(bundle["group_id"]),
        "problem_id": str(bundle["problem_id"]),
        "problem_name": bundle.get("problem_name", ""),
        "status": status,
        "created_at": bundle.get("created_at", ""),
        "updated_at": bundle.get("updated_at", bundle.get("created_at", "")),
        "source": bundle.get("source", ""),
        "round_count": total,
        "labeled_count": labeled,
        "progress": f"{labeled}/{total}",
        "problem_preview": _statement_preview(statement),
    }


def list_bundle_summaries(status: str | None = None, group_id: int | None = None) -> list[dict[str, Any]]:
    statuses = [status] if status else [PENDING, LABELED]
    summaries: list[dict[str, Any]] = []
    for candidate in statuses:
        base = _status_dir(candidate)
        if not base.exists():
            continue
        roots = [base / str(group_id)] if group_id is not None else [p for p in base.iterdir() if p.is_dir()]
        for group_dir in roots:
            if not group_dir.exists():
                continue
            for path in sorted(group_dir.glob("*.json")):
                with open(path, encoding="utf-8") as f:
                    bundle = json.load(f)
                summaries.append(summarize_bundle(bundle, candidate))

    summaries.sort(
        key=lambda item: (item.get("status", ""), item.get("updated_at", ""), item.get("problem_id", "")),
        reverse=True,
    )
    return summaries
