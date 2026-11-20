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
    build_scoreboard_html,
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


def test_build_card_html_avatar_img_vs_letter_circle():
    with_avatar = build_forward_card_html(
        [{"nickname": "Alice", "content_html": "hi", "avatar_data": "data:image/jpeg;base64,AAAA"}],
    )
    assert '<img class="avatar" src="data:image/jpeg;base64,AAAA"' in with_avatar
    without = build_forward_card_html([{"nickname": "Alice", "content_html": "hi"}])
    assert '<img class="avatar"' not in without
    assert 'class="avatar"' in without  # letter-color circle fallback


async def test_resolve_avatars_embeds_data_uri(monkeypatch):
    from kouhai_bot.render import forward_card as fc

    class FakeCache:
        async def data_uri(self, url):
            return "data:image/png;base64,Zm9v" if url == "https://ok/a.png" else None

    monkeypatch.setattr(fc, "get_avatar_cache", lambda: FakeCache())
    nodes = [
        {"nickname": "A", "content_html": "x", "avatar_url": "https://ok/a.png"},
        {"nickname": "B", "content_html": "y", "avatar_url": "https://fail/b.png"},
        {"nickname": "C", "content_html": "z"},
    ]
    resolved = await fc._resolve_avatars(nodes)
    assert resolved[0]["avatar_data"] == "data:image/png;base64,Zm9v"
    assert "avatar_data" not in resolved[1]  # fetch failure → letter circle
    assert "avatar_data" not in resolved[2]  # no url
    assert "avatar_data" not in nodes[0]     # caller dicts not mutated


async def test_resolve_avatars_injects_bot_avatar(monkeypatch):
    from kouhai_bot.render import forward_card as fc

    class FakeCache:
        async def data_uri(self, url):
            return "data:image/png;base64,Zm9v" if url.startswith("https://ok/") else None

    monkeypatch.setattr(fc, "get_avatar_cache", lambda: FakeCache())
    monkeypatch.setattr(fc, "_default_bot_avatar_data_uri", lambda: "data:image/jpeg;base64,DEFAULT")
    nodes = [
        {"nickname": "KouhaiBot", "content_html": "x"},                       # bot, no url → configured url
        {"nickname": "Alice", "content_html": "y"},                          # human, no url → circle
        {"nickname": "KouhaiBot", "content_html": "z", "avatar_url": "https://ok/own.png"},  # own wins
    ]
    resolved = await fc._resolve_avatars(
        nodes, bot_name="KouhaiBot", bot_avatar_url="https://ok/bot.png"
    )
    assert resolved[0]["avatar_data"] == "data:image/png;base64,Zm9v"  # configured url beats default
    assert "avatar_data" not in resolved[1]
    assert resolved[2]["avatar_data"] == "data:image/png;base64,Zm9v"
    assert "avatar_data" not in nodes[0]


async def test_resolve_avatars_bot_uses_bundled_default(monkeypatch):
    from kouhai_bot.render import forward_card as fc

    class FakeCache:
        async def data_uri(self, url):
            return None

    monkeypatch.setattr(fc, "get_avatar_cache", lambda: FakeCache())
    monkeypatch.setattr(fc, "_default_bot_avatar_data_uri", lambda: "data:image/jpeg;base64,DEFAULT")
    nodes = [{"nickname": "KouhaiBot", "content_html": "x"}]
    resolved = await fc._resolve_avatars(nodes, bot_name="KouhaiBot", bot_avatar_url="")
    assert resolved[0]["avatar_data"] == "data:image/jpeg;base64,DEFAULT"


async def test_resolve_avatars_bot_letter_circle_when_no_asset(monkeypatch):
    from kouhai_bot.render import forward_card as fc

    class FakeCache:
        async def data_uri(self, url):
            return None

    monkeypatch.setattr(fc, "get_avatar_cache", lambda: FakeCache())
    monkeypatch.setattr(fc, "_default_bot_avatar_data_uri", lambda: None)
    nodes = [{"nickname": "KouhaiBot", "content_html": "x"}]
    resolved = await fc._resolve_avatars(nodes, bot_name="KouhaiBot", bot_avatar_url="")
    assert "avatar_data" not in resolved[0]  # no asset → letter circle


def test_default_bot_avatar_asset_loads():
    from kouhai_bot.render import forward_card as fc

    uri = fc._default_bot_avatar_data_uri()
    assert uri is not None and uri.startswith("data:image/jpeg;base64,")


def test_build_card_html_escapes_nickname():
    html = build_forward_card_html(
        [{"nickname": "<script>", "content_html": "x"}]
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_build_scoreboard_html_medals_formula_and_rows():
    ranked = [
        {"rank": 1, "nickname": "Bob", "solved": 3, "score": 12.0},
        {"rank": 2, "nickname": "Alice", "solved": 2, "score": 6.5},
        {"rank": 3, "nickname": "Carol", "solved": 1, "score": 2.0},
        {"rank": 4, "nickname": "Dave", "solved": 1, "score": 1.0},
    ]
    html = build_scoreboard_html(ranked)
    assert "🥇" in html and "🥈" in html and "🥉" in html
    assert "#4" in html
    # formula uses a real superscript, not a raw 2^ caret
    assert "2<sup>(rating−2000)/300</sup>" in html
    assert "2^" not in html
    assert "共 4 人" in html
    # /help-style light list: bold nickname + plain "— N 题 / X 分", no poster styling
    assert "<strong>Bob</strong> — 3 题 / 12 分" in html
    assert "<strong>Alice</strong> — 2 题 / 6.5 分" in html
    assert "<h3>" in html and "<ul>" in html and "<li>" in html
    assert "sb-score" not in html and "sb-gold" not in html


def test_build_scoreboard_html_tied_ranks_share_medal():
    ranked = [
        {"rank": 1, "nickname": "A", "solved": 1, "score": 2.0},
        {"rank": 1, "nickname": "B", "solved": 1, "score": 2.0},
    ]
    html = build_scoreboard_html(ranked)
    assert html.count("🥇") == 2
    assert "#1" not in html


def test_build_scoreboard_html_escapes_nickname():
    html = build_scoreboard_html(
        [{"rank": 1, "nickname": "<b>x</b>", "solved": 1, "score": 1.0}]
    )
    assert "<b>x</b>" not in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html


def test_build_scoreboard_html_migration_note():
    ranked = [{"rank": 1, "nickname": "A", "solved": 1, "score": 2.0}]
    # absent by default
    assert "数据正在迁移中" not in build_scoreboard_html(ranked)
    # present, muted, and placed under the formula before the rows
    html = build_scoreboard_html(ranked, migration_note="数据正在迁移中，排名可能不完整。")
    assert '<div class="muted">数据正在迁移中，排名可能不完整。</div>' in html
    assert html.index("数据正在迁移中") < html.index("<ul>")
    assert html.index("2<sup>") < html.index("数据正在迁移中")
    # note content is escaped
    inj = build_scoreboard_html(ranked, migration_note="<script>x</script>")
    assert "<script>x</script>" not in inj
    assert "&lt;script&gt;" in inj


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
    assert "content" not in rest.sent[-1][1]  # no caption repeating the header title


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