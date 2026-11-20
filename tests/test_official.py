"""Official transport tests: events, budget, tokens, REST, sender, gateway."""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from kouhai_bot.official import (
    AccessTokenManager,
    OfficialGateway,
    OfficialRestClient,
    OfficialSender,
    ReplyBudget,
    normalize_event,
)
from kouhai_bot.official import budget as budget_mod
from kouhai_bot.official import sender as sender_mod
from kouhai_bot.official.rest import OfficialApiError


# ── event normalization ─────────────────────────────────────────────────

def test_normalize_group_message():
    ev = normalize_event("GROUP_MESSAGE_CREATE", {
        "id": "M1", "content": "/help", "group_openid": "G1",
        "author": {"member_openid": "U1", "union_openid": ""},
    })
    assert ev["type"] == "message"
    assert ev["message_type"] == "group"
    assert ev["group_openid"] == "G1"
    assert ev["user_openid"] == "U1"
    assert ev["message_id"] == "M1"
    assert ev["at_bot"] is False


def test_normalize_group_message_identity_and_context_fields():
    # 2026-11-16 official update: author carries username/member_nickname/avatar;
    # events carry structured mentions + message_scene + msg_elements.
    ev = normalize_event("GROUP_MESSAGE_CREATE", {
        "id": "M1", "content": "/review hi", "group_openid": "G1",
        "author": {
            "member_openid": "U1", "username": "全局昵称",
            "member_nickname": "群名片", "avatar": "https://thirdqq.qlogo.cn/x",
        },
        "mentions": [{"member_openid": "U2", "username": "别人", "scope": "single"}],
        "message_scene": {"source": "default", "ext": ["ref_msg_idx=TMP_x"]},
        "msg_elements": [{"message_type": 103, "content": "引用内容"}],
    })
    assert ev["nickname"] == "群名片"          # group card preferred over username
    assert ev["avatar"] == "https://thirdqq.qlogo.cn/x"
    assert ev["mentions"][0]["member_openid"] == "U2"
    assert ev["message_scene"]["ext"] == ["ref_msg_idx=TMP_x"]
    assert ev["msg_elements"][0]["message_type"] == 103


def test_normalize_group_nickname_falls_back_to_username():
    ev = normalize_event("GROUP_MESSAGE_CREATE", {
        "id": "M1", "content": "hi", "group_openid": "G1",
        "author": {"member_openid": "U1", "username": "全局昵称"},
    })
    assert ev["nickname"] == "全局昵称"        # no group card → QQ nickname
    assert ev["avatar"] == ""
    assert ev["mentions"] == []
    empty = normalize_event("GROUP_MESSAGE_CREATE", {
        "id": "M2", "content": "hi", "group_openid": "G1",
        "author": {"member_openid": "U1"},
    })
    assert empty["nickname"] == ""             # absent → Ctx falls back to openid[:8]


def test_normalize_at_message_and_c2c():
    at = normalize_event("GROUP_AT_MESSAGE_CREATE", {
        "id": "M2", "content": " hi", "group_openid": "G1",
        "author": {"member_openid": "U1"},
    })
    assert at["at_bot"] is True
    c2c = normalize_event("C2C_MESSAGE_CREATE", {
        "id": "M3", "content": "hi", "author": {"user_openid": "U9"},
    })
    assert c2c["message_type"] == "private"
    assert c2c["user_openid"] == "U9"


def test_normalize_member_and_interaction():
    add = normalize_event("GROUP_MEMBER_ADD", {"group_openid": "G1", "member_openid": "U1"})
    assert add["type"] == "member_add"
    rem = normalize_event("GROUP_MEMBER_REMOVE", {"group_openid": "G1", "member_openid": "U1"})
    assert rem["type"] == "member_remove"
    inter = normalize_event("INTERACTION_CREATE", {
        "id": "I1", "group_openid": "G1", "group_member_openid": "U1", "scene": "group",
        "data": {"type": 11, "resolved": {"button_data": "/np", "button_id": "next"}},
    })
    assert inter["type"] == "interaction"
    assert inter["interaction_type"] == 11
    assert inter["button_data"] == "/np"
    assert inter["button_id"] == "next"
    assert inter["user_openid"] == "U1"
    assert normalize_event("UNKNOWN", {}) is None


# ── reply budget ────────────────────────────────────────────────────────

def test_passive_seq_exhausts_after_five():
    b = ReplyBudget()
    assert [b.next_passive_seq("M1") for _ in range(5)] == [1, 2, 3, 4, 5]
    assert b.next_passive_seq("M1") is None
    assert b.next_passive_seq("") is None


