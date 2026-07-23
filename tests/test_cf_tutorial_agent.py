import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

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


def test_collect_blog_documents_does_not_refetch_rejected_url():
    problem_url = "https://codeforces.com/problemset/problem/1000/B"
    rejected_url = "https://codeforces.com/blog/entry/2"
    remaining_url = "https://codeforces.com/blog/entry/1"
    pages = {
        problem_url: _problem_page(),
        remaining_url: _blog_page("<p>Remaining candidate body.</p>"),
    }
    fetched: list[str] = []

    def fake_fetch(url, **_kwargs):
        fetched.append(url)
        if url == rejected_url:
            raise AssertionError("rejected candidate must not be fetched again")
        return pages[url]

    with patch("cf_tutorial_agent.fetch_html", side_effect=fake_fetch), patch(
        "cf_tutorial_agent.fetch_dynamic_editorial",
        return_value=("", ""),
    ):
        _, _, blogs, incomplete = agent.collect_blog_documents(
            pid="1000B",
            fetcher="http",
            pw_wait_ms=100,
            blog_limit=2,
            excluded_tutorial_urls=frozenset({rejected_url}),
        )

    assert fetched == [problem_url, remaining_url]
    assert [blog.tutorial_url for blog in blogs] == [remaining_url]
    assert incomplete == ()


def test_collect_filters_rejected_urls_before_applying_candidate_limit():
    problem_url = "https://codeforces.com/problemset/problem/1000/B"
    rejected_urls = frozenset(
        f"https://codeforces.com/blog/entry/{idx}" for idx in range(1, 4)
    )
    remaining_url = "https://codeforces.com/blog/entry/4"
    problem_page = "".join(
        f'<a href="/blog/entry/{idx}">Tutorial {idx}</a>'
        for idx in range(1, 5)
    )
    fetched: list[str] = []

    def fake_fetch(url, **_kwargs):
        fetched.append(url)
        if url == problem_url:
            return problem_page
        if url in rejected_urls:
            raise AssertionError("rejected candidate must not consume the limit")
        assert url == remaining_url
        return _blog_page("<p>Remaining candidate body.</p>")

    with patch("cf_tutorial_agent.fetch_html", side_effect=fake_fetch), patch(
        "cf_tutorial_agent.fetch_dynamic_editorial",
        return_value=("", ""),
    ):
        _, _, blogs, incomplete = agent.collect_blog_documents(
            pid="1000B",
            fetcher="http",
            pw_wait_ms=100,
            blog_limit=1,
            excluded_tutorial_urls=rejected_urls,
        )

    assert fetched == [problem_url, remaining_url]
    assert [blog.tutorial_url for blog in blogs] == [remaining_url]
    assert incomplete == ()


def test_collect_marks_bounded_candidate_search_incomplete():
    problem_url = "https://codeforces.com/problemset/problem/1000/B"
    pages = {
        problem_url: _problem_page(),
        "https://codeforces.com/blog/entry/2": _blog_page(
            "<p>First candidate body.</p>"
        ),
    }

    with patch(
        "cf_tutorial_agent.fetch_html",
        side_effect=lambda url, **_kwargs: pages[url],
    ), patch(
        "cf_tutorial_agent.fetch_dynamic_editorial",
        return_value=("", ""),
    ):
        _, _, blogs, incomplete = agent.collect_blog_documents(
            pid="1000B",
            fetcher="http",
            pw_wait_ms=100,
            blog_limit=1,
        )

    assert len(blogs) == 1
    assert incomplete == ("candidate_limit_reached",)


