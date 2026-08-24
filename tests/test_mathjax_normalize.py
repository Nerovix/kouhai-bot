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


def test_normal_script_content_dropped_when_mathjax_present():
    """Once the normalizer is active (MathJax present), non-tex script
    bodies are dropped."""
    html = (
        '<span class="MathJax_Preview"></span>'
        "<p>before</p><script>var x = 1;</script><p>after</p>"
    )
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


def test_escaped_inequalities_survive_tag_stripping():
    """Character references must not be decoded before tag-stripping, or
    '<' would be mistaken for markup and the text deleted."""
    html = (
        '<p>for all i &lt; n and x &gt;= y</p>'
        + _mathjax_formula("a_i \\le 5", "a_i\\le5", 60)
        + "<p>after</p>"
    )
    out = fetcher.normalize_mathjax(html)
    # normalize keeps the references verbatim; only html_to_text's final
    # unescape decodes them, after tag-stripping.
    stripped = re.sub(r"<[^>]+>", "", out)
    assert "i &lt; n" in stripped and "x &gt;= y" in stripped
    assert "after" in stripped
    text = fetcher.html_to_text(html)
    assert "for all i < n and x >= y" in text
    assert "after" in text


def test_void_element_inside_skipped_block_keeps_depth_balanced():
    html = (
        '<span class="MathJax" id="MathJax-Element-70-Frame">'
        '<nobr aria-hidden="true"><span class="math">a<br>'
        '<img src="/x.png"></span></nobr>'
        '<span class="MJX_Assistive_MathML"><math><mi>a</mi></math></span></span>'
        '<script type="math/tex">a</script>'
        "<p>tail text must survive</p>"
    )
    out = fetcher.normalize_mathjax(html)
    text = re.sub(r"<[^>]+>", "", out)
    assert "tail text must survive" in text
    assert "a" in text


def test_mathjax_v3_container_keeps_assistive_mathml_text():
    """MathJax v3 has no legacy TeX <script>; the container's assistive
    <math> is the only text source and must be preserved, not skipped."""
    html = (
        "<p>answer is </p>"
        '<mjx-container class="MathJax" jax="CHTML" display="false">'
        '<mjx-math class="MJX-TEX" aria-hidden="true">'
        '<mjx-mn class="mjx-n"><mjx-c class="mjx-c39"></mjx-c></mjx-mn>'
        "</mjx-math>"
        '<math xmlns="http://www.w3.org/1998/Math/MathML"><mn>9</mn></math>'
        "</mjx-container>"
        "<p>.</p>"
    )
    out = fetcher.normalize_mathjax(html)
    text = re.sub(r"<[^>]+>", "", out)
    assert "9" in text
    assert text.count("9") == 1


def test_plain_script_page_untouched():
    """Pages without MathJax output take the fast path: scripts keep their
    legacy behaviour (content preserved after tag-stripping)."""
    html = "<p>a</p><script>var x = 1;</script><p>b</p>"
    assert fetcher.normalize_mathjax(html) is html
    assert fetcher.normalize_mathjax(html) == html


def test_tex_script_literal_inequality_survives():
    """Script bodies are raw text, so a literal '<' inside math/tex must be
    escaped before tag-stripping or the inequality is deleted."""
    html = (
        '<span class="MathJax_Preview"></span>'
        '<span class="MathJax" id="MathJax-Element-90-Frame">'
        '<nobr aria-hidden="true"><span class="math">a &lt; b</span></nobr>'
        '<span class="MJX_Assistive_MathML"><math><mi>a</mi><mo>&lt;</mo><mi>b</mi></math></span>'
        "</span>"
        # literal '<' is legal raw text inside <script>
        '<script type="math/tex">a < b</script>'
        "<p>then tail</p>"
    )
    out = fetcher.normalize_mathjax(html)
    stripped = re.sub(r"<[^>]+>", "", out)
    # the literal '<' was escaped, so tag-stripping must not eat it
    assert "a &lt; b" in stripped
    assert "then tail" in stripped
    # html_to_text unescapes back to a real '<'
    assert "a < b" in fetcher.html_to_text(html)


def test_tex_script_preencoded_entity_not_double_escaped():
    """A TeX source that already contains '&lt;' must not become '&amp;lt;'."""
    html = (
        '<span class="MathJax_Preview"></span>'
        '<script type="math/tex">x &lt; y &gt; z</script>'
        "<p>tail</p>"
    )
    out = fetcher.normalize_mathjax(html)
    stripped = re.sub(r"<[^>]+>", "", out)
    assert "&amp;lt;" not in stripped
    assert "x &lt; y &gt; z" in stripped
    assert "x < y > z" in fetcher.html_to_text(html)


def test_mathjax_v3_utext_not_duplicated():
    """v3 visible subtree (mjx-math, incl. mjx-utext real text) is skipped;
    only the assistive MathML copy survives."""
    html = (
        "<p>value is </p>"
        '<mjx-container class="MathJax" jax="CHTML" display="false">'
        '<mjx-math class="MJX-TEX" aria-hidden="true">'
        '<mjx-utext variant="normal">世界</mjx-utext>'
        "</mjx-math>"
        '<mjx-assistive-mml unselectable="on"><math xmlns="http://www.w3.org/1998/Math/MathML">'
        "<mtext>hello 世界</mtext></math></mjx-assistive-mml>"
        "</mjx-container>"
        "<p>.</p>"
    )
    out = fetcher.normalize_mathjax(html)
    text = re.sub(r"<[^>]+>", "", out)
    assert text.count("世界") == 1
    assert "hello 世界" in text


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