def test_passive_window_expiry(monkeypatch):
    b = ReplyBudget()
    t = [1000.0]
    monkeypatch.setattr(budget_mod.time, "monotonic", lambda: t[0])
    for _ in range(5):
        b.next_passive_seq("M1")
    assert b.next_passive_seq("M1") is None
    t[0] += budget_mod.PASSIVE_WINDOW_SEC + 1
    assert b.next_passive_seq("M1") == 1  # window reset


async def test_active_qpm_limit():
    b = ReplyBudget()
    for _ in range(budget_mod.ACTIVE_QPM_MAX):
        assert await b.acquire_active("G1") is True
    assert await b.acquire_active("G1") is False


async def test_active_bot_wide_qpm_limit():
    # The bot-wide cap binds even when no single group hits its per-group cap.
    b = ReplyBudget()
    orig = budget_mod.ACTIVE_BOT_QPM_MAX
    budget_mod.ACTIVE_BOT_QPM_MAX = 3
    try:
        for i in range(3):
            assert await b.acquire_active(f"G{i}") is True  # 1 per group, 3 total
        assert await b.acquire_active("G3") is False  # bot-wide exhausted
        assert await b.acquire_active("G0") is False  # even a group with spare quota
    finally:
        budget_mod.ACTIVE_BOT_QPM_MAX = orig


async def test_active_daily_limit_memory():
    b = ReplyBudget()
    monkeypatch_daily = budget_mod.ACTIVE_DAILY_MAX
    # lower the cap via a fresh budget with patched constant
    orig = budget_mod.ACTIVE_DAILY_MAX
    budget_mod.ACTIVE_DAILY_MAX = 3
    try:
        for _ in range(3):
            assert await b.acquire_active("G1") is True
        assert await b.acquire_active("G1") is False
    finally:
        budget_mod.ACTIVE_DAILY_MAX = orig


async def test_active_daily_persisted(db):
    from kouhai_bot.db.repos import runtime as rt
    b = ReplyBudget(db=db)
    await b.acquire_active("G1")
    await b.acquire_active("G1")
    from kouhai_bot.db.timeutil import beijing_date
    assert await rt.get_active_count(db, "G1", beijing_date()) == 2


# ── tokens ──────────────────────────────────────────────────────────────

class FakeResp:
    def __init__(self, data, status=200):
        self._data = data
        self.status = status

    async def json(self, content_type=None):
        return self._data

    async def text(self):
        return json.dumps(self._data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self):
        self.posts = []
        self.requests = []
        self.token_response = {"access_token": "T1", "expires_in": 7200}
        self.token_status = 200
        self.next_response = {"id": "M1"}
        self.next_status = 200

    def post(self, url, json=None):
        self.posts.append((url, json))
        return FakeResp(self.token_response, self.token_status)

    def request(self, method, url, json=None, headers=None):
        self.requests.append((method, url, json, headers))
        return FakeResp(self.next_response, self.next_status)


async def test_token_cache_and_refresh():
    sess = FakeSession()
    mgr = AccessTokenManager("APP", "SECRET", session=sess)
    assert await mgr.get_token() == "T1"
    assert await mgr.get_token() == "T1"
    assert len(sess.posts) == 1  # cached
    assert sess.posts[0][1] == {"appId": "APP", "clientSecret": "SECRET"}
    mgr.invalidate()
    sess.token_response = {"access_token": "T2", "expires_in": 7200}
    assert await mgr.get_token() == "T2"
    assert len(sess.posts) == 2


async def test_token_missing_raises():
    sess = FakeSession()
    sess.token_response = {"code": 1, "message": "bad"}
    mgr = AccessTokenManager("APP", "SECRET", session=sess)
    with pytest.raises(RuntimeError):
        await mgr.get_token()


# ── REST ────────────────────────────────────────────────────────────────

async def _rest_with_fake():
    sess = FakeSession()
    tokens = AccessTokenManager("APP", "SECRET", session=sess)
    rest = OfficialRestClient(tokens, api_base="https://api.test", session=sess)
    return rest, sess, tokens


async def test_rest_send_builds_url_and_auth():
    rest, sess, _ = await _rest_with_fake()
    sess.next_response = {"id": "M1", "timestamp": 123}
    resp = await rest.send_group_message("G1", {"content": "hi", "msg_type": 0})
    assert resp["id"] == "M1"
    method, url, payload, headers = sess.requests[-1]
    assert method == "POST"
    assert url == "https://api.test/v2/groups/G1/messages"
    assert headers["Authorization"] == "QQBot T1"
    assert payload == {"content": "hi", "msg_type": 0}