def test_agent_extracts_editorial_from_full_blog_body(tmp_path):
    _write_statement(tmp_path)
    target_text = (
        "Use Kadane dynamic programming. Let dp be the best subarray ending here, "
        "update it with max(a_i, dp+a_i), and keep the best answer. This matches "
        "the maximum subarray objective and runs in linear time. " * 2
    )
    pages = {
        "https://codeforces.com/problemset/problem/1000/B": _problem_page(),
        "https://codeforces.com/blog/entry/2": _blog_page(
            f"""
            <h4>A - Other</h4>
            <p>This solves a graph problem.</p>
            <h4>B - Target Problem</h4>
            <p>{target_text}</p>
            """
        ),
        "https://codeforces.com/blog/entry/1": _blog_page("<p>Contest announcement only.</p>"),
    }

    def fake_fetch(url, **_kwargs):
        return pages[url]

    async def fake_chat_completion(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        assert "candidates" not in payload
        assert payload["blog"]["url"] == "https://codeforces.com/blog/entry/2"
        assert "A - Other" in payload["blog"]["body"]
        assert "B - Target Problem" in payload["blog"]["body"]
        return ChatCompletionResult(
            text=json.dumps(
                {
                    "match": True,
                    "section_title": "Target Problem",
                    "start_text": "Use Kadane dynamic programming.",
                    "end_text": "runs in linear time.",
                    "confidence": 0.92,
                    "reason": "blog section matches maximum subarray DP",
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
    assert result.bundle["agent_meta"]["source_kind"] == "llm_blog_extract"


def test_agent_tries_next_blog_when_extractor_rejects_first(tmp_path):
    _write_statement(tmp_path)
    target_text = ("The real solution uses prefix sums and dynamic programming over the array. " * 3) + "This is the unique final prefix marker."
    pages = {
        "https://codeforces.com/problemset/problem/1000/B": _problem_page(),
        "https://codeforces.com/blog/entry/2": _blog_page("<p>Only schedule and standings.</p>"),
        "https://codeforces.com/blog/entry/1": _blog_page(f"<p>{target_text}</p>"),
    }

    def fake_fetch(url, **_kwargs):
        return pages[url]

    async def fake_chat_completion(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        if payload["blog"]["url"].endswith("/2"):
            return ChatCompletionResult(text=json.dumps({"match": False, "reason": "announcement only"}))
        return ChatCompletionResult(
            text=json.dumps(
                {
                    "match": True,
                    "section_title": "Target Problem",
                    "start_text": "The real solution uses prefix sums",
                    "end_text": "unique final prefix marker.",
                    "confidence": 0.95,
                    "reason": "second blog has the target tutorial",
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

    assert result.selected_candidate_id == "b2"
    assert result.bundle["tutorial_url"] == "https://codeforces.com/blog/entry/1"
    assert "prefix sums" in result.bundle["sections"][0]["solution"]


def test_agent_keeps_placeholder_retryable_without_dynamic_fetch_or_llm(tmp_path):
    _write_statement(tmp_path)
    pages = {
        "https://codeforces.com/problemset/problem/1000/B": _problem_page(),
        "https://codeforces.com/blog/entry/2": _blog_page("<p>Tutorial is loading...</p>"),
    }

    def fake_fetch(url, **_kwargs):
        return pages[url]

    with patch("cf_tutorial_agent.fetch_html", side_effect=fake_fetch), \
            patch("cf_tutorial_agent.fetch_dynamic_editorial") as dynamic_fetch, \
            patch("cf_tutorial_agent.chat_completion") as llm_call:
        with pytest.raises(agent.AgentIncomplete, match="invalid_blog_content"):
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

    dynamic_fetch.assert_not_called()
    llm_call.assert_not_called()


def test_agent_skips_placeholder_and_sends_only_valid_blog_to_llm(tmp_path):
    _write_statement(tmp_path)
    target_text = (
        "Use prefix sums and dynamic programming to evaluate every transition. " * 3
    ) + "This is the unique final mixed marker."
    pages = {
        "https://codeforces.com/problemset/problem/1000/B": _problem_page(),
        "https://codeforces.com/blog/entry/2": _blog_page(
            "<p>Tutorial is loading...</p>"
        ),
        "https://codeforces.com/blog/entry/1": _blog_page(f"<p>{target_text}</p>"),
    }
    llm_urls = []

    def fake_fetch(url, **_kwargs):
        return pages[url]

    async def fake_chat_completion(messages, **_kwargs):
        payload = json.loads(messages[1]["content"])
        llm_urls.append(payload["blog"]["url"])
        assert "Tutorial is loading" not in payload["blog"]["body"]
        return ChatCompletionResult(
            text=json.dumps(
                {
                    "match": True,
                    "section_title": "Target Problem",
                    "start_text": "Use prefix sums and dynamic programming",
                    "end_text": "unique final mixed marker.",
                    "confidence": 0.94,
                    "reason": "valid blog contains the target tutorial",
                }
            )
        )

    with patch("cf_tutorial_agent.fetch_html", side_effect=fake_fetch), \
            patch("cf_tutorial_agent.fetch_dynamic_editorial", return_value=("", "")), \
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

    assert llm_urls == ["https://codeforces.com/blog/entry/1"]
    assert result.bundle["tutorial_url"] == "https://codeforces.com/blog/entry/1"


def test_agent_rejects_low_confidence_extraction(tmp_path):
    _write_statement(tmp_path)
    uncertain_text = ("Detailed but uncertain explanation with enough copied source text. " * 3) + "This is the unique final uncertain marker."
    problem_page = """
    <html><body>
      <div class="title">B. Target Problem</div>
      <a href="/blog/entry/2">Tutorial</a>
    </body></html>
    """
    pages = {
        "https://codeforces.com/problemset/problem/1000/B": problem_page,
        "https://codeforces.com/blog/entry/2": _blog_page(f"<p>{uncertain_text}</p>"),
    }

    def fake_fetch(url, **_kwargs):
        return pages[url]

    async def fake_chat_completion(*_args, **_kwargs):
        return ChatCompletionResult(
            text=json.dumps(
                {
                    "match": True,
                    "section_title": "Target Problem",
                    "start_text": "Detailed but uncertain explanation",
                    "end_text": "unique final uncertain marker.",
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
            assert "extractor_low_confidence" in str(exc)
        else:
            raise AssertionError("expected low confidence rejection")


def test_agent_keeps_incomplete_extractor_failure_retryable(tmp_path, monkeypatch):
    _write_statement(tmp_path)
    blog = agent.BlogDocument(
        blog_id="b1",
        tutorial_url="https://codeforces.com/blog/entry/1",
        tutorial_title="Editorial",
        body="Detailed candidate body " * 10,
    )

    monkeypatch.setattr(
        agent,
        "collect_blog_documents",
        lambda **_kwargs: (
            "https://codeforces.com/problemset/problem/1000/B",
            "Target Problem",
            [blog],
            (),
        ),
    )

    async def incomplete(**_kwargs):
        raise agent.AgentIncomplete("extractor_llm_failed:timeout")

    monkeypatch.setattr(agent, "extract_editorial_from_blog", incomplete)

    with pytest.raises(agent.AgentIncomplete, match="extractor_llm_failed"):
        asyncio.run(
            agent.run_agent_for_pid(
                pid="1000B",
                statements_dir=tmp_path,
                deadline_sec=10,
            )
        )


def test_extractor_empty_llm_result_is_incomplete(monkeypatch):
    blog = agent.BlogDocument(
        blog_id="b1",
        tutorial_url="https://codeforces.com/blog/entry/1",
        tutorial_title="Editorial",
        body="Detailed official explanation " * 10,
    )

    async def empty_result(*_args, **_kwargs):
        return ChatCompletionResult(text=None, failure_kind="timeout")

    monkeypatch.setattr(agent, "chat_completion", empty_result)

    with pytest.raises(agent.AgentIncomplete, match="extractor_llm_failed:timeout"):
        asyncio.run(
            agent.extract_editorial_from_blog(
                pid="1000B",
                problem_title="Target Problem",
                problem_text="statement",
                blog=blog,
                timeout=5,
                llm_text_limit=1000,
            )
        )


def test_extractor_uses_json_repair(monkeypatch):
    calls = []
    blog = agent.BlogDocument(
        blog_id="b1",
        tutorial_url="https://codeforces.com/blog/entry/1",
        tutorial_title="Editorial",
        body=("Detailed official explanation " * 8) + "unique final explanation marker",
    )

    async def fake_chat_completion(*args, **kwargs):
        calls.append(kwargs)
        return ChatCompletionResult(text='{match: true, section_title: "Target", start_text: "Detailed official explanation", end_text: "unique final explanation marker", confidence: 0.91, reason: "ok"}')

    async def fake_repair(text, **kwargs):
        assert text.startswith("{match: true")
        return {
            "match": True,
            "section_title": "Target",
            "start_text": "Detailed official explanation",
            "end_text": "unique final explanation marker",
            "confidence": 0.91,
            "reason": "ok",
        }, "R"

    monkeypatch.setattr(agent, "chat_completion", fake_chat_completion)
    monkeypatch.setattr(agent, "parse_json_with_llm_repair", fake_repair)

    selected, confidence, _reason = asyncio.run(
        agent.extract_editorial_from_blog(
            pid="39G",
            problem_title="Inverse Function",
            problem_text="statement",
            blog=blog,
            timeout=5,
            llm_text_limit=1000,
        )
    )

    assert selected.candidate_id == "b1"
    assert confidence == 0.91
    assert calls[0]["send_reasoning_effort"] is False


def test_extractor_disables_reasoning_effort(monkeypatch):
    calls = []
    blog = agent.BlogDocument(
        blog_id="b1",
        tutorial_url="https://codeforces.com/blog/entry/1",
        tutorial_title="Editorial",
        body=("Detailed official explanation " * 8) + "unique final explanation marker",
    )

    async def fake_chat_completion(*args, **kwargs):
        calls.append(kwargs)
        return ChatCompletionResult(
            text=json.dumps(
                {
                    "match": True,
                    "section_title": "Inverse Function",
                    "start_text": "Detailed official explanation",
                    "end_text": "unique final explanation marker",
                    "confidence": 0.9,
                    "reason": "ok",
                }
            )
        )

    monkeypatch.setattr(agent, "chat_completion", fake_chat_completion)
    selected, confidence, _reason = asyncio.run(
        agent.extract_editorial_from_blog(
            pid="39G",
            problem_title="Inverse Function",
            problem_text="statement",
            blog=blog,
            timeout=5,
            llm_text_limit=1000,
        )
    )

    assert selected.candidate_id == "b1"
    assert confidence == 0.9
    assert calls[0]["send_reasoning_effort"] is False
