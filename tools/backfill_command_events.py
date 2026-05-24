#!/usr/bin/env python3
"""Backfill command event logs from existing scoreboard history."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.eventlog import TZ
from kouhai_bot.eventlog_backfill import backfill_command_events


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(TZ)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill groups/<gid>/command_events/*.jsonl from scoreboard user_submissions.",
    )
    parser.add_argument("--group", type=int, action="append", dest="groups",
                        help="Group ID to backfill. Repeatable. Defaults to all groups under data_dir.")
    parser.add_argument("--days", type=int, default=2,
                        help="Backfill the last N days when --since is omitted. Default: 2.")
    parser.add_argument("--since", help="Inclusive ISO datetime lower bound.")
    parser.add_argument("--until", help="Exclusive ISO datetime upper bound.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Count records without writing JSONL.")
    args = parser.parse_args()

    summary = backfill_command_events(
        group_ids=args.groups,
        since=_parse_dt(args.since),
        until=_parse_dt(args.until),
        days=args.days,
        dry_run=args.dry_run,
    )
    mode = "dry-run" if args.dry_run else "written"
    print(
        f"{mode}: groups={summary.groups} "
        f"records_seen={summary.records_seen} "
        f"records_written={summary.records_written} "
        f"events_written={summary.events_written}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
