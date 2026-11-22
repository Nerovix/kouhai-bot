"""Tests for the official-native command registry and auto-discovery.

The finalized command set is 12 commands with 5 short aliases. Each test runs
against an isolated registry dict so discovery/registration never leaks into
other tests (and vice versa).
"""

import pytest

from kouhai_bot.handlers import registry
from kouhai_bot.handlers.registry import (
    CommandDef,
    all_commands,
    discover_commands,
    get,
    register,
)

EXPECTED_COMMANDS = {
    "submit", "clarify", "review", "newproblem", "problem", "tag",
    "scoreboard", "status", "sync", "clear", "help", "range",
}

EXPECTED_ALIASES = {
    "sbm": "submit",
    "clrf": "clarify",
    "rv": "review",
    "np": "newproblem",
    "pb": "problem",
}

# Cooldowns as declared in each cmd module's register().
EXPECTED_COOLDOWNS = {
    "submit": 0,
    "clarify": 10,
    "review": 5,
    "newproblem": 0,
    "problem": 0,
    "tag": 0,
    "scoreboard": 0,
    "status": 0,
    "sync": 3,
    "clear": 3,
    "help": 0,
    "range": 10,
}


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    monkeypatch.setattr(registry, "_registry", {})


def test_discover_registers_exact_command_set():
    discover_commands()
    cmds = all_commands()
    names = {c.name for c in cmds}
    assert names == EXPECTED_COMMANDS
    assert len(cmds) == 12


def test_discover_is_idempotent():
    discover_commands()
    discover_commands()
    cmds = all_commands()
    assert len(cmds) == 12
    assert {c.name for c in cmds} == EXPECTED_COMMANDS


def test_all_commands_sorted_and_deduplicated():
    discover_commands()
    cmds = all_commands()
    names = [c.name for c in cmds]
    assert names == sorted(names)
    assert len(names) == len(set(names))


def test_every_command_has_async_handler_and_description():
    import inspect

    discover_commands()
    for cmd in all_commands():
        assert cmd.handler is not None, f"/{cmd.name} has no handler"
        assert inspect.iscoroutinefunction(cmd.handler), f"/{cmd.name} handler not async"
        assert cmd.description, f"/{cmd.name} has no description"


def test_aliases_resolve_to_canonical_definitions():
    discover_commands()
    for alias, canonical in EXPECTED_ALIASES.items():
        cmd = get(alias)
        assert cmd is not None, f"alias /{alias} does not resolve"
        assert cmd.name == canonical
        assert cmd.trigger == alias
        # Alias-only interface: the canonical full name is NOT a usable trigger.
        assert get(canonical) is None, f"/{canonical} should not be a trigger"
    # Exactly the five approved aliases exist beyond the alias-less command names.
    alias_keys = set(registry._registry) - EXPECTED_COMMANDS
    assert alias_keys == set(EXPECTED_ALIASES)


def test_aliasless_commands_register_their_name():
    discover_commands()
    for name in EXPECTED_COMMANDS - set(EXPECTED_ALIASES.values()):
        cmd = get(name)
        assert cmd is not None, f"/{name} should resolve by its own name"
        assert cmd.name == name
        assert cmd.aliases == []
        assert cmd.trigger == name


def test_removed_commands_and_old_aliases_absent():
    discover_commands()
    for name in (
        "setproblem", "sp",       # private judge removed
        "testcd",                 # starred/CD removed
        "feedback", "fb",         # feedback removed
        "bad",                    # legacy feedback name
        "sb", "提交", "新题", "排名",  # legacy aliases never supported
    ):
        assert get(name) is None, f"/{name} should not be registered"


def test_cooldown_values_match_command_definitions():
    discover_commands()
    actual = {c.name: c.cooldown for c in all_commands()}
    assert actual == EXPECTED_COOLDOWNS


def test_help_metadata():
    discover_commands()
    cmd = get("help")
    assert cmd is not None
    assert cmd.description == "显示本帮助"
    assert cmd.aliases == []


def test_register_lookup_and_dedup():
    async def _handler(ctx):
        pass

    cmd = CommandDef(
        name="regtest", aliases=["rt"], description="registry test",
        handler=_handler,
    )
    register(cmd)
    assert get("regtest") is None  # aliased command: full name is not a trigger
    assert get("rt") is cmd
    assert all_commands() == [cmd]

    # Re-registering the same name replaces the definition; all_commands
    # still yields exactly one entry for it.
    replacement = CommandDef(
        name="regtest", aliases=["rt"], description="replaced",
        handler=_handler,
    )
    register(replacement)
    assert get("regtest") is None
    assert get("rt") is replacement
    assert all_commands() == [replacement]


def test_all_commands_dedups_alias_entries():
    async def _handler(ctx):
        pass

    register(CommandDef(name="aaa", aliases=["a1", "a2"], handler=_handler))
    register(CommandDef(name="bbb", aliases=["b1"], handler=_handler))
    # Only the 3 alias triggers are registered (full names are not triggers),
    # but all_commands still yields exactly 2 unique commands.
    assert len(registry._registry) == 3
    assert [c.name for c in all_commands()] == ["aaa", "bbb"]


def test_get_unknown_returns_none():
    assert get("nonexistent") is None
    assert all_commands() == []