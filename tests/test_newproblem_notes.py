import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_build_notes_message_translates_and_formats():
    from kouhai_bot.handlers.cmd import newproblem

    async def _run():
        stmt = {"notes": "Line A<br />Line B"}
        with patch.object(
            newproblem,
            "translate_sample_notes",
            AsyncMock(return_value=("第一行\n第二行", "")),
        ) as mocked_translate:
            result = await newproblem._build_notes_message(stmt)
        assert result == "样例解释：\n第一行\n第二行"
        mocked_translate.assert_awaited_once_with("Line A\nLine B", [])

    asyncio.run(_run())


def test_build_notes_message_falls_back_to_normalized_original():
    from kouhai_bot.handlers.cmd import newproblem

    async def _run():
        stmt = {
            "notes": (
                '<div class="x">First line</div>'
                '<div class="x">Second &amp; line</div>'
            )
        }
        with patch.object(
            newproblem,
            "translate_sample_notes",
            AsyncMock(return_value=(None, "")),
        ):
            result = await newproblem._build_notes_message(stmt)
        assert result == "样例解释：\nFirst line\nSecond & line"

    asyncio.run(_run())


def test_build_notes_message_strips_leaked_thinking_without_rewriting_text():
    from kouhai_bot.handlers.cmd import newproblem

    async def _run():
        stmt = {"notes": "placeholder"}
        raw = (
            "<think>internal translation notes</think>"
            "A \\xrightarrow[l=1,\\,r=6]{} B, and x \\lt y \\gt z, "
            "with p \\leq q \\ge r and s \\oplus t."
        )
        expected = (
            "A \\xrightarrow[l=1,\\,r=6]{} B, and x \\lt y \\gt z, "
            "with p \\leq q \\ge r and s \\oplus t."
        )
        with patch.object(
            newproblem,
            "translate_sample_notes",
            AsyncMock(return_value=(raw, "")),
        ):
            result = await newproblem._build_notes_message(stmt)
        assert result == f"样例解释：\n{expected}"

    asyncio.run(_run())


def test_build_notes_message_returns_empty_on_translate_exception():
    from kouhai_bot.handlers.cmd import newproblem

    async def _run():
        stmt = {"notes": "Line A<br />Line B"}
        with patch.object(
            newproblem,
            "translate_sample_notes",
            AsyncMock(side_effect=RuntimeError("llm down")),
        ):
            result = await newproblem._build_notes_message(stmt)
        assert result == ""

    asyncio.run(_run())
