"""Hot-migration patch suite (legacy service group only).

Runs against a synthetic staged source DB built in tmp_path — no real QQ
numbers, nicknames, or production data. Covers nickname matching (latest /
historical / ambiguous / no-match), per-user atomic + idempotent migration,
SQL-injection-safe nickname handling, insert-if-absent catalog loading, the
group gate, and the dispatch hook firing on non-command messages.
"""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from kouhai_bot import legacy_migration as lm
from kouhai_bot.config import BotConfig
from kouhai_bot.db import set_database
from kouhai_bot.db.repos import posts, problems
from kouhai_bot.handlers import process_event
from kouhai_bot.handlers.runtime import Runtime, set_runtime
from kouhai_bot.official import OfficialSender, ReplyBudget
from kouhai_bot.problem_content import statement_fingerprint

MIG = "MIGGROUP"
OTHER = "OTHERGROUP"

SOURCE_SCHEMA = """
CREATE TABLE legacy_users(old_user_id TEXT PRIMARY KEY, latest_nickname TEXT, nicknames_json TEXT);
CREATE TABLE legacy_solves(old_user_id TEXT, pid TEXT, solve_date TEXT, order_num INTEGER, nickname TEXT);
CREATE TABLE legacy_submissions(old_user_id TEXT, pid TEXT, type TEXT, content TEXT, result TEXT,
    reason TEXT, reply TEXT, model_tag TEXT, timestamp TEXT, seq INTEGER);
CREATE TABLE legacy_problems(pid TEXT PRIMARY KEY, contest_id INTEGER, idx TEXT, name TEXT,
    rating INTEGER, tags_json TEXT, statement_sha256 TEXT, statement_json TEXT,
    summary_zh TEXT, summary_status TEXT, summary_sha256 TEXT);
CREATE TABLE legacy_editorials(pid TEXT PRIMARY KEY, tutorial_url TEXT, tutorial_title TEXT,
    sections_json TEXT, source_sha256 TEXT, translation_zh TEXT, translation_status TEXT,
    statement_sha256 TEXT);
CREATE TABLE nickname_index(nickname TEXT, old_user_id TEXT, is_latest INTEGER);
"""


def _build_source(path):
    import json
    con = sqlite3.connect(path)
    con.executescript(SOURCE_SCHEMA)
    st1 = {"title": "Problem One", "description": "Given n, compute.", "samples": []}
    st2 = {"title": "Problem Two", "description": "Given m, compute.", "samples": []}
    sha1 = statement_fingerprint(st1)
    sha2 = statement_fingerprint(st2)
    con.executemany("INSERT INTO legacy_users VALUES (?,?,?)", [
        ("1001", "张三", json.dumps(["张三", "张三丰"], ensure_ascii=False)),
        ("1002", "李四", json.dumps(["李四", "李四旧"], ensure_ascii=False)),
        ("1003", "共享昵称", json.dumps(["共享昵称"], ensure_ascii=False)),
        ("1004", "共享昵称", json.dumps(["共享昵称"], ensure_ascii=False)),
        ("1005", "x'); DROP TABLE group_solves;--", json.dumps(["x'); DROP TABLE group_solves;--"], ensure_ascii=False)),
    ])
    con.executemany("INSERT INTO legacy_solves VALUES (?,?,?,?,?)", [
        ("1001", "P1", "2026-05-01", 1, "张三"),
        ("1002", "P1", "2026-05-02", 2, "李四"),
        ("1001", "P2", "2026-05-03", 3, "张三"),
        ("1005", "P1", "2026-05-04", 4, "x'); DROP TABLE group_solves;--"),
    ])
    con.executemany("INSERT INTO legacy_submissions VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("1001", "P1", "submit", "贪心", "correct", "", "做对了", "🐳", "2026-05-01T10:00:00+08:00", 0),
        ("1001", "P1", "clarify", "n 范围?", "clarify", "", "n≤1e5", "✦", "2026-05-01T11:00:00+08:00", 1),
        ("1002", "P1", "submit", "暴力", "incorrect", "超时", "再想想", "🐳", "2026-05-02T10:00:00+08:00", 0),
    ])
    con.executemany("INSERT INTO legacy_problems VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
        ("P1", 100, "A", "Problem One", 2300, "[]", sha1, json.dumps(st1, ensure_ascii=False),
         "摘要一", "verified", sha1),
        ("P2", 100, "B", "Problem Two", 2600, "[]", sha2, json.dumps(st2, ensure_ascii=False),
         "", "", ""),
    ])
    con.execute("INSERT INTO legacy_editorials VALUES (?,?,?,?,?,?,?,?)", (
        "P1", "https://example.com/blog", "Editorial", "[]", "srcsha", "题解一", "verified", sha1,
    ))
    con.executemany("INSERT INTO nickname_index VALUES (?,?,?)", [
        ("张三", "1001", 1), ("张三丰", "1001", 0),
        ("李四", "1002", 1), ("李四旧", "1002", 0),
        ("共享昵称", "1003", 1), ("共享昵称", "1004", 1),
        ("x'); DROP TABLE group_solves;--", "1005", 1),
    ])
    con.commit()
    con.close()
    return sha1, sha2


