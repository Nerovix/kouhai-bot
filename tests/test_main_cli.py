"""CLI surface tests for kouhai_bot.main.

The console-script entry points (start/restart/stop/status) are plain
functions registered in pyproject.toml; their behavior is covered in
test_main.py. Here we pin the machine-readable ``_print_status`` output
contract (the replacement for the removed ``_print_start_status``) and the
``kouhai-bot`` entry point's dispatch into the worker.
"""

import io
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

from kouhai_bot import main


def test_print_status_for_start_omits_stopped_existing():
    out = io.StringIO()
    with redirect_stdout(out):
        main._print_status(
            action="start",
            app_id="1900000000",
            already_running=False,
            started=True,
            pid=123,
            log_path=Path("/tmp/test.log"),
        )

    text = out.getvalue()
    assert "action=start" in text
    assert "app_id=1900000000" in text
    assert "already_running=no" in text
    assert "started=yes" in text
    assert "pid=123" in text
    assert "log=/tmp/test.log" in text
    assert "stopped_existing=" not in text
    assert "occupied=" not in text


def test_print_status_for_restart_omits_already_running():
    out = io.StringIO()
    with redirect_stdout(out):
        main._print_status(
            action="restart",
            app_id="1900000000",
            stopped_existing=True,
            started=False,
            pid=456,
            log_path=Path("/tmp/test.log"),
        )

    text = out.getvalue()
    assert "action=restart" in text
    assert "stopped_existing=yes" in text
    assert "started=no" in text
    assert "already_running=" not in text


def test_print_status_for_status_omits_pid_when_idle():
    out = io.StringIO()
    with redirect_stdout(out):
        main._print_status(
            action="status",
            app_id="1900000000",
            occupied=False,
            current_worktree_running="no",
            pid=None,
        )

    text = out.getvalue()
    assert "action=status" in text
    assert "occupied=no" in text
    assert "current_worktree_running=no" in text
    assert "pid=" not in text
    assert "log=" not in text


def test_print_status_field_order_is_stable():
    out = io.StringIO()
    with redirect_stdout(out):
        main._print_status(
            action="restart",
            app_id="1900000000",
            stopped_existing=True,
            started=True,
            pid=456,
            log_path=Path("/tmp/test.log"),
        )

    assert out.getvalue().splitlines() == [
        "action=restart",
        "app_id=1900000000",
        "stopped_existing=yes",
        "started=yes",
        "pid=456",
        "log=/tmp/test.log",
    ]


def test_main_entry_dispatches_to_worker_main_async():
    worker_main = AsyncMock()
    with patch("kouhai_bot.main.worker_main_async", worker_main):
        main.main()
    worker_main.assert_awaited_once()