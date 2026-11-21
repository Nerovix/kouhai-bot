#!/usr/bin/env python3
"""
cf_statement.py — Fetch CF problem statement and collect image metadata.

Usage:
  python cf_statement.py 542 D                    # contestId + index
  python cf_statement.py 542D                     # combined pid
  python cf_statement.py --url https://codeforces.com/problemset/problem/542/D

VL backends (set via --vl-backend or CF_VL_BACKEND env):
  - qwen     Qwen-VL API (needs QWEN_API_KEY, QWEN_BASE_URL)
  - openai   OpenAI-compatible VL (needs OPENAI_API_KEY, OPENAI_BASE_URL)
  - none     Dry-run only

Returns:
  - images: tex-formula / tex-graphics metadata for multimodal consumers
  - has_non_formula_images: True if problem contains tex-graphics diagrams
"""

import argparse
import base64
import html as html_lib
import json
import os
import re
import sys
import urllib.request
from html.parser import HTMLParser
from io import BytesIO
from urllib.parse import urljoin, urlparse

from PIL import Image

try:
    from . import cf_fetcher
except ImportError:  # Support direct execution of this file.
    import cf_fetcher

# ── Config ──────────────────────────────────────────────────────────────

DEFAULT_VL_BACKEND = os.environ.get("CF_VL_BACKEND", "none")