class FakeRest:
    def __init__(self):
        self.sent = []

    async def send_group_message(self, group_openid, payload):
        self.sent.append((group_openid, payload))
        return {"id": f"M{len(self.sent)}"}


@pytest.fixture
async def mig(db, tmp_path, monkeypatch):
    source = tmp_path / "legacy_source.db"
    sha1, sha2 = _build_source(str(source))
    cfg = BotConfig(data_dir=str(tmp_path))
    cfg.legacy_migration_enabled = True
    cfg.legacy_migration_group_openid = MIG
    cfg.legacy_migration_source_db = str(source)
    monkeypatch.setattr("kouhai_bot.config._config", cfg)
    set_database(db)
    sender = OfficialSender(FakeRest(), ReplyBudget(db), bot_name="算法助手")
    set_runtime(Runtime(db=db, sender=sender, browser=None))
    lm.reset_for_tests()
    yield SimpleNamespace(db=db, cfg=cfg, sha1=sha1, sha2=sha2, source=source)
    await lm.aclose_source()
    lm.reset_for_tests()


async def _count(db, sql, params=()):
    return await db.fetch_val(sql, params)


# ── matching + per-user migration ───────────────────────────────────────


async def test_latest_nickname_migrates_solves_and_submissions(mig):
    await lm.maybe_migrate(MIG, "openid_zhang", "张三")
    db = mig.db
    assert await _count(db, "SELECT COUNT(*) FROM group_solves WHERE user_openid='openid_zhang'") == 2
    assert await _count(db, "SELECT COUNT(*) FROM submissions WHERE user_openid='openid_zhang'") == 2
    row = await db.fetch_one(
        "SELECT old_user_id, nickname FROM legacy_migration_map WHERE user_openid='openid_zhang'"
    )
    assert row["old_user_id"] == "1001"
    # old global order preserved + tagged migrated
    solves = await db.fetch_all(
        "SELECT pid, order_num, source FROM group_solves WHERE user_openid='openid_zhang' ORDER BY order_num"
    )
    assert [(s["pid"], s["order_num"], s["source"]) for s in solves] == [
        ("P1", 1, "migrated"), ("P2", 3, "migrated"),
    ]
    # submissions appended with NULL request_id, not synced
    subs = await db.fetch_all(
        "SELECT request_id, synced, type FROM submissions WHERE user_openid='openid_zhang' ORDER BY seq"
    )
    assert all(s["request_id"] is None and s["synced"] == 0 for s in subs)
    assert [s["type"] for s in subs] == ["submit", "clarify"]


async def test_migration_is_idempotent(mig):
    await lm.maybe_migrate(MIG, "openid_zhang", "张三")
    await lm.maybe_migrate(MIG, "openid_zhang", "张三")
    await lm.maybe_migrate(MIG, "openid_zhang", "张三丰")  # historical, same old user
    db = mig.db
    assert await _count(db, "SELECT COUNT(*) FROM group_solves WHERE user_openid='openid_zhang'") == 2
    assert await _count(db, "SELECT COUNT(*) FROM submissions WHERE user_openid='openid_zhang'") == 2
    assert await _count(db, "SELECT COUNT(*) FROM legacy_migration_map") == 1


