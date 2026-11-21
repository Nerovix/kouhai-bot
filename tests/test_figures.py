"""Figure resolution and gallery HTML (render/figures.py + statement srcs)."""

from __future__ import annotations

import pytest

from kouhai_bot.config import BotConfig
from kouhai_bot.problem_content import statement_figure_srcs
from kouhai_bot.render import figures


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    config = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.config._config", config)
    return config


def test_ext_ignores_query_and_fragment_and_defaults_to_png():
    assert figures._ext("https://x/a/fig.PNG?token=1#frag") == ".png"
    assert figures._ext("https://x/a/photo.jpeg") == ".jpeg"
    assert figures._ext("https://x/a/noext") == ".png"
    assert figures._ext("https://x/a/weird.svg") == ".png"


def test_statement_figure_srcs_keeps_only_diagrams():
    statement = {
        "images": [
            {"src": "https://x/f1.png", "kind": "formula"},
            {"src": "https://x/g1.png", "kind": "graphic"},
            {"src": "  ", "kind": "graphic"},
            {"src": "https://x/g2.png", "kind": "graphic"},
            "not a dict",
        ]
    }
    assert statement_figure_srcs(statement) == [
        "https://x/g1.png",
        "https://x/g2.png",
    ]
    assert statement_figure_srcs({}) == []


async def test_resolve_keeps_source_numbers_when_one_figure_fails(cfg, monkeypatch):
    def fake_download(src):
        if "bad" in src:
            return None
        return b"RAW:" + src.encode()

    monkeypatch.setattr(figures, "_download", fake_download)

    result = await figures.figure_data_uris(
        ["https://x/a.png", "https://x/bad.png", "https://x/c.jpg"]
    )
    assert [number for number, _uri in result] == [1, 3]
    assert result[0][1].startswith("data:image/png;base64,")
    assert result[1][1].startswith("data:image/jpeg;base64,")


async def test_resolve_dedupes_and_caps(cfg, monkeypatch):
    monkeypatch.setattr(figures, "_download", lambda src: b"RAW")

    deduped = await figures.figure_data_uris(["https://x/a.png", "https://x/a.png"])
    assert [number for number, _uri in deduped] == [1]

    many = [f"https://x/{i}.png" for i in range(20)]
    capped = await figures.figure_data_uris(many)
    assert len(capped) == figures.MAX_FIGURES
    assert [number for number, _uri in capped] == list(range(1, figures.MAX_FIGURES + 1))


async def test_resolve_reuses_disk_cache_without_network(cfg, monkeypatch):
    calls = []

    def fake_download(src):
        calls.append(src)
        return b"RAW"

    monkeypatch.setattr(figures, "_download", fake_download)
    first = await figures.figure_data_uris(["https://x/a.png"])
    second = await figures.figure_data_uris(["https://x/a.png"])

    assert calls == ["https://x/a.png"]
    assert first == second


def test_resolve_empty_input_returns_empty():
    async def run():
        return await figures.figure_data_uris([])

    import asyncio

    assert asyncio.run(run()) == []


def test_figures_html_captions_use_source_numbers():
    html = figures.figures_html(
        [(1, "data:image/png;base64,AAA"), (4, "data:image/png;base64,BBB")]
    )
    assert html == (
        '<div class="figcap">图 1</div>'
        '<img src="data:image/png;base64,AAA" alt="图 1">'
        '<div class="figcap">图 4</div>'
        '<img src="data:image/png;base64,BBB" alt="图 4">'
    )
    assert figures.figures_html([]) == ""