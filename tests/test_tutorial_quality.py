"""Tests for tools/tutorial_tools.py quality helpers."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from tutorial_tools import is_tutorial_quality_ok
from tutorial_tools import tutorial_quality_reason

from kouhai_bot.tutorials import MIN_EDITORIAL_LEN


def _long(prefix: str = "x") -> str:
    return prefix * (MIN_EDITORIAL_LEN + 5)


def test_quality_ok_from_solution():
    bundle = {
        "sections": [{"hint": "", "solution": _long("s"), "raw_text": "", "code_blocks": []}]
    }
    assert tutorial_quality_reason(bundle) is None
    assert is_tutorial_quality_ok(bundle)


def test_loading_placeholder_fails():
    bundle = {
        "sections": [
            {
                "hint": "",
                "solution": "Will be added soon.",
                "raw_text": "Tutorial is loading",
                "code_blocks": [],
            }
        ]
    }
    assert tutorial_quality_reason(bundle) == "loading_placeholder"


def test_too_short_fails():
    bundle = {
        "sections": [{"hint": "short", "solution": "", "raw_text": "", "code_blocks": []}]
    }
    assert tutorial_quality_reason(bundle) == "too_short"