QWEN_API_KEY = os.environ.get("QWEN_API_KEY", "")
QWEN_BASE_URL = os.environ.get("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
QWEN_MODEL = os.environ.get("QWEN_MODEL", "").strip()

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")

# Hallucination patterns — if VL output matches these, it's likely garbage
_HALLUCINATION_PATTERNS = [
    r"\\documentclass",
    r"\\usepackage",
    r"\\begin\{document\}",
    r"\\begin\{tikzpicture\}",
    r"\\usetikzlibrary",
]

# Max reasonable LaTeX length for a single CF formula
_MAX_FORMULA_LENGTH = 400

# Context window size (chars) to extract around each formula image
_CONTEXT_WINDOW = 300


def _qwen_config() -> tuple[str, str, str]:
    api_key = QWEN_API_KEY
    base_url = QWEN_BASE_URL
    model = QWEN_MODEL

    try:
        from ..config import get_config

        cfg = get_config()
        api_key = api_key or cfg.qwen_api_key
        base_url = os.environ.get("QWEN_BASE_URL", "").strip() or cfg.qwen_base_url
        model = model or cfg.qwen_model
    except Exception:
        pass

    return api_key.strip(), base_url.rstrip("/"), model.strip()


# ── Fetch problem page ──────────────────────────────────────────────────

def fetch_problem_html(
    contest_id: int,
    index: str,
    *,
    fetcher: str = "auto",
    pw_wait_ms: int = 7000,
) -> tuple[str, str]:
    """Fetch CF problem page, return (html, pid)."""
    url = f"https://codeforces.com/problemset/problem/{contest_id}/{index}"
    body = cf_fetcher.fetch_html(url, fetcher=fetcher, pw_wait_ms=pw_wait_ms)
    return body, f"{contest_id}{index}"


# ── Extract problem statement block ─────────────────────────────────────

def extract_problem_statement(html: str) -> str | None:
    """Extract the <div class='problem-statement'> HTML block.

    Uses div-depth counting instead of assuming a ``</div><script`` terminator:
    Codeforces serves template variants where the statement container is not
    followed by a script tag, and a naive non-greedy regex then truncates or
    misses the block entirely.
    """
    m = re.search(r'<div class="problem-statement"[^>]*>', html)
    if not m:
        return None
    start = m.start()
    depth = 1
    pos = m.end()
    for dm in re.finditer(r"<div\b[^>]*>|</div\s*>", html[pos:], re.I):
        if dm.group(0).startswith("</"):
            depth -= 1
        else:
            depth += 1
        if depth <= 0:
            return html[start : pos + dm.end()]
    return None


# ── Find formula / graphics images ──────────────────────────────────────

def normalize_image_src(src: str) -> str:
    """Return an absolute Codeforces image URL."""
    value = (src or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        return "https:" + value
    return urljoin("https://codeforces.com", value)


_STATEMENT_IMAGE_HOSTS = ("espresso.codeforces.com", "codeforces.com")


def _is_plain_figure(src: str, cls: str) -> bool:
    """Whether a non-tex ``<img>`` is a statement figure worth rendering.

    Codeforces hosts both formulas and diagrams on ``espresso.codeforces.com``;
    the ``tex-formula``/``tex-graphics`` classes distinguish them. Some template
    variants emit ordinary ``<img>`` tags (no tex class) for board/diagram
    figures, so treat CF-hosted images without the formula class as figures.
    Icons and UI chrome on other hosts are ignored.
    """
    if "tex-formula" in cls:
        return False
    if "icon" in cls.lower():
        return False
    host = urlparse(src).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in _STATEMENT_IMAGE_HOSTS)


_TABLE_RE = re.compile(r"<table\b[^>]*>([\s\S]*?)</table>", re.I)
_TABLE_ROW_RE = re.compile(r"<tr\b[^>]*>([\s\S]*?)</tr>", re.I)
_TABLE_CELL_RE = re.compile(r"<td\b[^>]*>([\s\S]*?)</td>", re.I)
_CAPTION_TEXT_RE = re.compile(r"^(?:Figure|Fig\.?)\s*\d+[A-Za-z]?$", re.I)
_CAPTION_CELL_RE = re.compile(
    r"<td\b[^>]*>\s*(?:Figure|Fig\.?)\s*\d+[A-Za-z]?\s*</td>", re.I
)
# Block-level elements that carry the description prose/figures.
_DESCRIPTION_BLOCK_RE = re.compile(
    r"<(p|center|ul|ol)\b[^>]*>[\s\S]*?</\1\s*>", re.I
)
_DESCRIPTION_END_RE = re.compile(r'<div class="input-specification"', re.I)
_FIGURE_REFERENCE_RE = re.compile(r"\bFig(?:ure)?s?\.?\s*\d", re.I)
_FIGURE_PLACEHOLDER = "[[FIG_{}]]"


def figure_captions(ps_html: str) -> dict[str, str]:
    """Map figure image URL → its statement caption ("Figure 1").

    Codeforces renders labelled figure grids as a two-row table: images in
    one row, labels in the same columns of the next row.
    """
    captions: dict[str, str] = {}
    for table in _TABLE_RE.finditer(ps_html):
        rows = [
            [cell.group(1) for cell in _TABLE_CELL_RE.finditer(row.group(1))]
            for row in _TABLE_ROW_RE.finditer(table.group(1))
        ]
        for cells, label_cells in zip(rows, rows[1:]):
            if len(cells) != len(label_cells):
                continue
            for cell, label_cell in zip(cells, label_cells):
                img = re.search(r'<img\b[^>]*\bsrc="([^"]+)"', cell, re.I)
                if not img:
                    continue
                label = html_to_text(label_cell)
                if not _CAPTION_TEXT_RE.fullmatch(label):
                    continue
                src = normalize_image_src(img.group(1))
                if src:
                    captions[src] = label
    return captions


def extract_figure_passage(ps_html: str) -> str:
    """Description prose that explains the statement figures, in reading order.

    Figures are meaningless on their own: CF introduces them in the
    surrounding paragraph(s) ("... as shown in Figure 1") and often ties them
    to the samples afterwards. This returns that contiguous block run with
    ``[[FIG_n]]`` placeholders (n = figure position, matching the graphics
    order used by the card) so the card can render each figure inline where
    it is referenced. Returns "" when the statement has no figures or no
    prose around them.
    """
    _formulas, graphics = find_all_tex_images(ps_html)
    if not graphics:
        return ""
    figure_numbers = {
        item["src"]: index for index, item in enumerate(graphics, 1)
    }
    end_match = _DESCRIPTION_END_RE.search(ps_html)
    region = ps_html[: end_match.start()] if end_match else ps_html

    blocks: list[tuple[str, bool]] = []
    for match in _DESCRIPTION_BLOCK_RE.finditer(region):
        html_block = match.group(0)
        has_figure = False

        def replace_img(img_match: re.Match[str]) -> str:
            nonlocal has_figure
            tag = img_match.group(0)
            src_match = re.search(r'\bsrc="([^"]+)"', tag, re.I)
            if not src_match:
                return ""
            src = normalize_image_src(src_match.group(1))
            number = figure_numbers.get(src)
            if number is None:
                # Inline formula image: keep the explanation text readable.
                return ""
            has_figure = True
            return f" {_FIGURE_PLACEHOLDER.format(number)} "

        html_block = _CAPTION_CELL_RE.sub("", html_block)
        html_block = re.sub(r"<img\b[^>]*>", replace_img, html_block, flags=re.I)
        text = html_to_text(html_block)
        if text:
            blocks.append((text, has_figure))

    if not any(has_figure for _text, has_figure in blocks):
        return ""
    first = next(i for i, (_text, has) in enumerate(blocks) if has)
    stop = max(i for i, (_text, has) in enumerate(blocks) if has) + 1
    while first > 0 and _FIGURE_REFERENCE_RE.search(blocks[first - 1][0]):
        first -= 1
    while stop < len(blocks) and _FIGURE_REFERENCE_RE.search(blocks[stop][0]):
        stop += 1
    return "\n\n".join(text for text, _has in blocks[first:stop]).strip()


def extract_figure_notes(ps_html: str) -> str:
    """Note-section prose with ``[[FIG_n]]`` markers where its figures appear.

    Sample explanations often embed diagrams ("The tree in the first test:
    <img> "). The plain ``notes`` field strips every tag, which silently drops
    those figures; keeping positional markers lets the card render each one
    inside the explanation that refers to it.
    """
    _formulas, graphics = find_all_tex_images(ps_html)
    if not graphics:
        return ""
    figure_numbers = {
        item["src"]: index for index, item in enumerate(graphics, 1)
    }
    note_match = re.search(
        r'<div class="section-title">Note</div>([\s\S]*?)(?=<div class="section-title"|$)',
        ps_html,
        re.DOTALL,
    )
    if not note_match:
        return ""
    note_html = _CAPTION_CELL_RE.sub("", note_match.group(1))
    has_figure = False

    def replace_img(img_match: re.Match[str]) -> str:
        nonlocal has_figure
        src_match = re.search(r'\bsrc="([^"]+)"', img_match.group(0), re.I)
        if not src_match:
            return ""
        number = figure_numbers.get(normalize_image_src(src_match.group(1)))
        if number is None:
            return ""
        has_figure = True
        return f" {_FIGURE_PLACEHOLDER.format(number)} "

    note_html = re.sub(r"<img\b[^>]*>", replace_img, note_html, flags=re.I)
    if not has_figure:
        return ""
    blocks = [
        html_to_text(block.group(0))
        for block in _DESCRIPTION_BLOCK_RE.finditer(note_html)
    ]
    blocks = [block for block in blocks if block]
    return "\n\n".join(blocks) if blocks else html_to_text(note_html)


def find_all_tex_images(ps_html: str) -> tuple[list[dict], list[dict]]:
    """Find all statement images, split into formulas and figures.

    Returns (formulas, graphics) — each a list of {tag, src, class, start, end}.
    formula = ``tex-formula`` math images.
    graphic = ``tex-graphics`` diagrams plus ordinary CF-hosted ``<img>`` figures.
    """
    formulas = []
    graphics = []
    for m in re.finditer(r'<img\b[^>]*>', ps_html, re.I):
        tag = m.group(0)
        src_m = re.search(r'src="([^"]+)"', tag, re.I)
        cls_m = re.search(r'class="([^"]*)"', tag, re.I)
        alt_m = re.search(r'alt="([^"]*)"', tag, re.I)
        cls = cls_m.group(1) if cls_m else ""
        src = normalize_image_src(src_m.group(1) if src_m else "")
        if not src:
            continue
        entry = {
            "tag": tag,
            "src": src,
            "class": cls,
            "alt": html_lib.unescape(alt_m.group(1)).strip() if alt_m else "",
            "start": m.start(),
            "end": m.end(),
        }
        if "tex-formula" in cls:
            formulas.append(entry)
        elif "tex-graphics" in cls or _is_plain_figure(src, cls):
            graphics.append(entry)
    return formulas, graphics


# ── VL backends ─────────────────────────────────────────────────────────

def image_to_base64_url(image_bytes: bytes, mime: str = "image/png") -> str:
    b64 = base64.b64encode(image_bytes).decode()
    return f"data:{mime};base64,{b64}"


def download_image(url: str) -> bytes:
    """Download an image from URL."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read()


def preprocess_formula_png(raw_bytes: bytes, upscale: int = 2) -> bytes:
    """Convert transparent PNG to white background + upscale for better OCR."""
    img = Image.open(BytesIO(raw_bytes))
    if img.mode in ("LA", "PA"):
        img = img.convert("RGBA")
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    if upscale > 1:
        w, h = img.size
        img = img.resize((w * upscale, h * upscale), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def call_qwen_vl(prompt: str, image_urls: list[str]) -> str | None:
    """Call Qwen-VL API. Returns text or None."""
    api_key, base_url, model = _qwen_config()
    if not api_key:
        print("  Qwen-VL error: qwen.api_key is not configured", file=sys.stderr)
        return None
    if not base_url:
        print("  Qwen-VL error: qwen.base_url is not configured", file=sys.stderr)
        return None
    if not model:
        print("  Qwen-VL error: QWEN_MODEL is not configured", file=sys.stderr)
        return None

    content = [{"type": "text", "text": prompt}]
    for url in image_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 500,
    }
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"  Qwen-VL error: {e}", file=sys.stderr)
        return None


def call_openai_vl(prompt: str, image_urls: list[str]) -> str | None:
    """Call OpenAI-compatible VL API. Returns text or None."""
    content = [{"type": "text", "text": prompt}]
    for url in image_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})

    payload = {
        "model": OPENAI_MODEL,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 500,
    }
    req = urllib.request.Request(
        f"{OPENAI_BASE_URL}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"  OpenAI VL error: {e}", file=sys.stderr)
        return None


VL_BACKENDS = {
    "qwen": call_qwen_vl,
    "openai": call_openai_vl,
}


# ── Context extraction ──────────────────────────────────────────────────

def extract_context(ps_html: str, start: int, end: int, window: int = _CONTEXT_WINDOW) -> str:
    """Extract surrounding plain text around a formula image position.
    Returns stripped text of ~window chars before and after, with HTML tags removed.
    """
    ctx_start = max(0, start - window)
    ctx_end = min(len(ps_html), end + window)
    ctx_html = ps_html[ctx_start:ctx_end]
    # Strip HTML tags, normalize whitespace
    text = re.sub(r"<[^>]+>", " ", ctx_html)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ── Hallucination detection ─────────────────────────────────────────────

def is_hallucination(latex_str: str | None) -> bool:
    """Check if VL output looks like a hallucination rather than a real formula."""
    if not latex_str or latex_str.startswith("(VL failed)") or latex_str.startswith("(download error"):
        return True
    if len(latex_str) > _MAX_FORMULA_LENGTH:
        return True
    for pattern in _HALLUCINATION_PATTERNS:
        if re.search(pattern, latex_str):
            return True
    return False


# ── VL call with retry ──────────────────────────────────────────────────

def call_vl_with_retry(
    vl_fn,
    b64_url: str,
    context: str,
    pid: str,
    formula_idx: int,
    max_retries: int = 2,
) -> str:
    """Call VL with context injection; retry on hallucination with stricter prompt."""
    base_prompt = (
        "Output ONLY the exact LaTeX source code of this mathematical formula. "
        "Use standard LaTeX: \\sum \\prod \\int \\frac \\gcd \\operatorname "
        "\\leq \\geq \\mid \\nmid \\bmod \\cdot \\times \\cdots \\substack. "
        "Preserve all subscript and superscript ranges exactly. "
        "NO explanation, NO markdown fences, just raw LaTeX."
    )

    # First attempt: with context
    prompt = f"Context from problem: {context}\n\n{base_prompt}"
    result = vl_fn(prompt, [b64_url])
    result = result.strip() if result else "(VL failed)"

    if not is_hallucination(result):
        return result

    print(f"  [{pid}] Formula {formula_idx}: possible hallucination, retrying...",
          file=sys.stderr)

    # Retry with stricter prompt (no context, lower temperature implicitly)
    for attempt in range(max_retries):
        strict_prompt = (
            "Look at this image carefully. It is a single mathematical formula from a "
            "competitive programming problem. Output ONLY its exact LaTeX. "
            "If it's a diagram or drawing, output the word DIAGRAM. "
            "Do NOT generate LaTeX document structure. "
            "Do NOT use \\documentclass, \\usepackage, \\begin, or \\end. "
            "Just the formula, nothing else."
        )
        result = vl_fn(strict_prompt, [b64_url])
        result = result.strip() if result else "(VL failed)"

        if is_hallucination(result):
            print(f"  [{pid}] Formula {formula_idx}: retry {attempt+1} still hallucinated",
                  file=sys.stderr)
            continue

        # Check if VL says it's a diagram
        if result.upper().strip() == "DIAGRAM":
            return "[DIAGRAM — not a formula]"

        return result

    return "(VL hallucination after retries)"


# ── HTML to text ────────────────────────────────────────────────────────

class _MathJaxNormalizer(HTMLParser):
    """Collapse MathJax-rendered formulas so each formula contributes one text copy.

    A Playwright-fetched CF page has MathJax (v2 HTML-CSS + assistive MathML)
    output for every inline formula, and each formula appears THREE times in
    the DOM text: the visible <nobr> rendering, the assistive
    ``MJX_Assistive_MathML`` <math> block, and the trailing
    ``<script type="math/tex">`` TeX source. Naive tag-stripping keeps all
    three, so ``$$$1$$$`` becomes ``111``. This normalizer keeps only the TeX
    source (equivalent to the ``$$$...$$$`` markup on unrendered pages) and
    drops the rendered duplicates.
    """

    _SKIP_TAGS = frozenset({"nobr", "mjx-math"})
    _SKIP_CLASS_TOKENS = ("MathJax_Preview", "MJX_Assistive_MathML")
    # HTML void elements have no end tag; they must not bump the skip depth.
    _VOID_TAGS = frozenset(
        {"area", "base", "br", "col", "embed", "hr", "img", "input",
         "link", "meta", "param", "source", "track", "wbr"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self._skip_depth = 0
        self._in_any_script = False
        self._in_tex_script = False

    def _should_skip(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        if tag in self._SKIP_TAGS:
            return True
        cls = dict(attrs).get("class", "") or ""
        return any(tok in cls for tok in self._SKIP_CLASS_TOKENS)

    def handle_starttag(self, tag, attrs):
        if self._skip_depth:
            if tag not in self._VOID_TAGS:
                self._skip_depth += 1
            return
        if self._should_skip(tag, attrs):
            self._skip_depth = 1
            return
        if tag == "script":
            self._in_any_script = True
            # MathJax v2 emits type="math/tex" (inline) or
            # "math/tex; mode=display" (display formulas).
            if (dict(attrs).get("type") or "").startswith("math/tex"):
                self._in_tex_script = True
            return
        tag_text = self.get_starttag_text() or ""
        self.parts.append(tag_text)

    def handle_startendtag(self, tag, attrs):
        if not self._skip_depth and not self._should_skip(tag, attrs):
            tag_text = self.get_starttag_text() or ""
            self.parts.append(tag_text)

    def handle_endtag(self, tag):
        if self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "script":
            self._in_any_script = False
            self._in_tex_script = False
            return
        self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        if self._skip_depth:
            return
        # Keep normal text and the TeX source of <script type="math/tex">;
        # drop the body of any other script tag.
        if self._in_any_script and not self._in_tex_script:
            return
        if self._in_tex_script:
            # Script bodies are raw text: a literal '<' would be mistaken for
            # markup by the later tag-stripping regex and delete the
            # inequality (e.g. "a < b" -> "a"). Escape it; html.unescape()
            # (or the raw-text path, which already shows &lt; like the
            # unrendered-page path) restores it.
            data = data.replace("<", "&lt;").replace(">", "&gt;")
        self.parts.append(data)

    def handle_entityref(self, name):
        # convert_charrefs=False: keep character references verbatim so the
        # later tag-stripping regex cannot mistake a decoded '<'/'&' for
        # markup; html.unescape() decodes them once afterwards. References
        # inside the kept TeX source are preserved too.
        if self._skip_depth or (self._in_any_script and not self._in_tex_script):
            return
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        if self._skip_depth or (self._in_any_script and not self._in_tex_script):
            return
        self.parts.append(f"&#{name};")


def normalize_mathjax(html_frag: str) -> str:
    """Return a copy of ``html_frag`` with MathJax rendered artifacts removed.

    Only the ``<script type="math/tex">`` TeX source survives per formula, so
    later tag-stripping yields one copy per formula (matching the
    ``$$$...$$$`` markup found on unrendered CF pages). Pages without
    MathJax output are returned untouched, preserving legacy behaviour.
    """
    if "MathJax" not in html_frag and "math/tex" not in html_frag:
        return html_frag
    parser = _MathJaxNormalizer()
    parser.feed(html_frag)
    parser.close()
    return "".join(parser.parts)


def html_to_text(ps_html: str) -> str:
    """Strip HTML tags, normalize whitespace."""
    text = re.sub(r"<[^>]+>", "", normalize_mathjax(ps_html))
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\$\$\$|\$\$|\$", "", text)
    return text


# ── Main pipeline ───────────────────────────────────────────────────────

def process_problem(
    contest_id: int,
    index: str,
    vl_backend: str = "none",
    *,
    html: str | None = None,
) -> dict:
    """Fetch or reuse HTML, extract the statement, and return text + metadata.

    Returns dict with keys:
      - pid, url, text, text_length
      - formulas_found, formulas_processed
      - formula_details: list of {src, latex}
      - has_non_formula_images: True if tex-graphics (diagrams) found
      - formulas_failed: number of formulas that couldn't be converted after retries
    """
    pid = f"{contest_id}{index}"
    if html is None:
        html, pid = fetch_problem_html(contest_id, index)
    elif not cf_fetcher.content_valid(html):
        return {"error": "Unusable Codeforces HTML", "pid": pid}
    ps_html = extract_problem_statement(html)
    if not ps_html:
        return {"error": "Could not find problem-statement div", "pid": pid}

    formulas, graphics = find_all_tex_images(ps_html)
    has_non_formula_images = len(graphics) > 0

    print(f"[{pid}] Found {len(formulas)} formula(s), {len(graphics)} graphic(s)",
          file=sys.stderr)

    if has_non_formula_images:
        print(f"  [{pid}] ⚠ contains tex-graphics (diagrams)",
              file=sys.stderr)

    # Process formula images
    formula_results = []
    formulas_failed = 0

    if formulas and vl_backend in VL_BACKENDS:
        vl_fn = VL_BACKENDS[vl_backend]
        for i, fm in enumerate(formulas):
            print(f"  [{pid}] Formula {i+1}/{len(formulas)}: {fm['src'][:60]}...",
                  file=sys.stderr)
            try:
                # Download + preprocess
                img_bytes = download_image(fm["src"])
                img_bytes = preprocess_formula_png(img_bytes)
                b64_url = image_to_base64_url(img_bytes)

                # Extract context from surrounding text
                context = extract_context(ps_html, fm["start"], fm["end"])

                # Call VL with retry + hallucination detection
                desc = call_vl_with_retry(vl_fn, b64_url, context, pid, i + 1)

                formula_results.append({
                    "src": fm["src"],
                    "latex": desc,
                })
                print(f"    → {desc[:100]}", file=sys.stderr)

                if desc.startswith("(VL") or "[DIAGRAM" in desc:
                    formulas_failed += 1

            except Exception as e:
                formula_results.append({
                    "src": fm["src"],
                    "latex": f"(error: {e})",
                })
                formulas_failed += 1
                print(f"    → error: {e}", file=sys.stderr)

    elif formulas:
        print(f"  [{pid}] VL backend not configured, skipping {len(formulas)}",
              file=sys.stderr)
        for fm in formulas:
            formula_results.append({"src": fm["src"], "latex": "[FORMULA IMAGE]"})

    # Also mark graphics images in results (for reporting)
    for gm in graphics:
        formula_results.append({
            "src": gm["src"],
            "latex": "[DIAGRAM IMAGE]",
        })

    # Replace image tags with stable inline markers, so multimodal consumers can
    # align each image with its exact location in the statement text.
    result_html = ps_html
    ordered_images = sorted(formulas + graphics, key=lambda x: x["start"])
    caption_by_src = figure_captions(ps_html)
    image_entries: list[dict] = []
    for i, item in enumerate(ordered_images, 1):
        kind = "formula" if item in formulas else "graphic"
        marker = f"IMAGE_{i}"
        alt = str(item.get("alt", "") or "").strip()
        inline_hint = f": {alt}" if kind == "formula" and 0 < len(alt) <= 500 else ""
        image_entries.append({
            "src": item["src"],
            "kind": kind,
            "class": item.get("class", ""),
            "alt": alt,
            "context": extract_context(ps_html, item["start"], item["end"]),
            "marker": marker,
            "placeholder": f"[[{marker}: {kind}{inline_hint}]]",
            "start": item["start"],
            "end": item["end"],
        })
        if kind == "graphic":
            caption = caption_by_src.get(item["src"], "")
            if caption:
                image_entries[-1]["caption"] = caption

    for img_entry in sorted(image_entries, key=lambda x: x["start"], reverse=True):
        replacement = (
            '<span class="tex-formula-text">'
            f'{html_lib.escape(img_entry["placeholder"])}'
            '</span>'
        )
        result_html = (
            result_html[:img_entry["start"]] + replacement + result_html[img_entry["end"]:]
        )

    plain_text = html_to_text(result_html)

    return {
        "pid": pid,
        "url": f"https://codeforces.com/problemset/problem/{contest_id}/{index}",
        "formulas_found": len(formulas),
        "graphics_found": len(graphics),
        "has_non_formula_images": has_non_formula_images,
        "formulas_processed": len(formulas) - formulas_failed,
        "formulas_failed": formulas_failed,
        "formula_details": formula_results,
        "images": [
            {k: v for k, v in item.items() if k not in {"start", "end"}}
            for item in image_entries
        ],
        "figure_passage": extract_figure_passage(ps_html),
        "text": plain_text,
        "text_length": len(plain_text),
    }


# ── CLI ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Fetch CF problem statement with formula handling"
    )
    parser.add_argument(
        "args", nargs="*", help="contestId index, or combined pid, or nothing with --url"
    )
    parser.add_argument("--url", help="Full CF problem URL")
    parser.add_argument(
        "--vl-backend",
        choices=list(VL_BACKENDS) + ["none"],
        default=DEFAULT_VL_BACKEND,
        help="VL backend for formula images",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Only detect formulas, don't call VL"
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    vl_backend = "none" if args.dry_run else args.vl_backend

    # Parse contestId + index
    if args.url:
        m = re.match(r".*/problem/(\d+)/(\w+)", args.url)
        if not m:
            print("Error: could not parse --url", file=sys.stderr)
            sys.exit(1)
        contest_id, index = int(m.group(1)), m.group(2)
    elif len(args.args) == 1:
        m = re.match(r"(\d+)(\w+)", args.args[0])
        if not m:
            print("Error: could not parse pid", file=sys.stderr)
            sys.exit(1)
        contest_id, index = int(m.group(1)), m.group(2)
    elif len(args.args) >= 2:
        contest_id, index = int(args.args[0]), args.args[1]
    else:
        parser.print_help()
        sys.exit(1)

    result = process_problem(contest_id, index, vl_backend)
    if "error" in result:
        print(f"Error: {result['error']}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        json_out = {k: v for k, v in result.items() if k != "text"}
        json_out["text_preview"] = result["text"][:500]
        print(json.dumps(json_out, indent=2, ensure_ascii=False))
    else:
        print(f"=== CF{result['pid']} ===")
        print(f"URL: {result['url']}")
        print(f"Formulas: {result['formulas_found']} found, {result['formulas_processed']} processed")
        if result["has_non_formula_images"]:
            print("⚠ Contains statement images (diagrams) — requires a vision-capable llm.general_model provider")
        if result["formulas_failed"]:
            print(f"⚠ {result['formulas_failed']} formula(s) failed")
        for fr in result.get("formula_details", []):
            src_short = fr["src"][:60]
            print(f"  {src_short}")
            print(f"    → {fr.get('latex', '?')[:120]}")
        print(f"\n--- Problem text ({result['text_length']} chars) ---")
        print(result["text"][:2000])


if __name__ == "__main__":
    main()
