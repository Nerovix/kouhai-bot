"""Resolve Codeforces statement/editorial figures into embeddable data URIs.

The long-image card renders offline from embedded ``data:`` URIs (the same
approach used for the decorative snake image), so each remote figure is
downloaded once into a disk cache and reused across renders. Figures are
best-effort: a failed download is dropped rather than failing the card.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html as _html
import logging
import re
import urllib.request
from pathlib import Path

from ..config import get_config

logger = logging.getLogger("kouhai-bot.render.figures")

MAX_FIGURES = 12
_FETCH_TIMEOUT_SEC = 15
_USER_AGENT = "Mozilla/5.0"

_FIG_MARKER_RE = re.compile(r"\[\[FIG_(\d+)\]\]")
_CAPTION_NUMBER_RE = re.compile(r"^(?:Figure|Fig\.?)\s*(\d+\w*)$", re.I)

_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _ext(src: str) -> str:
    path = src.split("?", 1)[0].split("#", 1)[0]
    suffix = Path(path).suffix.lower()
    return suffix if suffix in _MIME_BY_EXT else ".png"


def _cache_path(src: str) -> Path:
    key = hashlib.sha256(src.encode("utf-8")).hexdigest()[:32]
    return Path(get_config().data_dir) / "render_cache" / "statement_images" / f"{key}{_ext(src)}"


def _download(src: str) -> bytes | None:
    request = urllib.request.Request(src, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_SEC) as resp:
            return resp.read()
    except Exception as exc:
        logger.warning("figure download failed (%s): %s", src, exc)
        return None


def _to_data_uri(src: str, raw: bytes) -> str:
    mime = _MIME_BY_EXT.get(_ext(src), "image/png")
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _write_cache(path: Path, raw: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_bytes(raw)
        tmp.replace(path)
    except OSError as exc:
        logger.warning("figure cache write failed (%s): %s", path, exc)


async def _resolve_one(src: str) -> str | None:
    src = (src or "").strip()
    if not src:
        return None
    path = _cache_path(src)
    raw: bytes | None = None
    if path.is_file():
        try:
            raw = path.read_bytes()
        except OSError:
            raw = None
    if not raw:
        raw = await asyncio.to_thread(_download, src)
        if not raw:
            return None
        _write_cache(path, raw)
    return _to_data_uri(src, raw)


async def figure_data_uris(
    srcs: list[str] | tuple[str, ...],
) -> list[tuple[int, str]]:
    """Resolve figure URLs to ``(figure_number, data_uri)`` in source order.

    De-duplicated and capped; failed downloads are dropped. The number is the
    source position, so a failed figure leaves a gap instead of silently
    renumbering the rest — statements refer to figures by number ("图 4").
    """
    ordered = list(
        dict.fromkeys(
            str(item).strip() for item in (srcs or []) if str(item).strip()
        )
    )
    ordered = ordered[:MAX_FIGURES]
    if not ordered:
        return []
    results = await asyncio.gather(*(_resolve_one(src) for src in ordered))
    return [
        (index, uri)
        for index, uri in enumerate(results, 1)
        if uri
    ]


def split_figure_markers(text: str) -> list[tuple[str, str]]:
    """Split marked text into ``("text", chunk)`` / ``("figure", "3")`` items."""
    items: list[tuple[str, str]] = []
    position = 0
    value = text or ""
    for match in _FIG_MARKER_RE.finditer(value):
        chunk = value[position:match.start()]
        if chunk:
            items.append(("text", chunk))
        items.append(("figure", match.group(1)))
        position = match.end()
    tail = value[position:]
    if tail:
        items.append(("text", tail))
    return items


def strip_figure_markers(text: str) -> str:
    """Plain-text form of marked text for channels that cannot show images."""
    plain = _FIG_MARKER_RE.sub(lambda m: f"（图 {m.group(1)}）", text or "")
    return re.sub(r"\n{3,}", "\n\n", plain).strip()


def _display_caption(caption: str, number: int) -> str:
    match = _CAPTION_NUMBER_RE.fullmatch((caption or "").strip())
    if match:
        return f"图 {match.group(1)}"
    return caption.strip()


def figure_rows_html(
    figures: list[tuple[int, str]],
    captions: dict[int, str] | None = None,
) -> str:
    """Bubble HTML for a run of figures: a two-column caption grid.

    Consecutive figures stay together (CF renders them as a 2×2 table), so
    they sit next to the prose that refers to them instead of being dumped at
    the end of the card.
    """
    parts: list[str] = []
    for number, uri in figures:
        caption = _display_caption((captions or {}).get(number, ""), number)
        caption_html = (
            f"<figcaption>{_html.escape(caption)}</figcaption>" if caption else ""
        )
        parts.append(
            f'<figure><img src="{uri}" alt="{_html.escape(caption)}">'
            f"{caption_html}</figure>"
        )
    if not parts:
        return ""
    single = " single" if len(parts) == 1 else ""
    return f'<div class="figgrid{single}">{"".join(parts)}</div>'