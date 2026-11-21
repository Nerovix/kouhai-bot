import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_build_notes_message_generates_and_formats():
    from kouhai_bot import problem_preparation

    async def _run():
        stmt = {
            "description": "Full statement",
            "input": "Input spec",
            "time_limit": "2 seconds",
            "memory_limit": "256 megabytes",
            "samples": [{"input": "1 2", "output": "3"}],
            "notes": "Line A<br />Line B",
            "images": [{"src": "https://x/g.png", "kind": "graphic"}],
        }
        with patch.object(
            problem_preparation,
            "generate_sample_explanation",
            AsyncMock(return_value=("第一行\n第二行", "『M』")),
        ) as mocked_generate:
            result = await problem_preparation.build_notes_message(stmt, summary="中文摘要")
        assert result == "样例解释：\n第一行\n第二行"
        kwargs = mocked_generate.await_args.kwargs
        assert kwargs["statement_text"] == "Full statement"
        assert kwargs["input_text"] == "Input spec"
        assert kwargs["limits_text"] == "Time: 2 seconds, Memory: 256 megabytes"
        assert kwargs["samples"] == [{"input": "1 2", "output": "3"}]
        assert kwargs["notes"] == "Line A\nLine B"
        assert kwargs["summary"] == "中文摘要"
        assert kwargs["images"] == stmt["images"]

    asyncio.run(_run())


def test_build_notes_message_without_original_notes_skips_generation():
    from kouhai_bot import problem_preparation

    async def _run():
        with patch.object(
            problem_preparation,
            "generate_sample_explanation",
            AsyncMock(),
        ) as mocked_generate:
            assert await problem_preparation.build_notes_message({"notes": ""}) == ""
            assert await problem_preparation.build_notes_message({}) == ""
        mocked_generate.assert_not_awaited()

    asyncio.run(_run())


def test_build_notes_message_strips_legacy_figure_markers():
    from kouhai_bot import problem_preparation

    async def _run():
        stmt = {"notes": "See the tree:\n\n[[FIG_1]]\n\nThen answer."}
        with patch.object(
            problem_preparation,
            "generate_sample_explanation",
            AsyncMock(return_value=("树的形状如下：一个根连着两个叶子。", "")),
        ) as mocked_generate:
            result = await problem_preparation.build_notes_message(stmt)
        assert mocked_generate.await_args.kwargs["notes"] == "See the tree:\n\nThen answer."
        assert result == "样例解释：\n树的形状如下：一个根连着两个叶子。"

    asyncio.run(_run())


def test_build_notes_message_strips_leaked_thinking_without_rewriting_text():
    from kouhai_bot import problem_preparation

    async def _run():
        stmt = {"notes": "placeholder"}
        raw = (
            "<thinking>internal explanation notes</thinking>"
            "A \\xrightarrow[l=1,\\,r=6]{} B, and x \\lt y \\gt z, "
            "with p \\leq q \\ge r and s \\oplus t."
        )
        expected = (
            "A \\xrightarrow[l=1,\\,r=6]{} B, and x \\lt y \\gt z, "
            "with p \\leq q \\ge r and s \\oplus t."
        )
        with patch.object(
            problem_preparation,
            "generate_sample_explanation",
            AsyncMock(return_value=(raw, "")),
        ):
            result = await problem_preparation.build_notes_message(stmt)
        assert result == f"样例解释：\n{expected}"

    asyncio.run(_run())


def test_build_notes_message_retries_then_returns_empty_on_exception():
    from kouhai_bot import problem_preparation

    async def _run():
        stmt = {"notes": "Line A<br />Line B"}
        with patch.object(
            problem_preparation,
            "generate_sample_explanation",
            AsyncMock(side_effect=RuntimeError("llm down")),
        ) as mocked_generate:
            result = await problem_preparation.build_notes_message(stmt)
        assert result == ""
        assert mocked_generate.await_count == problem_preparation.NOTES_ATTEMPTS

    asyncio.run(_run())