async def test_historical_nickname_match(mig):
    await lm.maybe_migrate(MIG, "openid_li", "李四旧")
    db = mig.db
    row = await db.fetch_one(
        "SELECT old_user_id FROM legacy_migration_map WHERE user_openid='openid_li'"
    )
    assert row["old_user_id"] == "1002"
    assert await _count(db, "SELECT COUNT(*) FROM group_solves WHERE user_openid='openid_li'") == 1


async def test_ambiguous_nickname_skipped(mig):
    await lm.maybe_migrate(MIG, "openid_amb", "共享昵称")
    db = mig.db
    assert await _count(db, "SELECT COUNT(*) FROM legacy_migration_map WHERE user_openid='openid_amb'") == 0
    assert await _count(db, "SELECT COUNT(*) FROM group_solves WHERE user_openid='openid_amb'") == 0


async def test_no_match_nickname_skipped(mig):
    await lm.maybe_migrate(MIG, "openid_new", "一个完全不同的昵称")
    db = mig.db
    assert await _count(db, "SELECT COUNT(*) FROM legacy_migration_map WHERE user_openid='openid_new'") == 0


async def test_empty_nickname_skipped(mig):
    await lm.maybe_migrate(MIG, "openid_empty", "")
    assert await _count(mig.db, "SELECT COUNT(*) FROM legacy_migration_map") == 0


async def test_injection_nickname_is_inert(mig):
    await lm.maybe_migrate(MIG, "openid_joke", "x'); DROP TABLE group_solves;--")
    db = mig.db
    # table still exists and the joke user's single solve migrated
    assert await _count(db, "SELECT COUNT(*) FROM group_solves WHERE user_openid='openid_joke'") == 1
    row = await db.fetch_one(
        "SELECT old_user_id FROM legacy_migration_map WHERE user_openid='openid_joke'"
    )
    assert row["old_user_id"] == "1005"


# ── catalog ─────────────────────────────────────────────────────────────


async def test_catalog_loaded_and_fingerprint_bound(mig):
    await lm.maybe_migrate(MIG, "openid_zhang", "张三")
    db = mig.db
    assert await _count(db, "SELECT COUNT(*) FROM problems") == 2
    assert await _count(db, "SELECT COUNT(*) FROM editorials") == 1
    # verified summary + editorial bind to the stored statement fingerprint
    assert await problems.get_verified_summary(db, "P1", mig.sha1) == "摘要一"
    ed = await problems.get_verified_editorial(db, "P1", mig.sha1)
    assert ed is not None and ed["translation_zh"] == "题解一"
    # rating available for scoring
    assert (await problems.get_problem(db, "P1"))["rating"] == 2300


async def test_catalog_does_not_clobber_live_problem(mig):
    db = mig.db
    # a live-prepared problem P1 with a DIFFERENT (fresh) statement fingerprint
    await problems.upsert_problem(
        db, "P1", name="Live One", rating=2900,
        statement_sha256="live_sha", statement_json='{"name":"Live One"}',
    )
    await lm.maybe_migrate(MIG, "openid_zhang", "张三")
    p1 = await problems.get_problem(db, "P1")
    assert p1["statement_sha256"] == "live_sha"  # untouched
    assert p1["rating"] == 2900
    # P2 (absent live) still inserted from catalog
    assert (await problems.get_problem(db, "P2"))["rating"] == 2600


async def test_scoreboard_reflects_migrated_solves(mig):
    await lm.maybe_migrate(MIG, "openid_zhang", "张三")
    await lm.maybe_migrate(MIG, "openid_li", "李四")
    ranked = await posts.group_scoreboard(mig.db, MIG)
    top = ranked[0]
    assert top["nickname"] == "张三"  # live nickname captured on migration
    assert top["solved"] == 2
    # P1(2300)=2 + P2(2600)=4 → 6
    assert round(top["score"], 6) == 6.0


# ── gating ──────────────────────────────────────────────────────────────


async def test_other_group_is_noop(mig):
    await lm.maybe_migrate(OTHER, "openid_zhang", "张三")
    db = mig.db
    assert await _count(db, "SELECT COUNT(*) FROM group_solves") == 0
    assert await _count(db, "SELECT COUNT(*) FROM legacy_migration_map") == 0
    assert await _count(db, "SELECT COUNT(*) FROM problems") == 0


