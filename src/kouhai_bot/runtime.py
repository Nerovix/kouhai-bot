"""Shared runtime bootstrap helpers."""

from __future__ import annotations

import logging
import sys

from .editorial_followup import schedule_prefetch_for_current_group
from .handlers.registry import discover_commands
from .scheduler.jobs import register_builtin_jobs

_BOOTSTRAPPED = False


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )


def bootstrap_runtime() -> None:
    """Load command handlers and scheduler jobs once per process."""
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    discover_commands()
    register_builtin_jobs()
    schedule_prefetch_for_current_group()
    _BOOTSTRAPPED = True
