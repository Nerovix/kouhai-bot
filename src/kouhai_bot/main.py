"""Kouhai Bot entry point and local process helpers (official API).

The official bot is an outbound WebSocket client with no listening port, so
instance identity is the ``app_id``: a pidfile under ``{data_dir}/run/`` plus
process liveness/cwd replaces the legacy NapCat WS-port probe. prod/test bots
are separate app_ids with separate pidfiles and databases.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import get_config
from .worker import main_async as worker_main_async

_START_WAIT_TIMEOUT_SEC = 10.0
_STOP_WAIT_TIMEOUT_SEC = 10.0


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


async def main_async() -> None:
    await worker_main_async()


def main() -> None:
    asyncio.run(main_async())


# ── pidfile / process helpers ───────────────────────────────────────────


def _pidfile_path(app_id: str, data_dir: str) -> Path:
    return Path(data_dir) / "run" / f"{app_id}.pid"


def _read_pidfile(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    if not text.isdigit():
        return None
    return int(text)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_cwd(pid: int) -> Path | None:
    try:
        return Path(f"/proc/{pid}/cwd").resolve()
    except Exception:
        return None


def _running_pid(app_id: str, data_dir: str) -> int | None:
    pid = _read_pidfile(_pidfile_path(app_id, data_dir))
    if pid is not None and _pid_alive(pid):
        return pid
    return None


def _current_worktree_state(pid: int | None) -> str:
    if pid is None:
        return "no"
    cwd = _pid_cwd(pid)
    if cwd is None:
        return "unknown"
    return "yes" if cwd == _repo_root().resolve() else "no"


def _kill_pid(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def _wait_for_exit(pid: int, timeout_sec: float) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    return not _pid_alive(pid)


def _stop_existing(app_id: str, data_dir: str) -> bool:
    path = _pidfile_path(app_id, data_dir)
    pid = _running_pid(app_id, data_dir)
    if pid is None:
        path.unlink(missing_ok=True)
        return False
    _kill_pid(pid, signal.SIGTERM)
    if not _wait_for_exit(pid, _STOP_WAIT_TIMEOUT_SEC):
        _kill_pid(pid, signal.SIGKILL)
        if not _wait_for_exit(pid, _STOP_WAIT_TIMEOUT_SEC):
            raise RuntimeError(f"Could not stop bot pid {pid} (app_id={app_id})")
    path.unlink(missing_ok=True)
    return True


def _bot_log_path(app_id: str, data_dir: str) -> Path:
    tz = timezone(timedelta(hours=8))
    date_str = datetime.now(tz).strftime("%Y-%m-%d")
    log_dir = Path(data_dir) / "logs" / app_id
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{date_str}.log"


def _load_proxy_env() -> dict[str, str]:
    """Load proxy variables from ~/.proxy_env, returning a dict suitable for env=."""
    proxy_env_path = Path.home() / ".proxy_env"
    if not proxy_env_path.is_file():
        return {}
    try:
        lines = proxy_env_path.read_text().splitlines()
    except OSError:
        return {}
    env_vars: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"export\s+(\w+)=(.*)", line)
        if not m:
            continue
        key = m.group(1)
        value = m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        value = re.sub(
            r"[$](\w+)",
            lambda m2: env_vars.get(m2.group(1), ""),
            value,
        )
        env_vars[key] = value
    return env_vars


def _spawn_detached_bot(app_id: str, data_dir: str) -> tuple[int, Path]:
    log_path = _bot_log_path(app_id, data_dir)
    env = os.environ.copy()
    if not env.get("HTTPS_PROXY") and not env.get("https_proxy"):
        proxy_vars = _load_proxy_env()
        for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                     "NO_PROXY", "no_proxy"):
            if key in proxy_vars:
                env[key] = proxy_vars[key]
    with open(log_path, "ab") as log_file:
        proc = subprocess.Popen(
            ["nohup", sys.executable, "-m", "kouhai_bot.worker"],
            cwd=str(_repo_root()),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    pidfile = _pidfile_path(app_id, data_dir)
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(proc.pid))
    return proc.pid, log_path


def _wait_for_startup(pid: int, timeout_sec: float) -> bool:
    """Confirm the detached process stays alive past initial bootstrap."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return False
        time.sleep(0.2)
    return _pid_alive(pid)


def _print_status(
    *,
    action: str,
    app_id: str,
    stopped_existing: bool | None = None,
    already_running: bool | None = None,
    started: bool | None = None,
    pid: int | None = None,
    log_path: Path | None = None,
    occupied: bool | None = None,
    current_worktree_running: str | None = None,
) -> None:
    print(f"action={action}")
    print(f"app_id={app_id}")
    if stopped_existing is not None:
        print(f"stopped_existing={'yes' if stopped_existing else 'no'}")
    if already_running is not None:
        print(f"already_running={'yes' if already_running else 'no'}")
    if started is not None:
        print(f"started={'yes' if started else 'no'}")
    if occupied is not None:
        print(f"occupied={'yes' if occupied else 'no'}")
    if current_worktree_running is not None:
        print(f"current_worktree_running={current_worktree_running}")
    if pid is not None:
        print(f"pid={pid}")
    if log_path is not None:
        print(f"log={log_path}")


def start() -> None:
    """Start the bot in the background unless its app_id pidfile is live."""
    cfg = get_config()
    cfg.validate_official()
    if _running_pid(cfg.app_id, cfg.data_dir) is not None:
        _print_status(
            action="start", app_id=cfg.app_id,
            already_running=True, started=False,
        )
        return
    pid, log_path = _spawn_detached_bot(cfg.app_id, cfg.data_dir)
    started = _wait_for_startup(pid, _START_WAIT_TIMEOUT_SEC)
    _print_status(
        action="start", app_id=cfg.app_id,
        already_running=False, started=started, pid=pid, log_path=log_path,
    )
    if not started:
        raise RuntimeError(f"Detached bot failed to start (app_id={cfg.app_id}). See log: {log_path}")


def restart() -> None:
    """Stop the existing app_id instance, then start a detached replacement."""
    cfg = get_config()
    cfg.validate_official()
    stopped_existing = _stop_existing(cfg.app_id, cfg.data_dir)
    pid, log_path = _spawn_detached_bot(cfg.app_id, cfg.data_dir)
    started = _wait_for_startup(pid, _START_WAIT_TIMEOUT_SEC)
    _print_status(
        action="restart", app_id=cfg.app_id,
        stopped_existing=stopped_existing, started=started, pid=pid, log_path=log_path,
    )
    if not started:
        raise RuntimeError(f"Detached bot failed to start (app_id={cfg.app_id}). See log: {log_path}")


def stop() -> None:
    """Stop the bot instance identified by the configured app_id."""
    cfg = get_config()
    stopped_existing = _stop_existing(cfg.app_id, cfg.data_dir)
    _print_status(action="stop", app_id=cfg.app_id, stopped_existing=stopped_existing)


def status() -> None:
    """Report whether the configured app_id instance is running."""
    cfg = get_config()
    pid = _running_pid(cfg.app_id, cfg.data_dir)
    _print_status(
        action="status", app_id=cfg.app_id,
        occupied=pid is not None,
        current_worktree_running=_current_worktree_state(pid),
        pid=pid,
    )


if __name__ == "__main__":
    main()