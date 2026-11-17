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
  background: #f7f7f7; text-align: center; padding: 14px 12px;
  font-size: 16px; color: #1a1a1a; font-weight: 500;
  border-bottom: 1px solid #e0e0e0;
}
.body { padding: 14px 12px 20px; }
.msg { display: flex; align-items: flex-start; margin-bottom: 18px; }
.avatar {
  width: 38px; height: 38px; border-radius: 6px; flex: 0 0 38px;
  color: #fff; font-size: 17px; font-weight: 600;
  display: flex; align-items: center; justify-content: center;
  margin-right: 10px; user-select: none;
}
img.avatar { object-fit: cover; display: block; }
.col { flex: 1 1 auto; min-width: 0; }
.nick { font-size: 12px; color: #8a8a8a; margin-bottom: 5px; }
.bubble {
  background: #fff; border-radius: 10px; padding: 10px 12px;
  font-size: 15px; color: #1a1a1a; line-height: 1.55;
  word-break: break-word; overflow-wrap: anywhere;
  display: inline-block; max-width: 100%;
}
.bubble pre {
  font-family: "SF Mono", "Menlo", "Consolas", monospace;
  font-size: 13px; background: #f5f5f5; border-radius: 6px;
  padding: 8px 10px; margin: 6px 0; white-space: pre-wrap;
  word-break: break-all; line-height: 1.45;
}
.bubble img { max-width: 100%; border-radius: 6px; display: block; margin: 6px 0; }
.bubble .muted { color: #8a8a8a; font-size: 13px; }
.bubble h3 { font-size: 15px; margin: 4px 0; }
.footer { text-align: center; color: #b0b0b0; font-size: 12px; padding: 8px 0 4px; }
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