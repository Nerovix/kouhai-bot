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
