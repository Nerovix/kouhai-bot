"""Statement figure caption mapping (image metadata for the explanation LLM)."""

from kouhai_bot.problems import fetcher

PROBLEM_HTML = (
    '<div class="problem-statement">'
    "<p>Consider the grid shown in Figure 1 and remember it.</p>"
    '<center><table class="tex-tabular"><tr>'
    '<td><img class="tex-graphics" src="/predownloaded/g1.png"></td>'
    '<td><img class="tex-graphics" src="/predownloaded/g2.png"></td>'
    "</tr><tr><td>Figure 1</td><td>Figure 2</td></tr></table></center>"
    "<p>Figure 2 shows the state after the move.</p>"
    "</div><div class=\"input-specification\"><p>Input</p></div>"
)


def test_figure_captions_map_sources_to_labels():
    captions = fetcher.figure_captions(PROBLEM_HTML)
    assert captions == {
        "https://codeforces.com/predownloaded/g1.png": "Figure 1",
        "https://codeforces.com/predownloaded/g2.png": "Figure 2",
    }


def test_figure_captions_empty_without_graphics():
    html = (
        '<div class="problem-statement"><p>No diagrams here.</p></div>'
        '<div class="input-specification"></div>'
    )
    assert fetcher.figure_captions(html) == {}