import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot import main


def test_bot_log_path_uses_group_and_local_date(tmp_path):
    tz = timezone(timedelta(hours=8))
    expected_date = datetime.now(tz).strftime("%Y-%m-%d")

    path = main._bot_log_path(123456, str(tmp_path))

    assert path == tmp_path / "logs" / "123456" / f"{expected_date}.log"


def test_start_reports_already_running_without_spawning():
    cfg = SimpleNamespace(napcat_ws_port=8097, current_group=123456, data_dir="/tmp/data")
    printed = []

    with patch("kouhai_bot.main.get_config", return_value=cfg), \
            patch("kouhai_bot.main._port_has_listener", return_value=True), \
            patch("builtins.print", side_effect=printed.append):
        main.start()

    assert printed == [
        "action=start",
        "port=8097",
        "already_running=yes",
        "started=no",
    ]


def test_restart_reports_detached_start_status(tmp_path):
    cfg = SimpleNamespace(napcat_ws_port=8096, current_group=123456789, data_dir=str(tmp_path))
    log_path = tmp_path / "logs" / "123456789" / "2026-05-16.log"
    printed = []

    with patch("kouhai_bot.main.get_config", return_value=cfg), \
            patch("kouhai_bot.main._stop_existing_instance_on_port", return_value=True), \
            patch("kouhai_bot.main._spawn_detached_bot", return_value=(4321, log_path)), \
            patch("kouhai_bot.main._wait_for_port_bind", return_value=True), \
            patch("builtins.print", side_effect=printed.append):
        main.restart()

    assert printed == [
        "action=restart",
        "port=8096",
        "stopped_existing=yes",
        "started=yes",
        "pid=4321",
        f"log={log_path}",
    ]


def test_stop_reports_if_instance_was_stopped():
    cfg = SimpleNamespace(napcat_ws_port=8098, current_group=123456, data_dir="/tmp/data")
    printed = []

    with patch("kouhai_bot.main.get_config", return_value=cfg), \
            patch("kouhai_bot.main._stop_existing_instance_on_port", return_value=True), \
            patch("builtins.print", side_effect=printed.append):
        main.stop()

    assert printed == [
        "action=stop",
        "port=8098",
        "stopped_existing=yes",
    ]


def test_status_reports_idle_when_port_is_free():
    cfg = SimpleNamespace(napcat_ws_port=8100, current_group=123456, data_dir="/tmp/data")
    printed = []

    with patch("kouhai_bot.main.get_config", return_value=cfg), \
            patch("kouhai_bot.main._current_port_listeners", return_value=set()), \
            patch("kouhai_bot.main._port_has_listener", return_value=False), \
            patch("builtins.print", side_effect=printed.append):
        main.status()

    assert printed == [
        "action=status",
        "port=8100",
        "occupied=no",
        "current_worktree_running=no",
    ]


def test_status_reports_current_worktree_listener():
    cfg = SimpleNamespace(napcat_ws_port=8101, current_group=123456, data_dir="/tmp/data")
    printed = []
    repo_root = Path("/repo/current")

    with patch("kouhai_bot.main.get_config", return_value=cfg), \
            patch("kouhai_bot.main._current_port_listeners", return_value={2201}), \
            patch("kouhai_bot.main._port_has_listener", return_value=True), \
            patch("kouhai_bot.main._repo_root", return_value=repo_root), \
            patch("kouhai_bot.main._pid_cwd", return_value=repo_root), \
            patch("builtins.print", side_effect=printed.append):
        main.status()

    assert printed == [
        "action=status",
        "port=8101",
        "occupied=yes",
        "current_worktree_running=yes",
        "pids=2201",
    ]


def test_status_reports_other_listener_when_worktree_differs():
    cfg = SimpleNamespace(napcat_ws_port=8102, current_group=123456, data_dir="/tmp/data")
    printed = []

    with patch("kouhai_bot.main.get_config", return_value=cfg), \
            patch("kouhai_bot.main._current_port_listeners", return_value={3311, 3312}), \
            patch("kouhai_bot.main._port_has_listener", return_value=True), \
            patch("kouhai_bot.main._repo_root", return_value=Path("/repo/current")), \
            patch("kouhai_bot.main._pid_cwd", side_effect=[Path("/repo/other"), Path("/repo/elsewhere")]), \
            patch("builtins.print", side_effect=printed.append):
        main.status()

    assert printed == [
        "action=status",
        "port=8102",
        "occupied=yes",
        "current_worktree_running=no",
        "pids=3311,3312",
    ]



def test_status_reports_pidless_listener_as_occupied():
    cfg = SimpleNamespace(napcat_ws_port=8103, current_group=123456, data_dir="/tmp/data")
    printed = []

    with patch("kouhai_bot.main.get_config", return_value=cfg), \
            patch("kouhai_bot.main._current_port_listeners", return_value=set()), \
            patch("kouhai_bot.main._port_has_listener", return_value=True), \
            patch("builtins.print", side_effect=printed.append):
        main.status()

    assert printed == [
        "action=status",
        "port=8103",
        "occupied=yes",
        "current_worktree_running=no",
        "pids=unknown",
    ]

def test_spawn_detached_bot_uses_nohup_and_group_daily_log(tmp_path):
    fake_proc = SimpleNamespace(pid=2468)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with patch("kouhai_bot.main.subprocess.Popen", return_value=fake_proc) as popen, \
            patch("kouhai_bot.main._repo_root", return_value=repo_root):
        pid, log_path = main._spawn_detached_bot(8099, 654321, str(tmp_path))

    assert pid == 2468
    assert log_path.parent == tmp_path / "logs" / "654321"
    assert log_path.name.endswith(".log")
    assert log_path.exists()

    args = popen.call_args.args[0]
    kwargs = popen.call_args.kwargs
    assert args == ["nohup", sys.executable, "-m", "kouhai_bot.worker"]
    assert kwargs["cwd"] == str(repo_root)
    assert kwargs["stdin"] is main.subprocess.DEVNULL
    assert kwargs["stderr"] is main.subprocess.STDOUT
    assert kwargs["start_new_session"] is True
    assert kwargs["close_fds"] is True
