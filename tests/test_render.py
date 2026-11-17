"""Long-image renderer + media cache + base64 upload tests."""

from __future__ import annotations

import base64

import pytest

from kouhai_bot.official import OfficialSender, ReplyBudget
from kouhai_bot.official import media as media_mod
from kouhai_bot.official.media import MediaCache, content_key
from kouhai_bot.official.rest import OfficialApiError
from kouhai_bot.render import (
    build_forward_card_html,
    nodes_to_text,
    pre_block,
    render_card_png,
    send_forward_card,
    text_to_html,
)
from kouhai_bot.render.browser import RenderBrowser


def test_text_to_html_escapes_and_breaks():
    assert text_to_html("a<b>\nc") == "a&lt;b&gt;<br>c"


def test_pre_block_escapes():
    assert pre_block("1 < 2") == "<pre>1 &lt; 2</pre>"


def test_build_card_html_structure():
    html = build_forward_card_html(
        [{"nickname": "Alice", "content_html": text_to_html("hello")}],
        title="题目卡片",
    )
    assert "题目卡片" in html
    assert "Alice" in html
    assert "hello" in html
    assert 'class="bubble"' in html
    assert 'class="avatar"' in html


def test_build_card_html_escapes_nickname():
    html = build_forward_card_html(
        [{"nickname": "<script>", "content_html": "x"}]
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_nodes_to_text_fallback():
    nodes = [
        {"nickname": "A", "content_html": "line1<br>line2"},
        {"nickname": "B", "content_text": "explicit"},
    ]
    text = nodes_to_text(nodes)
    assert "A：line1\nline2" in text
    assert "B：explicit" in text


# ── media cache ─────────────────────────────────────────────────────────

def test_media_cache_put_get_expiry(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(media_mod.time, "monotonic", lambda: t[0])
    cache = MediaCache()
    key = content_key(b"png")
    assert cache.get(key) is None
    cache.put(key, "FI", ttl_sec=300)
    assert cache.get(key) == "FI"
    t[0] += 280  # past ttl - margin
    assert cache.get(key) is None


# ── sender base64 upload ────────────────────────────────────────────────

class FakeRest:
    def __init__(self):
        self.sent = []
        self.uploads = []

    async def send_group_message(self, group_openid, payload):
        self.sent.append((group_openid, payload))
        return {"id": f"M{len(self.sent)}"}

    async def upload_group_media(self, group_openid, payload):
        self.uploads.append((group_openid, payload))
        return {"file_info": "FI", "file_uuid": "u", "ttl": 300}


async def test_send_image_bytes_base64_and_cache():
    rest = FakeRest()
    s = OfficialSender(rest, ReplyBudget())
    png = b"\x89PNG fake bytes"
    mid = await s.send_group_image_bytes("G1", png, content="题卡")
    assert mid is not None
    up = rest.uploads[-1][1]
    assert up["file_type"] == 1
    assert up["srv_send_msg"] is False
    assert base64.b64decode(up["file_data"]) == png
    assert "url" not in up
    assert rest.sent[-1][1]["media"]["file_info"] == "FI"
    # second identical image reuses cached file_info (no new upload)
    await s.send_group_image_bytes("G1", png)
    assert len(rest.uploads) == 1


async def test_send_image_bytes_upload_failure_returns_none():
    class FailRest(FakeRest):
        async def upload_group_media(self, group_openid, payload):
            raise OfficialApiError(400, {"code": 850031, "message": "too large"})

    s = OfficialSender(FailRest(), ReplyBudget())
    assert await s.send_group_image_bytes("G1", b"x") is None


# ── real Chromium rendering (integration) ───────────────────────────────

@pytest.fixture
async def browser():
    b = RenderBrowser()
    yield b
    await b.close()


async def test_render_card_png_real(browser):
    nodes = [
        {"nickname": "算法助手", "content_html": text_to_html("题目摘要\n第二行")},
        {"nickname": "算法助手", "content_html": pre_block("3\n1 2 3")},
    ]
    png = await render_card_png(nodes, title="题目卡片", browser=browser)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 1000


async def test_send_forward_card_uses_image(browser):
    rest = FakeRest()
    s = OfficialSender(rest, ReplyBudget())
    nodes = [{"nickname": "助手", "content_html": text_to_html("hi")}]
    mid = await send_forward_card(s, "G1", nodes, title="帮助", browser=browser)
    assert mid is not None
    assert rest.uploads  # uploaded an image
    assert rest.sent[-1][1]["msg_type"] == 7


async def test_send_forward_card_text_fallback_on_render_error():
    class BrokenBrowser:
        async def screenshot_html(self, *a, **k):
            raise RuntimeError("no chromium")

    rest = FakeRest()
    s = OfficialSender(rest, ReplyBudget())
    nodes = [{"nickname": "助手", "content_text": "纯文本降级"}]
    mid = await send_forward_card(
        s, "G1", nodes, browser=BrokenBrowser(), text_fallback="降级文本"
    )
    assert mid is not None
    assert rest.uploads == []  # no upload
    assert rest.sent[-1][1]["content"] == "降级文本"
    assert rest.sent[-1][1]["msg_type"] == 0