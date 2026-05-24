"""Command handler registry with auto-discovery.

Each command module in handlers/cmd/ exports a `register()` function
that returns a CommandDef. The registry scans and collects them all.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
from dataclasses import dataclass, field
from typing import Callable, Awaitable


@dataclass
class CommandDef:
    """Definition of a bot command."""
    name: str                           # e.g. "newproblem"
    aliases: list[str] = field(default_factory=list)  # e.g. ["np", "新题"]
    description: str = ""               # one-line Chinese description
    usage: str = ""                     # usage example, e.g. "/newproblem"
    detailed: str = ""                  # longer help text for /help <cmd>
    handler: Callable[..., Awaitable[None]] | None = None  # async handler(ctx, args)
    cooldown: int = 0                   # per-user cooldown in seconds
    admin_only: bool = False            # requires admin privilege


# Global registry — populated by auto-discovery
_registry: dict[str, CommandDef] = {}


def register(cmd: CommandDef) -> None:
    """Register a command definition."""
    _registry[cmd.name] = cmd
    for alias in cmd.aliases:
        _registry[alias] = cmd


def get(name: str) -> CommandDef | None:
    """Look up a command by name or alias."""
    return _registry.get(name)


def all_commands() -> list[CommandDef]:
    """Return all unique command definitions (deduplicated by name)."""
    seen: set[str] = set()
    result = []
    for cmd in _registry.values():
        if cmd.name not in seen:
            seen.add(cmd.name)
            result.append(cmd)
    result.sort(key=lambda c: c.name)
    return result


def discover_commands(package_path: str = "kouhai_bot.handlers.cmd") -> None:
    """Auto-discover and register all command modules."""
    package = importlib.import_module(package_path)
    pkg_dir = os.path.dirname(package.__file__) if package.__file__ else ""

    for _, module_name, _ in pkgutil.iter_modules([pkg_dir]):
        full_name = f"{package_path}.{module_name}"
        mod = importlib.import_module(full_name)
        if hasattr(mod, "register"):
            mod.register()