async def test_rest_error_invalidates_token_on_401():
    rest, sess, tokens = await _rest_with_fake()
    await tokens.get_token()
    sess.next_status = 401
    sess.next_response = {"code": 11244, "message": "expired"}
    with pytest.raises(OfficialApiError):
        await rest.send_group_message("G1", {"content": "hi", "msg_type": 0})
    assert tokens._token == ""  # invalidated


async def test_rest_get_gateway():
    rest, sess, _ = await _rest_with_fake()
    sess.next_response = {"url": "wss://gw.test"}
    assert await rest.get_gateway() == "wss://gw.test"


async def test_rest_respond_interaction_puts_ack():
    rest, sess, _ = await _rest_with_fake()
    sess.next_response = {}
    assert await rest.respond_interaction("I1") is True
    method, url, payload, headers = sess.requests[-1]
    assert method == "PUT"
    assert url == "https://api.test/interactions/I1"
    assert payload == {"code": 0}
    assert await rest.respond_interaction("") is False  # no id → no request


# ── sender ──────────────────────────────────────────────────────────────

class FakeRest:
    def __init__(self):
        self.sent = []
        self.uploads = []
        self.recalled = []
        self.acks = []
        self.fail_passive = False

    async def send_group_message(self, group_openid, payload):
        self.sent.append((group_openid, payload))
        if self.fail_passive and payload.get("msg_id"):
            raise OfficialApiError(400, {"code": 1, "message": "passive expired"})
        n = len(self.sent)
        return {"id": f"M{n}", "ext_info": {"ref_idx": f"REFIDX_{n}"}}

    async def upload_group_media(self, group_openid, payload):
        self.uploads.append((group_openid, payload))
        return {"file_info": "FILEINFO", "file_uuid": "uuid", "ttl": 3600}

    async def recall_group_message(self, group_openid, message_id):
        self.recalled.append((group_openid, message_id))
        return True

    async def respond_interaction(self, interaction_id, *, code=0):
        self.acks.append((interaction_id, code))
        return True


async def test_sender_passive_first_then_active():
    rest = FakeRest()
    s = OfficialSender(rest, ReplyBudget())
    for i in range(5):
        await s.send_group_text("G1", f"t{i}", msg_id="M1")
    # first five are passive with msg_seq 1..5
    for i in range(5):
        assert rest.sent[i][1]["msg_id"] == "M1"
        assert rest.sent[i][1]["msg_seq"] == i + 1
    # sixth falls back to active (no msg_id)
    await s.send_group_text("G1", "t5", msg_id="M1")
    assert "msg_id" not in rest.sent[5][1]


async def test_sender_passive_failure_falls_back_active():
    rest = FakeRest()
    rest.fail_passive = True
    s = OfficialSender(rest, ReplyBudget())
    mid = await s.send_group_text("G1", "hi", msg_id="M1")
    assert mid is not None
    assert rest.sent[-1][1].get("msg_id") is None  # active


async def test_sender_at_uses_markdown():
    rest = FakeRest()
    s = OfficialSender(rest, ReplyBudget())
    await s.send_group_at("G1", "U1", "做对了～")
    payload = rest.sent[-1][1]
    assert payload["msg_type"] == sender_mod.MSG_MARKDOWN
    assert '<qqbot-at-user id="U1"/>' in payload["markdown"]["content"]
    assert "做对了～" in payload["markdown"]["content"]


async def test_sender_image_upload_then_media():
    rest = FakeRest()
    s = OfficialSender(rest, ReplyBudget())
    mid = await s.send_group_image_url("G1", "https://img.test/a.png", content="题卡")
    assert mid is not None
    assert rest.uploads[-1][1]["url"] == "https://img.test/a.png"
    payload = rest.sent[-1][1]
    assert payload["msg_type"] == sender_mod.MSG_MEDIA
    assert payload["media"]["file_info"] == "FILEINFO"
    assert payload["content"] == "题卡"


async def test_sender_react_is_tiny_text():
    rest = FakeRest()
    s = OfficialSender(rest, ReplyBudget())
    await s.react("G1", "👀", msg_id="M1")
    assert rest.sent[-1][1] == {"content": "👀", "msg_type": 0, "msg_id": "M1", "msg_seq": 1}


async def test_sender_react_mentions_user():
    rest = FakeRest()
    s = OfficialSender(rest, ReplyBudget())
    await s.react("G1", "👀", msg_id="M1", user_openid="U1")
    payload = rest.sent[-1][1]
    assert payload["msg_type"] == sender_mod.MSG_MARKDOWN
    assert payload["markdown"]["content"] == '<qqbot-at-user id="U1"/> 👀'
    assert payload["msg_id"] == "M1"