async def test_disabled_is_noop(mig):
    mig.cfg.legacy_migration_enabled = False
    await lm.maybe_migrate(MIG, "openid_zhang", "张三")
    assert await _count(mig.db, "SELECT COUNT(*) FROM legacy_migration_map") == 0


def test_is_migration_group_gate(mig):
    assert lm.is_migration_group(MIG) is True
    assert lm.is_migration_group(OTHER) is False
    assert lm.is_migration_group("") is False
    mig.cfg.legacy_migration_enabled = False
    assert lm.is_migration_group(MIG) is False


# ── dispatch hook ───────────────────────────────────────────────────────


async def test_hook_fires_on_non_command_message(mig, monkeypatch):
    captured = []
    monkeypatch.setattr(
        lm, "schedule_migration",
        lambda g, u, n: captured.append((g, u, n)),
    )
    event = {
        "type": "message", "message_type": "group",
        "group_openid": MIG, "user_openid": "openid_zhang",
        "message_id": "ROBOT1.0_noncmd", "content": "随便说一句话",
        "author": {"member_nickname": "张三"},
    }
    await process_event(event, spawn_handlers=False)
    assert captured == [(MIG, "openid_zhang", "张三")]


async def test_schedule_migration_dedups_and_completes(mig):
    lm.schedule_migration(MIG, "openid_zhang", "张三")
    lm.schedule_migration(MIG, "openid_zhang", "张三")  # deduped while in-flight
    for _ in range(200):
        if "openid_zhang" not in lm._inflight:
            break
        await asyncio.sleep(0.01)
    db = mig.db
    assert await _count(db, "SELECT COUNT(*) FROM legacy_migration_map WHERE user_openid='openid_zhang'") == 1
    assert await _count(db, "SELECT COUNT(*) FROM group_solves WHERE user_openid='openid_zhang'") == 2


# ── order_num floor: native solves never collide with unmigrated orders ──


async def test_native_solve_numbers_above_legacy_floor(mig):
    await lm.maybe_migrate(MIG, "openid_zhang", "张三")  # old orders 1,3; source max=4
    res = await posts.record_solve(mig.db, MIG, "openid_native", "P3", "新人")
    assert res["order_num"] == 5  # max(cur_max=3, floor=4) + 1
    rows = await mig.db.fetch_all(
        "SELECT order_num FROM group_solves WHERE group_openid=?", (MIG,)
    )
    nums = [r["order_num"] for r in rows]
    assert len(nums) == len(set(nums))  # no duplicate order_num


async def test_floor_applies_even_if_nobody_migrates(mig):
    # Startup loads the floor without migrating anyone (every member silent).
    await lm.ensure_order_floor(mig.db)
    assert await _count(mig.db, "SELECT COUNT(*) FROM group_solves") == 0
    res = await posts.record_solve(mig.db, MIG, "openid_native", "P3", "新人")
    assert res["order_num"] == 5  # floor=4 → first native is 5, not 1


async def test_late_migration_never_collides_with_native(mig):
    await lm.ensure_order_floor(mig.db)
    await posts.record_solve(mig.db, MIG, "openid_native", "P3", "新人")  # order 5
    # members speak later, bringing old orders 1..4 in any sequence
    await lm.maybe_migrate(MIG, "openid_zhang", "张三")  # 1,3
    await lm.maybe_migrate(MIG, "openid_li", "李四")  # 2
    await lm.maybe_migrate(MIG, "openid_joke", "x'); DROP TABLE group_solves;--")  # 4
    rows = await mig.db.fetch_all(
        "SELECT order_num FROM group_solves WHERE group_openid=?", (MIG,)
    )
    nums = [r["order_num"] for r in rows]
    assert len(nums) == len(set(nums))
    assert max(nums) == 5
    latest = await mig.db.fetch_one(
        "SELECT pid FROM group_solves WHERE group_openid=? ORDER BY order_num DESC LIMIT 1",
        (MIG,),
    )
    assert latest["pid"] == "P3"  # the native solve is correctly the latest


async def test_non_migration_group_plain_counter(mig):
    r1 = await posts.record_solve(mig.db, OTHER, "u1", "P1", "A")
    r2 = await posts.record_solve(mig.db, OTHER, "u2", "P2", "B")
    assert r1["order_num"] == 1 and r2["order_num"] == 2