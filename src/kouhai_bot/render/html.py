"""Mobile QQ merged-forward card replica → HTML for long-image rendering.

A opened forward card reads as a vertical list of chat bubbles: avatar +
nickname + white bubble. Official events deliver ``author.avatar`` (实测: a
public JPEG URL); the render layer pre-fetches it into a base64 data URI
(``avatar_data`` on a node). When absent, avatars fall back to deterministic
initial-letter color circles.
"""

from __future__ import annotations

import html as _html
from typing import Any

_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  background: #ededed;
  font-family: -apple-system, "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
  -webkit-font-smoothing: antialiased;
}
.header {
  background: #f7f7f7; text-align: center; padding: 22px 16px;
  font-size: 28px; color: #1a1a1a; font-weight: 500;
  border-bottom: 1px solid #e0e0e0;
}
.body { padding: 24px 20px 28px; }
.msg { display: flex; align-items: flex-start; margin-bottom: 28px; }
.avatar {
  width: 52px; height: 52px; border-radius: 8px; flex: 0 0 52px;
  color: #fff; font-size: 26px; font-weight: 600;
  display: flex; align-items: center; justify-content: center;
  margin-right: 14px; user-select: none;
}
img.avatar { object-fit: cover; display: block; }
.col { flex: 1 1 auto; min-width: 0; }
.nick { font-size: 20px; color: #8a8a8a; margin-bottom: 8px; }
.bubble {
  background: #fff; border-radius: 12px; padding: 16px 18px;
  font-size: 28px; color: #1a1a1a; line-height: 1.5;
  word-break: break-word; overflow-wrap: anywhere;
  display: inline-block; max-width: min(520px, 100%);
}
.bubble pre {
  font-family: "SF Mono", "Menlo", "Consolas", monospace;
  font-size: 24px; background: #f5f5f5; border-radius: 8px;
  padding: 12px 14px; margin: 8px 0; white-space: pre-wrap;
  word-break: break-all; line-height: 1.45;
}
.bubble img { max-width: 100%; border-radius: 8px; display: block; margin: 8px 0; }
.bubble .muted { color: #8a8a8a; font-size: 24px; }
.bubble h3 { font-size: 28px; margin: 6px 0; }
.bubble ul { margin: 8px 0 4px; padding-left: 26px; }
.bubble li { margin: 7px 0; }
.bubble strong { font-weight: 600; }
.bubble .short { color: #9a9a9a; font-size: 22px; }
.bubble sup { font-size: 0.75em; }
.footer { text-align: center; color: #b0b0b0; font-size: 20px; padding: 12px 0 6px; }
"""


def escape(text: str) -> str:
    return _html.escape(str(text), quote=True)


def text_to_html(text: str) -> str:
    return escape(text).replace("\n", "<br>")


def pre_block(text: str) -> str:
    return f"<pre>{escape(text)}</pre>"


def _avatar_color(name: str) -> str:
    h = 0
    for ch in name or "?":
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    hue = h % 360
    return f"hsl({hue}, 48%, 58%)"


def _initial(name: str) -> str:
    name = (name or "?").strip()
    return escape(name[0].upper()) if name else "?"


def build_forward_card_html(
    nodes: list[dict[str, Any]],
    *,
    title: str = "聊天记录",
    footer: str = "",
) -> str:
    """nodes: [{nickname, content_html}]. content_html is trusted builder HTML."""
    rows = []
    for node in nodes:
        nickname = str(node.get("nickname", "") or "")
        content_html = node.get("content_html", "")
        avatar_data = str(node.get("avatar_data", "") or "")
        if avatar_data:
            avatar_html = f'<img class="avatar" src="{avatar_data}" alt="">'
        else:
            avatar_html = (
                f'<div class="avatar" style="background:{_avatar_color(nickname)}">'
                f"{_initial(nickname)}</div>"
            )
        rows.append(
            '<div class="msg">'
            f"{avatar_html}"
            '<div class="col">'
            f'<div class="nick">{escape(nickname)}</div>'
            f'<div class="bubble">{content_html}</div>'
            "</div></div>"
        )
    footer_html = f'<div class="footer">{escape(footer)}</div>' if footer else ""
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        f"<style>{_CSS}</style></head><body>"
        f'<div class="header">{escape(title)}</div>'
        f'<div class="body">{"".join(rows)}</div>'
        f"{footer_html}"
        "</body></html>"
    )


# ── Scoreboard: light text enhancement (same style as /help) ────────────

_SB_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _fmt_points(points: float) -> str:
    return f"{float(points):.2f}".rstrip("0").rstrip(".")


def build_scoreboard_html(
    ranked: list[dict[str, Any]], *, migration_note: str = ""
) -> str:
    """Inner bubble HTML for /scoreboard, styled like /help: a bold heading,
    a muted formula line (real superscript instead of a raw ``2^`` caret), and
    a simple bullet list with bold nicknames. Keeps the chat-record card form
    rather than a designed poster. ``ranked`` entries carry rank/nickname/
    solved/score (posts.group_scoreboard). Nicknames are escaped; plain-text
    fallback stays with the caller. ``migration_note`` (legacy service group
    only) renders as an extra muted line under the formula.
    """
    total = len(ranked)
    parts = [
        f'<h3>📊 累计解题排行榜 <span class="muted">共 {total} 人</span></h3>',
        '<div class="muted">计分：2000 分 = 1 分，每 +300 分翻倍'
        '（2<sup>(rating−2000)/300</sup>）</div>',
    ]
    if migration_note:
        parts.append(f'<div class="muted">{escape(migration_note)}</div>')
    items = []
    for entry in ranked:
        rank = int(entry.get("rank", 0) or 0)
        nickname = escape(str(entry.get("nickname", "") or ""))
        solved = int(entry.get("solved", 0) or 0)
        points = _fmt_points(entry.get("score", 0) or 0)
        rank_text = _SB_MEDALS.get(rank) or f"#{rank}"
        items.append(
            f"<li>{rank_text} <strong>{nickname}</strong> — {solved} 题 / {points} 分</li>"
        )
    parts.append(f'<ul>{"".join(items)}</ul>')
    return "".join(parts)