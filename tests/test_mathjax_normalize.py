"""MathJax-rendered CF pages must not triple formula text.

Playwright-fetched CF pages carry every inline formula three times in the DOM
text (visible <nobr> rendering, assistive MathML, and the TeX <script>), so
naive tag-stripping turned ``$$$1$$$`` into ``111``. These tests pin the
normalization that keeps exactly the TeX-source copy.

Regression: CF 1989D sample note rendered as "111-st ... 999 ingots ...
121212 experience points".
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.problems import fetcher
from kouhai_bot.problems.picker import fetch_statement  # noqa: F401  (import path smoke)


def _mathjax_formula(tex: str, rendered: str, element_id: int) -> str:
    """Reconstruct MathJax v2 output for one inline formula, exactly as
    served on rendered CF pages (structure captured from CF 1989D)."""
    return (
        '<span class="MathJax_Preview" style="color: inherit;"></span>'
        f'<span class="MathJax" id="MathJax-Element-{element_id}-Frame" tabindex="0" '
        f'data-mathml="&lt;math xmlns=&quot;http://www.w3.org/1998/Math/MathML&quot;&gt;&lt;mn&gt;{rendered}&lt;/mn&gt;&lt;/math&gt;" '
        'role="presentation" style="position: relative;">'
        '<nobr aria-hidden="true">'
        f'<span class="math" id="MathJax-Span-{element_id * 2}" style="width: 0.6em; display: inline-block;">'
        '<span style="display: inline-block; position: relative; width: 0.5em; height: 0px; font-size: 122%;">'
        '<span style="position: absolute; clip: rect(1.3em, 1000.4em, 2.3em, -1000em); top: -2.1em; left: 0em;">'
        f'<span class="mrow" id="MathJax-Span-{element_id * 2 + 1}">'
        f'<span class="mn" id="MathJax-Span-{element_id * 2 + 2}" style="font-family: MathJax_Main;">{rendered}</span>'
        '</span><span style="display: inline-block; width: 0px; height: 2.1em;"></span>'
        '</span></span>'
        '<span style="display: inline-block; overflow: hidden; vertical-align: -0.09em; border-left: 0px solid; width: 0px; height: 0.9em;"></span>'
        '</span></nobr>'
        f'<span class="MJX_Assistive_MathML" role="presentation"><math xmlns="http://www.w3.org/1998/Math/MathML"><mn>{rendered}</mn></math></span>'
        '</span>'
        f'<script type="math/tex" id="MathJax-Element-{element_id}">{tex}</script>'
    )


def _note_html() -> str:
    """Playwright-rendered Note block for CF 1989D (digit-bearing formulas)."""
    return (
        '<div class="note"><div class="section-title">Note</div>'
        "<p>In the first example, you can do the following: </p><ol> <li> craft one weapon of the "
        + _mathjax_formula("1", "1", 26) + "-st class from the "
        + _mathjax_formula("1", "1", 27) + "-st type of metal, spending "
        + _mathjax_formula("9", "9", 29)
        + " ingots; </li><li> melt that weapon, returning "
        + _mathjax_formula("8", "8", 30)
        + " ingots of the "
        + _mathjax_formula("1", "1", 31)
        + "-st metal type; </li></ol> In the end you'll have "
        + _mathjax_formula("c = [2, 4, 2]", "c=[2,4,2]", 42)
        + " ingots left. In total, you've crafted "
        + _mathjax_formula("6", "6", 43)
        + " weapons and melted "
        + _mathjax_formula("6", "6", 44)
        + " weapons, gaining "
        + _mathjax_formula("12", "12", 45)
        + " experience points in total.</div></div>"
    )


def test_normalize_mathjax_keeps_single_copy():
    html = _mathjax_formula("9", "9", 29)
    out = fetcher.normalize_mathjax(html)
    assert "<nobr" not in out
    assert "MJX_Assistive_MathML" not in out
    assert "MathJax_Preview" not in out
    # 剥标签后只剩一份 TeX 源码
    text = re.sub(r"<[^>]+>", "", out)
    assert text == "9"


def test_html_to_text_no_tripled_digits():
    text = fetcher.html_to_text(_note_html())
    assert "111" not in text
    assert "999" not in text
    assert "888" not in text
    assert "121212" not in text
    assert "666" not in text
    assert "c = [2, 4, 2]" in text
    assert "gaining 12 experience points" in text


def test_html_to_text_plain_markup_unchanged():
    """Unrendered CF pages (cloudscraper path) keep $$$...$$$ handling."""
    html = "<p>craft one weapon of the $$$1$$$-st class, spending $$$9$$$ ingots</p>"
    text = fetcher.html_to_text(html)
    assert text == "craft one weapon of the 1-st class, spending 9 ingots"


def test_normal_script_content_dropped():
    html = "<p>before</p><script>var x = 1;</script><p>after</p>"
    out = fetcher.normalize_mathjax(html)
    assert "var x" not in out
    assert "<p>before</p>" in out and "<p>after</p>" in out


def test_display_mode_tex_script_kept():
    """MathJax v2 display formulas use type='math/tex; mode=display'."""
    html = (
        '<p>Consider</p>'
        '<span class="MathJax_Preview"></span>'
        '<span class="MathJax" id="MathJax-Element-50-Frame">'
        '<nobr aria-hidden="true"><span class="math">\\sum_{i=1}^{n} i</span></nobr>'
        '<span class="MJX_Assistive_MathML" role="presentation">'
        '<math><munderover><mo>&#8721;</mo><mrow><mi>i</mi><mo>=</mo><mn>1</mn></mrow>'
        '<mrow><mi>n</mi></mrow></munderover></math></span></span>'
        '<script type="math/tex; mode=display">\\sum_{i=1}^{n} i</script>'
        '<p>equals n(n+1)/2.</p>'
    )
    out = fetcher.normalize_mathjax(html)
    text = re.sub(r"<[^>]+>", "", out)
    assert "\\sum_{i=1}^{n} i" in text
    assert text.count("\\sum") == 1


def test_picker_notes_extraction_no_duplication():
    """End-to-end: the picker's Note extraction yields clean digits."""
    from kouhai_bot.problems import picker

    ps_html = '<div class="problem-statement">' + _note_html() + "</div>"
    note_m = re.search(
        r'<div class="section-title">Note</div>([\s\S]*?)(?=<div class="section-title"|$)',
        ps_html,
        re.DOTALL,
    )
    assert note_m
    note = picker.cf_statement.normalize_mathjax(note_m.group(1))
    note = re.sub(r"<[^>]+>", "", note)
    note = re.sub(r"\s+", " ", note).strip()
    note = re.sub(r"\$\$\$|\$\$|\$", "", note)
    assert "1-st class" in note
    assert "spending 9 ingots" in note
    assert "returning 8 ingots" in note
    assert "c = [2, 4, 2] ingots left" in note
    assert "crafted 6 weapons and melted 6 weapons" in note
    assert "gaining 12 experience points" in note
    assert not re.search(r"(\d)\1{2,}", note)
