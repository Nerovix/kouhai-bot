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
    name: str                           # canonical internal id, e.g. "newproblem"
    aliases: list[str] = field(default_factory=list)  # public short triggers, e.g. ["np"]
    description: str = ""               # one-line Chinese description
    usage: str = ""                     # usage example, e.g. "/np"
    detailed: str = ""                  # longer help text for /help <cmd>
    handler: Callable[..., Awaitable[None]] | None = None  # async handler(ctx, args)
    cooldown: int = 0                   # per-user cooldown in seconds
    admin_only: bool = False            # requires admin privilege

    @property
    def trigger(self) -> str:
        """The public command users type: the short alias when one exists,
        otherwise the canonical name. Full names of aliased commands are not
        accepted as triggers (alias-only interface)."""
        return self.aliases[0] if self.aliases else self.name


# Global registry — populated by auto-discovery
_registry: dict[str, CommandDef] = {}


def register(cmd: CommandDef) -> None:
    """Register a command's public triggers.

    Aliased commands expose ONLY their short aliases as triggers; the canonical
    full name is kept for internal identity (dispatch rewrites alias→name before
    the handler parses raw_text) but is not itself a usable command. Alias-less
    commands register their name.
    """
    if cmd.aliases:
        for alias in cmd.aliases:
            _registry[alias] = cmd
    else:
        _registry[cmd.name] = cmd


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