async def test_sender_active_quota_exhausted_returns_none():
    rest = FakeRest()
    b = ReplyBudget()
    orig = budget_mod.ACTIVE_DAILY_MAX
    budget_mod.ACTIVE_DAILY_MAX = 0
    try:
        s = OfficialSender(rest, b)
        assert await s.send_group_text("G1", "hi") is None
        assert rest.sent == []
    finally:
        budget_mod.ACTIVE_DAILY_MAX = orig


async def test_sender_captures_outbound_ref_idx():
    rest = FakeRest()
    s = OfficialSender(rest, ReplyBudget())
    mid = await s.send_group_text("G1", "题卡")
    assert mid == "M1"
    assert s.ref_idx_for(mid) == "REFIDX_1"
    assert s.ref_idx_for("unknown") is None
    assert s.ref_idx_for(None) is None


async def test_sender_ack_interaction():
    rest = FakeRest()
    s = OfficialSender(rest, ReplyBudget())
    assert await s.ack_interaction("I1") is True
    assert rest.acks == [("I1", 0)]


# ── scene.ext parsing + quote normalization (实测 shapes) ────────────────

def test_parse_scene_ext_list_and_dict():
    from kouhai_bot.official.events import parse_scene_ext
    ext = ["ref_msg_idx=REFIDX_a=b", "msg_idx=REFIDX_m", "auth_token=tok"]
    parsed = parse_scene_ext(ext)
    assert parsed["ref_msg_idx"] == "REFIDX_a=b"  # split on first '=' only
    assert parsed["msg_idx"] == "REFIDX_m"
    assert parsed["auth_token"] == "tok"
    assert parse_scene_ext({"msg_idx": "X"}) == {"msg_idx": "X"}
    assert parse_scene_ext(None) == {}
    assert parse_scene_ext(["noequals", 5]) == {}


def test_normalize_quote_message_fields():
    ev = normalize_event("GROUP_MESSAGE_CREATE", {
        "id": "M1", "content": " 引用bot的消息", "group_openid": "G1",
        "message_type": 103,
        "author": {"member_openid": "U1", "avatar": "https://thirdqq.qlogo.cn/x"},
        "msg_elements": [
            {"message_type": 103, "content": "被引正文", "msg_idx": "REFIDX_quoted"},
        ],
        "message_scene": {"ext": [
            "ref_msg_idx=REFIDX_quoted", "msg_idx=REFIDX_self", "auth_token=t",
        ]},
    })
    assert ev["ref_msg_idx"] == "REFIDX_quoted"
    assert ev["msg_idx"] == "REFIDX_self"
    assert ev["quoted_text"] == "被引正文"


def test_normalize_ordinary_message_has_no_ref():
    ev = normalize_event("GROUP_MESSAGE_CREATE", {
        "id": "M1", "content": "普通你好", "group_openid": "G1",
        "author": {"member_openid": "U1"},
        "message_scene": {"ext": ["msg_idx=REFIDX_self", "auth_token=t"]},
    })
    assert ev["ref_msg_idx"] == ""
    assert ev["msg_idx"] == "REFIDX_self"
    assert ev["quoted_text"] == ""


# ── gateway (local WS server) ───────────────────────────────────────────

async def test_gateway_identify_and_dispatch():
    captured = {}
    received = []
    stop = asyncio.Event()

    async def handler(ws):
        await ws.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 60000}}))
        ident = json.loads(await ws.recv())
        captured["identify"] = ident
        await ws.send(json.dumps({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "S1"}}))
        await ws.send(json.dumps({
            "op": 0, "s": 2, "t": "GROUP_MESSAGE_CREATE",
            "d": {"id": "M1", "content": "/help", "group_openid": "G1",
                  "author": {"member_openid": "U1"}},
        }))
        # close after dispatch so the client recv loop ends
        await ws.close()

    async def on_event(ev):
        received.append(ev)
        stop.set()

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        sess = FakeSession()
        tokens = AccessTokenManager("APP", "SECRET", session=sess)

        class GwRest:
            async def get_gateway(self):
                return f"ws://127.0.0.1:{port}"

        gw = OfficialGateway(GwRest(), tokens, on_event=on_event, intents=1 << 25)
        await asyncio.wait_for(gw.run(stop), timeout=5)
    finally:
        server.close()
        await server.wait_closed()

    assert captured["identify"]["op"] == 2
    assert captured["identify"]["d"]["token"] == "QQBot T1"
    assert captured["identify"]["d"]["intents"] == 1 << 25
    assert captured["identify"]["d"]["shard"] == [0, 1]
    assert len(received) == 1
    assert received[0]["user_openid"] == "U1"
    assert received[0]["content"] == "/help"