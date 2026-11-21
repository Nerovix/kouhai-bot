"""Statement figure captions and the inline figure passage."""

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


def test_figure_passage_keeps_referencing_prose_and_markers():
    passage = fetcher.extract_figure_passage(PROBLEM_HTML)
    assert passage == (
        "Consider the grid shown in Figure 1 and remember it.\n\n"
        "[[FIG_1]] [[FIG_2]]\n\n"
        "Figure 2 shows the state after the move."
    )


def test_figure_passage_empty_without_graphics():
    html = (
        '<div class="problem-statement"><p>No diagrams here.</p></div>'
        '<div class="input-specification"></div>'
    )
    assert fetcher.extract_figure_passage(html) == ""


NOTE_HTML = (
    '<div class="problem-statement">'
    '<div class="section-title">Input</div><p>n</p>'
    '<div class="section-title">Note</div>'
    '<div class="note">'
    '<p>The tree in the first test: '
    '<img class="tex-graphics" src="/predownloaded/n1.png"> One optimal permutation.</p>'
    '<p>The tree in the third test: '
    '<img class="tex-graphics" src="/predownloaded/n2.png"></p>'
    '</div></div>'
)


def test_figure_notes_keep_markers_at_note_positions():
    notes = fetcher.extract_figure_notes(NOTE_HTML)
    assert notes == (
        "The tree in the first test: [[FIG_1]] One optimal permutation.\n\n"
        "The tree in the third test: [[FIG_2]]"
    )


def test_figure_notes_empty_without_note_graphics():
    html = (
        '<div class="problem-statement"><p>Figure 1 here.</p>'
        '<img class="tex-graphics" src="/predownloaded/d1.png">'
        '<div class="section-title">Note</div><p>Plain note.</p></div>'
    )
    assert fetcher.extract_figure_notes(html) == ""