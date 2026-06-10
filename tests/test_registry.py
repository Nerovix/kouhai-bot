"""Tests for command registry and help auto-generation."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.handlers.registry import (
    discover_commands, all_commands, get, CommandDef
)


def test_discover_commands():
    """All command modules should be discovered."""
    discover_commands()
    cmds = all_commands()
    names = {c.name for c in cmds}
    expected = {"help", "newproblem", "submit", "problem",
                "tag", "clarify", "scoreboard", "setproblem", "sync"}
    missing = expected - names
    assert not missing, f"Missing commands: {missing}"
    assert len(cmds) >= 7


def test_help_is_registered():
    """Help command should be auto-registered."""
    discover_commands()
    cmd = get("help")
    assert cmd is not None
    assert cmd.description == "显示本帮助"
    assert cmd.aliases == []


def test_newproblem_aliases():
    """Only the approved newproblem short alias should resolve."""
    discover_commands()
    assert get("新题") is None  # old alias remains unsupported
    assert get("newproblem") is not None  # canonical name works
    cmd = get("np")
    assert cmd is not None
    assert cmd.name == "newproblem"


def test_alias_lookup():
    """Approved short aliases resolve; old aliases still do not."""
    discover_commands()
    expected = {
        "sbm": "submit",
        "clrf": "clarify",
        "rv": "review",
        "pb": "problem",
        "np": "newproblem",
        "sp": "setproblem",
    }
    for alias, canonical in expected.items():
        cmd = get(alias)
        assert cmd is not None
        assert cmd.name == canonical
        assert get(canonical) is cmd
    assert get("提交") is None  # old alias remains unsupported
