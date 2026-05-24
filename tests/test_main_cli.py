"""Tests for CLI status output in kouhai_bot.main."""

import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.main import _print_start_status


def test_print_start_status_for_start_omits_stopped_existing():
    out = io.StringIO()
    with redirect_stdout(out):
        _print_start_status(
            port=8097,
            action="start",
            already_running=False,
            started=True,
            pid=123,
            log_path=Path("/tmp/test.log"),
        )

    text = out.getvalue()
    assert "action=start" in text
    assert "already_running=no" in text
    assert "started=yes" in text
    assert "stopped_existing=" not in text


def test_print_start_status_for_restart_omits_already_running():
    out = io.StringIO()
    with redirect_stdout(out):
        _print_start_status(
            port=8097,
            action="restart",
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
