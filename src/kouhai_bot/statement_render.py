"""Render problem-card text into QQ-friendly PNG images."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import textwrap
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import get_config


_LATEX_INLINE_RE = re.compile(r"(\$\$.*?\$\$|\$.*?\$|\\\(.*?\\\)|\\\[.*?\\\])", re.S)


def image_message_from_path(path: str) -> list[dict]:
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return [{"type": "image", "data": {"file": f"base64://{b64}"}}]


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simsun.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def _display_text(text: str) -> str:
    """Make LaTeX delimiters look intentional inside the rendered image."""
    def repl(match: re.Match[str]) -> str:
        token = match.group(0).strip()
        token = token.removeprefix("$$").removesuffix("$$")
        token = token.removeprefix("$").removesuffix("$")
        token = token.removeprefix(r"\(").removesuffix(r"\)")
        token = token.removeprefix(r"\[").removesuffix(r"\]")
        return token.strip()

    return _LATEX_INLINE_RE.sub(repl, text)


def _wrap_line(line: str, width: int) -> list[str]:
    if not line:
        return [""]
    chunks: list[str] = []
    for part in line.splitlines() or [line]:
        wrapped = textwrap.wrap(
            part,
            width=width,
            replace_whitespace=False,
            drop_whitespace=False,
            break_long_words=True,
            break_on_hyphens=False,
        )
        chunks.extend(wrapped or [""])
    return chunks


def render_text_to_png(
    text: str,
    *,
    group_id: int,
    slug: str,
    max_width: int = 980,
) -> str:
    """Render text into a PNG and return its path.

    This keeps formulas in the image layer instead of sending raw LaTeX as QQ
    text. It intentionally avoids embedding problem id/title; callers control
    the text passed in.
    """
    cfg = get_config()
    root = Path(cfg.data_dir) / "groups" / str(group_id) / "rendered"
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    path = root / f"{slug}-{digest}-{int(time.time())}.png"

    body_font = _font(26)
    code_font = _font(24)
    title_font = _font(30, bold=True)
    padding_x = 34
    padding_y = 30
    line_gap = 10
    para_gap = 14
    wrap_width = 52

    normalized = _display_text(text).replace("\r\n", "\n").replace("\r", "\n").strip()
    paragraphs = normalized.split("\n")
    draw_probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    rendered: list[tuple[str, ImageFont.ImageFont, int]] = []
    for raw in paragraphs:
        font = title_font if raw.strip().endswith(":") and len(raw.strip()) <= 24 else body_font
        if raw.startswith(("Input:", "Output:")) or re.match(r"^\s*\d+(\s+\d+)*\s*$", raw):
            font = code_font
        lines = _wrap_line(raw, wrap_width)
        for line in lines:
            rendered.append((line, font, line_gap))
        rendered[-1] = (rendered[-1][0], rendered[-1][1], para_gap)

    content_width = max_width - padding_x * 2
    height = padding_y * 2
    for line, font, gap in rendered:
        bbox = draw_probe.textbbox((0, 0), line or " ", font=font)
        height += (bbox[3] - bbox[1]) + gap
    height = max(height, 160)

    image = Image.new("RGB", (max_width, height), "#fbfbf8")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (10, 10, max_width - 10, height - 10),
        radius=18,
        fill="#ffffff",
        outline="#d8d6cc",
        width=2,
    )
    y = padding_y
    for line, font, gap in rendered:
        draw.text((padding_x, y), line, fill="#1f2933", font=font)
        bbox = draw.textbbox((padding_x, y), line or " ", font=font)
        y += (bbox[3] - bbox[1]) + gap

    image.save(path, format="PNG", optimize=True)
    return str(path)
