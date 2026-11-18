"""Pidfile-based lifecycle coverage for kouhai_bot.main (official API).

Instance identity is the official ``app_id`` (placeholder values only — never
real credentials): a pidfile under ``{data_dir}/run/`` plus process
liveness/cwd replaces the legacy NapCat WS-port probe.
"""

import os
import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kouhai_bot import main

APP_ID = "1900000000"


def _cfg(data_dir: str, *, validate_error: Exception | None = None) -> SimpleNamespace:
    def _validate() -> None:
        if validate_error is not None:
            raise validate_error

    return SimpleNamespace(app_id=APP_ID, data_dir=data_dir, validate_official=_validate)


# ── pidfile / log path formatting ───────────────────────────────────────


def test_pidfile_path_uses_app_id_under_run_dir(tmp_path):
    assert main._pidfile_path(APP_ID, str(tmp_path)) == (
        tmp_path / "run" / f"{APP_ID}.pid"
    )


def test_bot_log_path_uses_app_id_and_local_date(tmp_path):
    tz = timezone(timedelta(hours=8))
    expected_date = datetime.now(tz).strftime("%Y-%m-%d")

    path = main._bot_log_path(APP_ID, str(tmp_path))

    assert path == tmp_path / "logs" / APP_ID / f"{expected_date}.log"
    assert path.parent.is_dir()


def test_read_pidfile_parses_digits_and_rejects_garbage(tmp_path):
    path = tmp_path / "bot.pid"
    assert main._read_pidfile(path) is None  # missing file

    path.write_text("  4321\n")
    assert main._read_pidfile(path) == 4321

    path.write_text("not-a-pid")
    assert main._read_pidfile(path) is None

    path.write_text("-5")
    assert main._read_pidfile(path) is None


# ── process liveness / worktree identity ────────────────────────────────


def test_pid_alive_nonpositive_pid_is_dead_without_kill(monkeypatch):
    def _fail(pid, sig):
        raise AssertionError("os.kill must not be called for pid <= 0")

    monkeypatch.setattr(main.os, "kill", _fail)
    assert main._pid_alive(0) is False
    assert main._pid_alive(-1) is False


def test_pid_alive_maps_kill_errors(monkeypatch):
    outcome = {}

    def _kill(pid, sig):
        assert sig == 0
        if "exc" in outcome:
            raise outcome["exc"]

    monkeypatch.setattr(main.os, "kill", _kill)
    assert main._pid_alive(4321) is True

    outcome["exc"] = ProcessLookupError()
    assert main._pid_alive(4321) is False

    outcome["exc"] = PermissionError()
    assert main._pid_alive(4321) is True


def test_running_pid_requires_live_pidfile_process(tmp_path, monkeypatch):
    pidfile = main._pidfile_path(APP_ID, str(tmp_path))
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text("4321")

    monkeypatch.setattr(main, "_pid_alive", lambda pid: pid == 4321)
    monkeypatch.setattr(main, "_pid_is_worker", lambda pid: pid == 4321)
    assert main._running_pid(APP_ID, str(tmp_path)) == 4321

    monkeypatch.setattr(main, "_pid_alive", lambda pid: False)
    assert main._running_pid(APP_ID, str(tmp_path)) is None

    pidfile.unlink()
    assert main._running_pid(APP_ID, str(tmp_path)) is None


