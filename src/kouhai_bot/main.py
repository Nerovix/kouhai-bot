"""Kouhai Bot entry point and local process helpers."""

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

_PORT_WAIT_TIMEOUT_SEC = 10.0


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


async def main_async() -> None:
    await worker_main_async()


def main() -> None:
    asyncio.run(main_async())


def _listener_lines(port: int) -> list[str]:
    try:
        result = subprocess.run(
            ["ss", "-ltnp", f"( sport = :{port} )"],
            capture_output=True, text=True, check=False,
        )
    except Exception:
        return []

    return [
        line
        for line in result.stdout.splitlines()
        if line.startswith("LISTEN")
    ]


def _listener_pids(port: int) -> set[int]:
    lines = _listener_lines(port)
    return {int(pid) for line in lines for pid in re.findall(r"pid=(\d+)", line)}


def _port_has_listener(port: int) -> bool:
    return bool(_listener_lines(port))


def _kill_pids(pids: set[int], sig: int) -> None:
    for pid in sorted(pids):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def _current_port_listeners(port: int) -> set[int]:
    pids = _listener_pids(port)
    pids.discard(os.getpid())
    return pids


def _wait_for_port_release(port: int, timeout_sec: float) -> set[int]:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        remaining = _current_port_listeners(port)
        if not _port_has_listener(port):
            return set()
        time.sleep(0.1)
    return _current_port_listeners(port)


def _stop_existing_instance_on_port(port: int) -> bool:
    pids = _current_port_listeners(port)
    if not pids:
        if _port_has_listener(port):
            raise RuntimeError(
                f"NapCat WS port {port} is occupied, but the listener PID is unavailable"
            )
        return False

    _kill_pids(pids, signal.SIGTERM)
    remaining = _wait_for_port_release(port, timeout_sec=_PORT_WAIT_TIMEOUT_SEC)
    if not _port_has_listener(port):
        return True

    _kill_pids(remaining, signal.SIGKILL)
    remaining = _wait_for_port_release(port, timeout_sec=_PORT_WAIT_TIMEOUT_SEC)
    if _port_has_listener(port):
        raise RuntimeError(
            f"Could not release NapCat WS port {port}; still listening: {sorted(remaining)}"
        )
    return True


def _bot_log_path(group_id: int, data_dir: str) -> Path:
    tz = timezone(timedelta(hours=8))
    date_str = datetime.now(tz).strftime("%Y-%m-%d")
    log_dir = Path(data_dir) / "logs" / str(group_id)
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{date_str}.log"


def _pid_cwd(pid: int) -> Path | None:
    try:
        return Path(f"/proc/{pid}/cwd").resolve()
    except Exception:
        return None


def _current_worktree_listener_state(port: int) -> str:
    repo_root = _repo_root().resolve()
    saw_unknown = False
    for pid in sorted(_current_port_listeners(port)):
        cwd = _pid_cwd(pid)
        if cwd is None:
            saw_unknown = True
            continue
        if cwd == repo_root:
            return "yes"
    return "unknown" if saw_unknown else "no"


def _wait_for_port_bind(port: int, timeout_sec: float) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if _port_has_listener(port):
            return True
        time.sleep(0.1)
    return _port_has_listener(port)


def _spawn_detached_bot(port: int, group_id: int, data_dir: str) -> tuple[int, Path]:
    log_path = _bot_log_path(group_id, data_dir)
    with open(log_path, "ab") as log_file:
        proc = subprocess.Popen(
            ["nohup", sys.executable, "-m", "kouhai_bot.worker"],
            cwd=str(_repo_root()),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    return proc.pid, log_path


def _print_start_status(
    *,
    port: int,
    action: str,
    stopped_existing: bool | None = None,
    already_running: bool | None = None,
    started: bool | None = None,
    pid: int | None,
    log_path: Path | None,
) -> None:
    print(f"action={action}")
    print(f"port={port}")
    if stopped_existing is not None:
        print(f"stopped_existing={'yes' if stopped_existing else 'no'}")
    if already_running is not None:
        print(f"already_running={'yes' if already_running else 'no'}")
    if started is not None:
        print(f"started={'yes' if started else 'no'}")
    if pid is not None:
        print(f"pid={pid}")
    if log_path is not None:
        print(f"log={log_path}")


def start() -> None:
    """Start the bot instance in the background if its WS port is free."""
    cfg = get_config()
    port = cfg.napcat_ws_port
    if _port_has_listener(port):
        _print_start_status(
            port=port,
            action="start",
            already_running=True,
            started=False,
            pid=None,
            log_path=None,
        )
        return

    pid, log_path = _spawn_detached_bot(port, cfg.current_group, cfg.data_dir)
    started = _wait_for_port_bind(port, timeout_sec=_PORT_WAIT_TIMEOUT_SEC)
    _print_start_status(
        port=port,
        action="start",
        already_running=False,
        started=started,
        pid=pid,
        log_path=log_path,
    )
    if not started:
        raise RuntimeError(
            f"Detached bot failed to bind NapCat WS port {port}. See log: {log_path}"
        )


def restart() -> None:
    """Restart the bot instance bound to the configured NapCat WS port.

    Stops any existing listener on that port, then starts a detached background instance.
    """
    cfg = get_config()
    port = cfg.napcat_ws_port
    stopped_existing = _stop_existing_instance_on_port(port)
    pid, log_path = _spawn_detached_bot(port, cfg.current_group, cfg.data_dir)
    started = _wait_for_port_bind(port, timeout_sec=_PORT_WAIT_TIMEOUT_SEC)
    _print_start_status(
        port=port,
        action="restart",
        stopped_existing=stopped_existing,
        started=started,
        pid=pid,
        log_path=log_path,
    )
    if not started:
        raise RuntimeError(
            f"Detached bot failed to bind NapCat WS port {port}. See log: {log_path}"
        )


def stop() -> None:
    """Stop the bot instance bound to the configured NapCat WS port."""
    cfg = get_config()
    port = cfg.napcat_ws_port
    stopped_existing = _stop_existing_instance_on_port(port)
    print("action=stop")
    print(f"port={port}")
    print(f"stopped_existing={'yes' if stopped_existing else 'no'}")


def status() -> None:
    """Report whether the configured NapCat WS port is occupied."""
    cfg = get_config()
    port = cfg.napcat_ws_port
    listeners = sorted(_current_port_listeners(port))
    occupied = _port_has_listener(port)
    print("action=status")
    print(f"port={port}")
    print(f"occupied={'yes' if occupied else 'no'}")
    print(f"current_worktree_running={_current_worktree_listener_state(port) if occupied else 'no'}")
    if listeners:
        print("pids=" + ",".join(str(pid) for pid in listeners))
    elif occupied:
        print("pids=unknown")


if __name__ == "__main__":
    main()
