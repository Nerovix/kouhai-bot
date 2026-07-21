from types import SimpleNamespace

from kouhai_bot import config
from kouhai_bot.problems import fetcher


def test_qwen_config_uses_yaml_config_when_env_missing(monkeypatch):
    monkeypatch.setattr(fetcher, "QWEN_API_KEY", "")
    monkeypatch.setattr(fetcher, "QWEN_BASE_URL", "https://env.example/v1")
    monkeypatch.setattr(fetcher, "QWEN_MODEL", "")
    monkeypatch.setattr(
        config,
        "_config",
        SimpleNamespace(
            qwen_api_key="sk-from-config",
            qwen_base_url="https://dashscope.example/v1/",
            qwen_model="qwen-vl-test",
        ),
    )
    monkeypatch.delenv("QWEN_BASE_URL", raising=False)

    assert fetcher._qwen_config() == (
        "sk-from-config",
        "https://dashscope.example/v1",
        "qwen-vl-test",
    )


def test_process_problem_marks_inline_image_position(monkeypatch):
    problem_html = (
        '<div class="problem-statement">'
        '<p>Choose x <img class="tex-formula" src="/predownloaded/a.png" alt="a_i &lt; b_i"> such that y.</p>'
        "</div><script>"
    )
    monkeypatch.setattr(
        fetcher,
        "fetch_problem_html",
        lambda contest_id, index: (problem_html, f"{contest_id}{index}"),
    )

    result = fetcher.process_problem(1, "A", vl_backend="none")

    assert "Choose x [[IMAGE_1: formula: a_i < b_i]] such that y." in result["text"]
    assert result["images"] == [
        {
            "src": "https://codeforces.com/predownloaded/a.png",
            "kind": "formula",
            "class": "tex-formula",
            "alt": "a_i < b_i",
            "context": "Choose x such that y.",
            "marker": "IMAGE_1",
            "placeholder": "[[IMAGE_1: formula: a_i < b_i]]",
        }
    ]


def test_fetch_problem_html_uses_shared_fetcher(monkeypatch):
    calls = []

    def fake_fetch(url, *, fetcher, pw_wait_ms):
        calls.append((url, fetcher, pw_wait_ms))
        return "<html>statement</html>"

    monkeypatch.setattr(fetcher.cf_fetcher, "fetch_html", fake_fetch)

    html, pid = fetcher.fetch_problem_html(
        1534,
        "F2",
        fetcher="playwright",
        pw_wait_ms=321,
    )

    assert html == "<html>statement</html>"
    assert pid == "1534F2"
    assert calls == [
        ("https://codeforces.com/problemset/problem/1534/F2", "playwright", 321)
    ]