def test_running_pid_rejects_recycled_non_worker_process(tmp_path, monkeypatch):
    pidfile = main._pidfile_path(APP_ID, str(tmp_path))
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text("4321")

    # PID is alive but its cmdline is not our worker (OS recycled the PID).
    monkeypatch.setattr(main, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(main, "_pid_is_worker", lambda pid: False)
    assert main._running_pid(APP_ID, str(tmp_path)) is None


def test_pid_is_worker_reads_cmdline(monkeypatch):
    def _read_bytes(self):
        if str(self) == "/proc/4321/cmdline":
            return b"python\x00-m\x00kouhai_bot.worker\x00"
        if str(self) == "/proc/9999/cmdline":
            return b"nginx\x00-g\x00daemon off;\x00"
        raise OSError("unreadable")

    monkeypatch.setattr(main.Path, "read_bytes", _read_bytes)
    assert main._pid_is_worker(4321) is True
    assert main._pid_is_worker(9999) is False  # recycled innocent process
    assert main._pid_is_worker(1234) is False  # unreadable cmdline → fail-safe


def test_current_worktree_state_variants(monkeypatch):
    repo_root = Path("/repo/current")
    monkeypatch.setattr(main, "_repo_root", lambda: repo_root)

    assert main._current_worktree_state(None) == "no"

    monkeypatch.setattr(main, "_pid_cwd", lambda pid: None)
    assert main._current_worktree_state(4321) == "unknown"

    monkeypatch.setattr(main, "_pid_cwd", lambda pid: repo_root)
    assert main._current_worktree_state(4321) == "yes"

    monkeypatch.setattr(main, "_pid_cwd", lambda pid: Path("/repo/other"))
    assert main._current_worktree_state(4321) == "no"


# ── stop: SIGTERM → SIGKILL escalation + pidfile cleanup ────────────────


def test_stop_existing_without_running_process_cleans_stale_pidfile(tmp_path):
    pidfile = main._pidfile_path(APP_ID, str(tmp_path))
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text("4321")

    with patch("kouhai_bot.main._pid_alive", return_value=False), \
            patch("kouhai_bot.main.os.kill") as kill:
        assert main._stop_existing(APP_ID, str(tmp_path)) is False

    kill.assert_not_called()
    assert not pidfile.exists()


def test_stop_existing_sigterm_graceful(tmp_path):
    pidfile = main._pidfile_path(APP_ID, str(tmp_path))
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text("4321")
    signals: list[int] = []

    with patch("kouhai_bot.main._pid_alive", return_value=True), \
            patch("kouhai_bot.main._pid_is_worker", return_value=True), \
            patch("kouhai_bot.main.os.kill", side_effect=lambda pid, sig: signals.append(sig)), \
            patch("kouhai_bot.main._wait_for_exit", return_value=True) as wait:
        assert main._stop_existing(APP_ID, str(tmp_path)) is True

    assert signals == [signal.SIGTERM]
    wait.assert_called_once_with(4321, main._STOP_WAIT_TIMEOUT_SEC)
    assert not pidfile.exists()


def test_stop_existing_escalates_to_sigkill(tmp_path):
    pidfile = main._pidfile_path(APP_ID, str(tmp_path))
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text("4321")
    signals: list[int] = []

    with patch("kouhai_bot.main._pid_alive", return_value=True), \
            patch("kouhai_bot.main._pid_is_worker", return_value=True), \
            patch("kouhai_bot.main.os.kill", side_effect=lambda pid, sig: signals.append(sig)), \
            patch("kouhai_bot.main._wait_for_exit", side_effect=[False, True]):
        assert main._stop_existing(APP_ID, str(tmp_path)) is True

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert not pidfile.exists()


def test_stop_existing_raises_when_process_never_dies(tmp_path):
    pidfile = main._pidfile_path(APP_ID, str(tmp_path))
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text("4321")

    with patch("kouhai_bot.main._pid_alive", return_value=True), \
            patch("kouhai_bot.main._pid_is_worker", return_value=True), \
            patch("kouhai_bot.main.os.kill"), \
            patch("kouhai_bot.main._wait_for_exit", return_value=False):
        with pytest.raises(RuntimeError, match="Could not stop bot pid 4321"):
            main._stop_existing(APP_ID, str(tmp_path))

    assert pidfile.exists()  # not cleaned up on failure


def test_wait_for_exit_uses_fake_clock(monkeypatch):
    clock = iter([0.0, 0.05, 0.15, 5.0])
    monkeypatch.setattr(main.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(main.time, "sleep", lambda sec: None)
    alive = iter([True, True, False])
    monkeypatch.setattr(main, "_pid_alive", lambda pid: next(alive))

    assert main._wait_for_exit(4321, timeout_sec=1.0) is True


# ── detached spawn ──────────────────────────────────────────────────────


def test_spawn_detached_bot_uses_nohup_and_app_id_daily_log(tmp_path):
    fake_proc = SimpleNamespace(pid=2468)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with patch("kouhai_bot.main.subprocess.Popen", return_value=fake_proc) as popen, \
            patch("kouhai_bot.main._repo_root", return_value=repo_root):
        pid, log_path = main._spawn_detached_bot(APP_ID, str(tmp_path))

    assert pid == 2468
    assert log_path.parent == tmp_path / "logs" / APP_ID
    assert log_path.name.endswith(".log")
    assert log_path.exists()

    pidfile = main._pidfile_path(APP_ID, str(tmp_path))
    assert pidfile.read_text() == "2468"

    args = popen.call_args.args[0]
    kwargs = popen.call_args.kwargs
    assert args == ["nohup", sys.executable, "-m", "kouhai_bot.worker"]
    assert kwargs["cwd"] == str(repo_root)
    assert kwargs["stdin"] is main.subprocess.DEVNULL
    assert kwargs["stderr"] is main.subprocess.STDOUT
    assert kwargs["start_new_session"] is True
    assert kwargs["close_fds"] is True


def test_spawn_detached_bot_injects_proxy_env_when_unset(tmp_path):
    fake_proc = SimpleNamespace(pid=2468)
    env_without_proxy = {
        k: v for k, v in os.environ.items()
        if k not in ("HTTPS_PROXY", "https_proxy")
    }

    with patch("kouhai_bot.main.subprocess.Popen", return_value=fake_proc) as popen, \
            patch.dict(os.environ, env_without_proxy, clear=True), \
            patch(
                "kouhai_bot.main._load_proxy_env",
                return_value={"https_proxy": "http://proxy.example:8080"},
            ):
        main._spawn_detached_bot(APP_ID, str(tmp_path))

    env = popen.call_args.kwargs["env"]
    assert env["https_proxy"] == "http://proxy.example:8080"


def test_spawn_detached_bot_keeps_existing_proxy_env(tmp_path):
    fake_proc = SimpleNamespace(pid=2468)

    with patch("kouhai_bot.main.subprocess.Popen", return_value=fake_proc) as popen, \
            patch.dict(os.environ, {"HTTPS_PROXY": "http://existing.example:3128"}), \
            patch("kouhai_bot.main._load_proxy_env") as load_proxy:
        main._spawn_detached_bot(APP_ID, str(tmp_path))

    load_proxy.assert_not_called()
    assert popen.call_args.kwargs["env"]["HTTPS_PROXY"] == "http://existing.example:3128"


def test_wait_for_startup_detects_early_death(monkeypatch):
    monkeypatch.setattr(main, "_pid_alive", lambda pid: False)
    assert main._wait_for_startup(4321, timeout_sec=1.0) is False


def test_wait_for_startup_survives_timeout(monkeypatch):
    clock = iter([0.0, 0.1, 5.0, 5.0])
    monkeypatch.setattr(main.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(main.time, "sleep", lambda sec: None)
    monkeypatch.setattr(main, "_pid_alive", lambda pid: True)
    assert main._wait_for_startup(4321, timeout_sec=1.0) is True


# ── proxy env file parsing (unchanged behavior) ─────────────────────────


def test_load_proxy_env_does_not_resolve_forward_refs_from_process_env(tmp_path):
    (tmp_path / ".proxy_env").write_text(
        "export HTTPS_PROXY=$PROXY_URL\n"
        "export PROXY_URL=http://proxy.example:8080\n"
    )

    with patch("kouhai_bot.main.Path.home", return_value=tmp_path), \
            patch.dict(os.environ, {"PROXY_URL": "http://unrelated.example:3128"}):
        proxy_vars = main._load_proxy_env()

    assert proxy_vars == {
        "HTTPS_PROXY": "",
        "PROXY_URL": "http://proxy.example:8080",
    }


def test_load_proxy_env_returns_empty_when_file_cannot_be_read(tmp_path):
    (tmp_path / ".proxy_env").touch()

    with patch("kouhai_bot.main.Path.home", return_value=tmp_path), \
            patch("kouhai_bot.main.Path.read_text", side_effect=PermissionError):
        assert main._load_proxy_env() == {}


# ── entry points: start / restart / stop / status ───────────────────────


def test_start_reports_already_running_without_spawning(tmp_path):
    printed = []

    with patch("kouhai_bot.main.get_config", return_value=_cfg(str(tmp_path))), \
            patch("kouhai_bot.main._running_pid", return_value=4321), \
            patch("kouhai_bot.main._spawn_detached_bot") as spawn, \
            patch("builtins.print", side_effect=printed.append):
        main.start()

    spawn.assert_not_called()
    assert printed == [
        "action=start",
        f"app_id={APP_ID}",
        "already_running=yes",
        "started=no",
    ]


def test_start_spawns_and_reports_success(tmp_path):
    printed = []
    log_path = tmp_path / "logs" / APP_ID / "2026-05-16.log"

    with patch("kouhai_bot.main.get_config", return_value=_cfg(str(tmp_path))), \
            patch("kouhai_bot.main._running_pid", return_value=None), \
            patch("kouhai_bot.main._spawn_detached_bot", return_value=(4321, log_path)) as spawn, \
            patch("kouhai_bot.main._wait_for_startup", return_value=True) as wait, \
            patch("builtins.print", side_effect=printed.append):
        main.start()

    spawn.assert_called_once_with(APP_ID, str(tmp_path))
    wait.assert_called_once_with(4321, main._START_WAIT_TIMEOUT_SEC)
    assert printed == [
        "action=start",
        f"app_id={APP_ID}",
        "already_running=no",
        "started=yes",
        "pid=4321",
        f"log={log_path}",
    ]


def test_start_raises_when_detached_process_dies(tmp_path):
    log_path = tmp_path / "bot.log"

    with patch("kouhai_bot.main.get_config", return_value=_cfg(str(tmp_path))), \
            patch("kouhai_bot.main._running_pid", return_value=None), \
            patch("kouhai_bot.main._spawn_detached_bot", return_value=(4321, log_path)), \
            patch("kouhai_bot.main._wait_for_startup", return_value=False), \
            patch("builtins.print"):
        with pytest.raises(RuntimeError, match="failed to start"):
            main.start()


def test_start_requires_official_credentials(tmp_path):
    cfg = _cfg(str(tmp_path), validate_error=RuntimeError("official.app_id is required"))

    with patch("kouhai_bot.main.get_config", return_value=cfg), \
            patch("kouhai_bot.main._spawn_detached_bot") as spawn:
        with pytest.raises(RuntimeError, match="app_id"):
            main.start()

    spawn.assert_not_called()


def test_restart_stops_then_starts(tmp_path):
    printed = []
    log_path = tmp_path / "logs" / APP_ID / "2026-05-16.log"
    calls: list[str] = []

    def _stop(app_id, data_dir):
        calls.append("stop")
        assert (app_id, data_dir) == (APP_ID, str(tmp_path))
        return True

    def _spawn(app_id, data_dir):
        calls.append("spawn")
        return 4321, log_path

    with patch("kouhai_bot.main.get_config", return_value=_cfg(str(tmp_path))), \
            patch("kouhai_bot.main._stop_existing", side_effect=_stop), \
            patch("kouhai_bot.main._spawn_detached_bot", side_effect=_spawn), \
            patch("kouhai_bot.main._wait_for_startup", return_value=True), \
            patch("builtins.print", side_effect=printed.append):
        main.restart()

    assert calls == ["stop", "spawn"]
    assert printed == [
        "action=restart",
        f"app_id={APP_ID}",
        "stopped_existing=yes",
        "started=yes",
        "pid=4321",
        f"log={log_path}",
    ]


def test_restart_without_existing_instance(tmp_path):
    printed = []
    log_path = tmp_path / "bot.log"

    with patch("kouhai_bot.main.get_config", return_value=_cfg(str(tmp_path))), \
            patch("kouhai_bot.main._stop_existing", return_value=False), \
            patch("kouhai_bot.main._spawn_detached_bot", return_value=(4321, log_path)), \
            patch("kouhai_bot.main._wait_for_startup", return_value=True), \
            patch("builtins.print", side_effect=printed.append):
        main.restart()

    assert "stopped_existing=no" in printed


def test_stop_reports_if_instance_was_stopped(tmp_path):
    printed = []

    with patch("kouhai_bot.main.get_config", return_value=_cfg(str(tmp_path))), \
            patch("kouhai_bot.main._stop_existing", return_value=True) as stop_existing, \
            patch("builtins.print", side_effect=printed.append):
        main.stop()

    stop_existing.assert_called_once_with(APP_ID, str(tmp_path))
    assert printed == [
        "action=stop",
        f"app_id={APP_ID}",
        "stopped_existing=yes",
    ]


def test_stop_reports_when_nothing_running(tmp_path):
    printed = []

    with patch("kouhai_bot.main.get_config", return_value=_cfg(str(tmp_path))), \
            patch("kouhai_bot.main._stop_existing", return_value=False), \
            patch("builtins.print", side_effect=printed.append):
        main.stop()

    assert printed == [
        "action=stop",
        f"app_id={APP_ID}",
        "stopped_existing=no",
    ]


def test_status_reports_idle_when_no_pidfile_process(tmp_path):
    printed = []

    with patch("kouhai_bot.main.get_config", return_value=_cfg(str(tmp_path))), \
            patch("kouhai_bot.main._running_pid", return_value=None), \
            patch("builtins.print", side_effect=printed.append):
        main.status()

    assert printed == [
        "action=status",
        f"app_id={APP_ID}",
        "occupied=no",
        "current_worktree_running=no",
    ]


def test_status_reports_current_worktree_process(tmp_path):
    printed = []
    repo_root = Path("/repo/current")

    with patch("kouhai_bot.main.get_config", return_value=_cfg(str(tmp_path))), \
            patch("kouhai_bot.main._running_pid", return_value=4321), \
            patch("kouhai_bot.main._repo_root", return_value=repo_root), \
            patch("kouhai_bot.main._pid_cwd", return_value=repo_root), \
            patch("builtins.print", side_effect=printed.append):
        main.status()

    assert printed == [
        "action=status",
        f"app_id={APP_ID}",
        "occupied=yes",
        "current_worktree_running=yes",
        "pid=4321",
    ]


def test_status_reports_other_worktree_process(tmp_path):
    printed = []

    with patch("kouhai_bot.main.get_config", return_value=_cfg(str(tmp_path))), \
            patch("kouhai_bot.main._running_pid", return_value=4321), \
            patch("kouhai_bot.main._repo_root", return_value=Path("/repo/current")), \
            patch("kouhai_bot.main._pid_cwd", return_value=Path("/repo/other")), \
            patch("builtins.print", side_effect=printed.append):
        main.status()

    assert printed == [
        "action=status",
        f"app_id={APP_ID}",
        "occupied=yes",
        "current_worktree_running=no",
        "pid=4321",
    ]


def test_status_reports_unknown_when_cwd_unreadable(tmp_path):
    printed = []

    with patch("kouhai_bot.main.get_config", return_value=_cfg(str(tmp_path))), \
            patch("kouhai_bot.main._running_pid", return_value=4321), \
            patch("kouhai_bot.main._pid_cwd", return_value=None), \
            patch("builtins.print", side_effect=printed.append):
        main.status()

    assert printed == [
        "action=status",
        f"app_id={APP_ID}",
        "occupied=yes",
        "current_worktree_running=unknown",
        "pid=4321",
    ]