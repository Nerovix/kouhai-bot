import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import cf_tutorial_agent as agent
from kouhai_bot.llm import ChatCompletionResult


def _write_statement(statements_dir: Path, pid: str = "1000B") -> None:
    statements_dir.mkdir(parents=True, exist_ok=True)
    (statements_dir / f"{pid}.json").write_text(
        json.dumps(
            {
                "name": "Target Problem",
                "description": "Given an array, find the maximum subarray sum.",
                "input": "n and array a",
                "output": "maximum sum",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _problem_page() -> str:
    return """
    <html><body>
      <div class="title">B. Target Problem</div>
      <a href="/blog/entry/1">Announcement</a>
      <a href="/blog/entry/2">Tutorial</a>
    </body></html>
    """


def _blog_page(body: str, title: str = "Editorial") -> str:
    return f"""
    <html>
      <head><title>{title}</title></head>
      <body><div class="ttypography">{body}</div></body>
    </html>
    """


def test_extract_blog_links_prefers_tutorial_text():
    links = agent.extract_blog_links(_problem_page(), "https://codeforces.com/problemset/problem/1000/B", limit=2)
    assert links == [
        "https://codeforces.com/blog/entry/2",
        "https://codeforces.com/blog/entry/1",
    ]


def test_agent_selects_high_confidence_candidate(tmp_path):
    _write_statement(tmp_path)

    pages = {
        "https://codeforces.com/problemset/problem/1000/B": _problem_page(),
        "https://codeforces.com/blog/entry/2": _blog_page(
            """
            <h4>A - Other</h4>
            <p>This solves a graph problem.</p>
            <h4>B - Target Problem</h4>
            <p>Use Kadane dynamic programming. Let dp be the best subarray ending here,
            update it with max(a_i, dp+a_i), and keep the best answer. This matches
            the maximum subarray objective and runs in linear time.</p>
            """
        ),
        "https://codeforces.com/blog/entry/1": _blog_page(
            "<h4>B - Target Problem</h4><p>Authors and preparation.</p>",
            title="Announcement",
        ),
    }

    def fake_fetch(url, **_kwargs):
        return pages[url]

    async def fake_chat_completion(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        ids = [row["id"] for row in payload["candidates"]]
        assert ids
        return ChatCompletionResult(
            text=json.dumps(
                {
                    "match": True,
                    "candidate_id": ids[0],
                    "confidence": 0.92,
                    "reason": "candidate discusses maximum subarray DP",
                }
            )
        )

    with patch("cf_tutorial_agent.fetch_html", side_effect=fake_fetch), \
            patch("cf_tutorial_agent.chat_completion", side_effect=fake_chat_completion):
        result = asyncio.run(
            agent.run_agent_for_pid(
                pid="1000B",
                statements_dir=tmp_path,
                fetcher="http",
                blog_limit=2,
                deadline_sec=10,
                selector_timeout_sec=5,
            )
        )

    assert result.confidence == 0.92
    assert result.bundle["tutorial_url"] == "https://codeforces.com/blog/entry/2"
    assert result.bundle["sections"][0]["label"] == "B"
    assert "Kadane" in result.bundle["sections"][0]["raw_text"]
    assert result.bundle["agent_meta"]["source_kind"] == "section"


def test_agent_rejects_low_confidence(tmp_path):
    _write_statement(tmp_path)
    pages = {
        "https://codeforces.com/problemset/problem/1000/B": _problem_page(),
        "https://codeforces.com/blog/entry/2": _blog_page(
            "<h4>B - Target Problem</h4><p>Some unrelated short text.</p>"
        ),
    }

    def fake_fetch(url, **_kwargs):
        return pages[url]

    async def fake_chat_completion(*_args, **_kwargs):
        return ChatCompletionResult(
            text=json.dumps(
                {
                    "match": True,
                    "candidate_id": "c1",
                    "confidence": 0.4,
                    "reason": "too little evidence",
                }
            )
        )

    with patch("cf_tutorial_agent.fetch_html", side_effect=fake_fetch), \
            patch("cf_tutorial_agent.chat_completion", side_effect=fake_chat_completion):
        try:
            asyncio.run(
                agent.run_agent_for_pid(
                    pid="1000B",
                    statements_dir=tmp_path,
                    fetcher="http",
                    blog_limit=1,
                    deadline_sec=10,
                    selector_timeout_sec=5,
                )
            )
        except agent.AgentNoMatch as exc:
            assert "selector_low_confidence" in str(exc)
        else:
            raise AssertionError("expected low confidence rejection")


def test_dynamic_candidate_used_when_static_section_is_placeholder(tmp_path):
    _write_statement(tmp_path)
    pages = {
        "https://codeforces.com/problemset/problem/1000/B": _problem_page(),
        "https://codeforces.com/blog/entry/2": _blog_page(
            "<h4>B - Target Problem</h4><p>Tutorial is loading...</p>"
        ),
    }

    def fake_fetch(url, **_kwargs):
        return pages[url]

    async def fake_chat_completion(*_args, **_kwargs):
        return ChatCompletionResult(
            text=json.dumps(
                {
                    "match": True,
                    "candidate_id": "c1",
                    "confidence": 0.9,
                    "reason": "dynamic tutorial matches",
                }
            )
        )

    with patch("cf_tutorial_agent.fetch_html", side_effect=fake_fetch), \
            patch("cf_tutorial_agent.fetch_dynamic_editorial", return_value=("Target Problem", "Dynamic official explanation " * 8)), \
            patch("cf_tutorial_agent.chat_completion", side_effect=fake_chat_completion):
        result = asyncio.run(
            agent.run_agent_for_pid(
                pid="1000B",
                statements_dir=tmp_path,
                fetcher="http",
                blog_limit=1,
                deadline_sec=10,
                selector_timeout_sec=5,
            )
        )

    assert result.bundle["agent_meta"]["source_kind"] == "dynamic"
    assert "Dynamic official explanation" in result.bundle["sections"][0]["solution"]



def test_repair_concatenated_heading_from_old_cf_blog():
    section = agent.Section(
        label="C",
        title="Sereja and SubsequencesIt is clear that we need to calculate the sum of products. " * 3,
        hint="",
        solution="",
        code_blocks=[],
        raw_text="",
    )

    repaired = agent._repair_concatenated_heading(section, "Sereja and Subsequences")

    assert repaired.title == "Sereja and Subsequences"
    assert repaired.solution.startswith("It is clear")
    assert len(repaired.solution) >= agent.MIN_EDITORIAL_LEN


def test_parse_old_problem_sections_handles_problem_headings():
    text = "Problem A\nEasy text.\n\nPorblem B\n" + ("Target explanation. " * 10) + "\n\nProblem C\nOther text."
    sections = agent.parse_old_problem_sections(text)
    labels = [section.label for section in sections]
    assert "A" in labels
    assert "B" in labels
    b = [section for section in sections if section.label == "B"][0]
    assert "Target explanation" in b.solution


def test_selector_disables_reasoning_effort(monkeypatch):
    calls = []
    candidate = agent.EditorialCandidate(
        candidate_id="c1",
        tutorial_url="https://codeforces.com/blog/entry/1",
        tutorial_title="Editorial",
        source_kind="old_problem_section",
        label="G",
        title="Problem G",
        section=agent.Section(
            label="G",
            title="Problem G",
            hint="",
            solution="Detailed official explanation " * 8,
            code_blocks=[],
            raw_text="Detailed official explanation " * 8,
        ),
    )

    async def fake_chat_completion(*args, **kwargs):
        calls.append(kwargs)
        return ChatCompletionResult(
            text=json.dumps({"match": True, "candidate_id": "c1", "confidence": 0.9, "reason": "ok"})
        )

    monkeypatch.setattr(agent, "chat_completion", fake_chat_completion)
    selected, confidence, _reason = asyncio.run(
        agent.select_candidate(
            pid="39G",
            problem_title="Inverse Function",
            problem_text="statement",
            candidates=[candidate],
            timeout=5,
            llm_text_limit=1000,
        )
    )

    assert selected.candidate_id == "c1"
    assert confidence == 0.9
    assert calls[0]["send_reasoning_effort"] is False
