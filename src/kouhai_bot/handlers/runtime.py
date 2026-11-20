"""Process-wide runtime handles for the official-native handler layer.

The worker sets these once at startup (Database + OfficialSender + optional
RenderBrowser). Handlers and the stateful coordinator reach them through
``get_runtime()`` instead of threading dependencies through every signature.
Tests inject a fake runtime via ``set_runtime()``.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..db.database import Database
from ..official.sender import OfficialSender
from ..render.browser import RenderBrowser


@dataclass
class Runtime:
    db: Database
    sender: OfficialSender
    browser: RenderBrowser | None = None


_runtime: Runtime | None = None


def set_runtime(runtime: Runtime | None) -> None:
    global _runtime
    _runtime = runtime


def get_runtime() -> Runtime:
    if _runtime is None:
        raise RuntimeError("handler runtime not initialized (worker must set_runtime)")
    return _runtime