import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.problems.picker import _normalize_sample_block


def test_normalize_sample_block_div_lines():
    raw = (
        '<div class="test-example-line test-example-line-even test-example-line-0">7</div>'
        '<div class="test-example-line test-example-line-odd test-example-line-1">4</div>'
        '<div class="test-example-line test-example-line-odd test-example-line-1">1 4 4</div>'
        '<div class="test-example-line test-example-line-odd test-example-line-1">1 2 3 4</div>'
    )
    assert _normalize_sample_block(raw) == "7\n4\n1 4 4\n1 2 3 4"


def test_normalize_sample_block_br_lines():
    raw = "5 3 5<br />5 -5 5 1 -4<br />2 1 2<br />"
    assert _normalize_sample_block(raw) == "5 3 5\n5 -5 5 1 -4\n2 1 2"


def test_process_problem_describes_tex_graphics(monkeypatch):
    import base64
    from kouhai_bot.problems import fetcher

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )
    html = (
        '<div class="problem-statement">'
        '<p>Consider the graph <img class="tex-graphics" src="//img.example/graph.png" /></p>'
        '</div><script></script>'
    )

    monkeypatch.setattr(
        fetcher,
        "fetch_problem_html",
        lambda contest_id, index: (html, f"{contest_id}{index}"),
    )
    monkeypatch.setattr(fetcher, "download_image", lambda url: png)
    fetcher.VL_BACKENDS["fake"] = (
        lambda prompt, image_urls: "three nodes labeled 1, 2, 3 with edges 1-2 and 2-3"
    )
    try:
        result = fetcher.process_problem(100, "A", vl_backend="fake")
    finally:
        fetcher.VL_BACKENDS.pop("fake", None)

    assert result["has_non_formula_images"] is True
    assert result["graphics_found"] == 1
    assert result["graphics_failed"] == 0
    assert result["graphics_details"][0]["src"] == "https://img.example/graph.png"
    assert "Diagram 1" in result["text"]
    assert "three nodes labeled 1, 2, 3" in result["text"]


def test_diagram_details_for_cache_keeps_description_and_src():
    from kouhai_bot.problems.picker import _diagram_details_for_cache

    diagrams = _diagram_details_for_cache([
        {"src": "https://img.example/a.png", "label": "Diagram 2", "description": "two arrows"},
        {"src": "", "description": ""},
        "bad",
    ])

    assert diagrams == [{
        "src": "https://img.example/a.png",
        "label": "Diagram 2",
        "description": "two arrows",
    }]
