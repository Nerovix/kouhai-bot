import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.problems.picker import _extract_samples, _normalize_sample_block


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


def _legacy_sample_html() -> str:
    """Old template: plain <pre> without attributes."""
    return """
    <div class="sample-tests"><div class="section-title">Example</div>
    <div class="sample-test">
      <div class="input"><div class="title">Input</div>
        <pre>3\n1 2 3</pre></div>
      <div class="output"><div class="title">Output</div>
        <pre>6</pre></div>
      <div class="input"><div class="title">Input</div>
        <pre>2\n5 5</pre></div>
      <div class="output"><div class="title">Output</div>
        <pre>10</pre></div>
    </div></div>
    <div class="note"><div class="section-title">Note</div></div>
    """


def _modern_sample_html() -> str:
    """New template: <pre id=...> with Copy button, multi-line rows as divs."""
    return """
    <div class="sample-tests"><div class="section-title">Example</div>
    <div class="sample-test">
      <div class="input"><div class="title">Input<div title="Copy" id="c1" class="input-output-copier">Copy</div></div>
        <pre id="p1"><div class="test-example-line test-example-line-even test-example-line-0">3</div><div class="test-example-line test-example-line-odd test-example-line-1">5</div></pre></div>
      <div class="output"><div class="title">Output<div title="Copy" id="c2" class="input-output-copier">Copy</div></div>
        <pre id="p2">4 1 2 3 4 -1 0</pre></div>
    </div></div>
    <div class="note"><div class="section-title">Note</div></div>
    """


def test_extract_samples_legacy_template():
    samples = _extract_samples(_legacy_sample_html())
    assert samples == [
        {"input": "3\n1 2 3", "output": "6"},
        {"input": "2\n5 5", "output": "10"},
    ]


def test_extract_samples_modern_template_with_pre_ids_and_multiline_rows():
    samples = _extract_samples(_modern_sample_html())
    assert samples == [
        {"input": "3\n5", "output": "4 1 2 3 4 -1 0"},
    ]


def test_extract_samples_no_container_returns_empty():
    assert _extract_samples("<div class='problem-statement'><p>no samples</p></div>") == []


def test_extract_samples_skips_pair_without_pre():
    html = """
    <div class="sample-test">
      <div class="input"><div class="title">Input</div>
        <pre>1</pre></div>
      <div class="output"><div class="title">Output</div>
        <p>missing pre here</p></div>
      <div class="input"><div class="title">Input</div>
        <pre>2</pre></div>
      <div class="output"><div class="title">Output</div>
        <pre>4</pre></div>
    </div>
    """
    samples = _extract_samples(html)
    assert samples == [{"input": "2", "output": "4"}]
