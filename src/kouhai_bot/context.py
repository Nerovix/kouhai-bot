"""Group and user context persistence.

Group context: rolling message log per group (used for LLM context).
User sessions: per-user conversation history keyed by (group_id, user_id).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .config import get_config


def _data_dir() -> Path:
    return Path(get_config().data_dir)


# ── Display name ─────────────────────────────────────────────────────────

def get_display_name(sender: dict) -> str:
    """Get best display name from a OneBot11 sender dict."""
    card = sender.get("card", "")
    nick = sender.get("nickname", "")
    return card or nick or str(sender.get("user_id", "?"))


# ── Group context ────────────────────────────────────────────────────────

def _group_ctx_file(group_id: int) -> str:
    d = _data_dir() / "groups"
    d.mkdir(parents=True, exist_ok=True)
    return str(d / f"groupctx_{group_id}.json")


def load_group_ctx(group_id: int) -> list[dict]:
    """Load rolling message history for a group."""
    path = _group_ctx_file(group_id)
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return []


def save_group_ctx(group_id: int, ctx: list[dict]) -> None:
    """Save rolling message history for a group. Trims to max length."""
    cfg = get_config()
    limit = getattr(cfg, "max_context_per_session", 100)
    if len(ctx) > limit:
        ctx = ctx[-limit:]
    with open(_group_ctx_file(group_id), "w") as f:
        json.dump(ctx, f, ensure_ascii=False)


def append_group_ctx(group_id: int, msg: dict) -> None:
    """Append a message to the group context and save."""
    ctx = load_group_ctx(group_id)
    ctx.append(msg)
    save_group_ctx(group_id, ctx)


# ── Session context (per user) ──────────────────────────────────────────

def session_key(group_id: int, user_id: int) -> str:
    """Generate a session key for a user in a group."""
    return f"group_{group_id}_user_{user_id}"


def _session_file(key: str) -> str:
    d = _data_dir() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return str(d / f"{key}.json")


def load_session(key: str) -> list[dict]:
    """Load conversation history for a session."""
    path = _session_file(key)
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return []


def save_session(key: str, ctx: list[dict]) -> None:
    """Save conversation history for a session. Trims to max length."""
    cfg = get_config()
    limit = getattr(cfg, "max_context_per_session", 100)
    if len(ctx) > limit:
        ctx = ctx[-limit:]
    with open(_session_file(key), "w") as f:
        json.dump(ctx, f, ensure_ascii=False)


def append_session(key: str, msg: dict) -> None:
    """Append a message to a session and save."""
    ctx = load_session(key)
    ctx.append(msg)
    save_session(key, ctx)
