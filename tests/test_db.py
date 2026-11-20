"""SQLite state-layer invariants (fresh-start, openid native, group=view)."""

from __future__ import annotations

import json

import pytest

from kouhai_bot.db import Database
from kouhai_bot.db.repos import feedback, posts, problems, runtime, submissions, users


async def test_schema_version(db):
    ver = await db.fetch_val("SELECT value FROM meta WHERE key='schema_version'")
    assert ver == "1"


async def test_user_and_group_upsert(db):
    await users.upsert_user(db, "U1", nickname="Alice")
    await users.upsert_user(db, "U1")  # no nickname → keeps existing
    u = await users.get_user(db, "U1")
    assert u["nickname"] == "Alice"
    await users.set_group_rating(db, "G1", 2200, 2600)
    assert await users.get_group_rating(db, "G1") == (2200, 2600)
    await users.set_group_jobs(db, "G1", ["contest_check"], {"contest_check": "12:01"})
    g = await users.get_group(db, "G1")
    assert g["enabled_jobs"] == ["contest_check"]
    assert g["job_schedules"]["contest_check"] == "12:01"


async def test_summary_fingerprint_gate(db):
    await problems.upsert_problem(db, "542D", statement_sha256="sha-A", name="P")
    await problems.set_problem_summary(db, "542D", "摘要", "verified", "sha-A")
    assert await problems.get_verified_summary(db, "542D", "sha-A") == "摘要"
    assert await problems.get_verified_summary(db, "542D", "sha-B") is None
    await problems.set_problem_summary(db, "542D", "x", "incomplete", "sha-A")
    assert await problems.get_verified_summary(db, "542D", "sha-A") is None


async def test_editorial_gate(db):
    await problems.upsert_problem(db, "P1", statement_sha256="s")
    await problems.upsert_editorial_source(
        db, "P1", tutorial_url="u", tutorial_title="t",
        sections_json="[]", source_sha256="e", statement_sha256="s",
    )
    assert await problems.get_verified_editorial(db, "P1", "s") is None
    await problems.set_editorial_translation(db, "P1", "中文", "verified")
    ed = await problems.get_verified_editorial(db, "P1", "s")
    assert ed["translation_zh"] == "中文"
    assert await problems.get_verified_editorial(db, "P1", "other") is None
    assert await problems.get_editorial_state(db, "P1", "s") == "verified"
    await problems.set_no_editorial(db, "P2", "s2", "exhaustive")
    assert await problems.get_editorial_state(db, "P2", "s2") == "no_editorial"
    assert await problems.get_editorial_state(db, "P2", "wrong") == "incomplete"


async def test_post_demotes_previous_current(db):
    await posts.post_problem(db, "G1", "A", posted_at=100)
    await posts.post_problem(db, "G1", "B", posted_at=200)
    cur = await posts.get_current_post(db, "G1")
    assert cur["pid"] == "B"
    assert cur["posted_at"] == 200
    old = await posts.get_post(db, "G1", "A")
    assert old["is_current"] == 0


async def test_record_solve_first_blood_and_dedup(db):
    await problems.upsert_problem(db, "A", rating=2300)
    await posts.post_problem(db, "G1", "A")
    r1 = await posts.record_solve(db, "G1", "U1", "A", "Alice")
    assert r1["is_first_blood"] is True
    assert r1["order_num"] == 1
    assert r1["solved_count"] == 1
    # same user same problem → duplicate, no new row
    r2 = await posts.record_solve(db, "G1", "U1", "A", "Alice")
    assert r2["duplicate"] is True
    assert r2["is_first_blood"] is False
    # second user same problem → not first blood, order 2
    r3 = await posts.record_solve(db, "G1", "U2", "A", "Bob")
    assert r3["is_first_blood"] is False
    assert r3["order_num"] == 2
    post = await posts.get_post(db, "G1", "A")
    assert post["first_solver_openid"] == "U1"
    assert post["solved_at"] is not None


async def test_scoreboard_weighted_and_tied_ranks(db):
    await problems.upsert_problem(db, "A", rating=2300)  # 2^1 = 2
    await problems.upsert_problem(db, "B", rating=2000)  # 2^0 = 1
    await problems.upsert_problem(db, "C", rating=None)  # no contribution
    await posts.record_solve(db, "G1", "U1", "A", "Alice")
    await posts.record_solve(db, "G1", "U1", "B", "Alice")  # U1 score 3
    await posts.record_solve(db, "G1", "U2", "A", "Bob")    # U2 score 2
    await posts.record_solve(db, "G1", "U3", "C", "Cara")   # U3 score 0
    await users.upsert_user(db, "U1", nickname="AliceLive")
    board = await posts.group_scoreboard(db, "G1")
    assert [e["user_openid"] for e in board] == ["U1", "U2", "U3"]
    assert board[0]["score"] == pytest.approx(3.0)
    assert board[0]["nickname"] == "AliceLive"  # live nickname preferred
    assert board[0]["rank"] == 1
    assert board[1]["rank"] == 2
    assert board[2]["score"] == 0.0
    assert board[2]["nickname"] == "Cara"


async def test_scoreboard_tied_score_shares_rank(db):
    await problems.upsert_problem(db, "A", rating=2000)
    await posts.record_solve(db, "G1", "U1", "A", "a")
    await posts.record_solve(db, "G1", "U2", "A", "b")
    board = await posts.group_scoreboard(db, "G1")
    assert board[0]["rank"] == 1
    assert board[1]["rank"] == 1  # equal score → same rank


async def test_submission_upsert_by_request_id(db):
    rec = {
        "request_id": "req-1", "group_openid": "G1", "user_openid": "U1",
        "pid": "A", "type": "submit", "content": "sol", "result": "pending",
        "seq": 1,
    }
    await submissions.save_submission(db, rec)
    rec2 = dict(rec, result="correct", reply="对的", reason="")
    await submissions.save_submission(db, rec2)
    rows = await submissions.get_user_problem_submissions(db, "G1", "U1", "A")
    assert len(rows) == 1  # upsert, not append
    assert rows[0]["result"] == "correct"
    assert rows[0]["reply"] == "对的"


async def test_submission_append_without_request_id(db):
    base = {"group_openid": "G1", "user_openid": "U1", "pid": "A", "type": "clarify"}
    await submissions.save_submission(db, dict(base, content="q1"))
    await submissions.save_submission(db, dict(base, content="q2"))
    rows = await submissions.get_user_problem_submissions(db, "G1", "U1", "A")
    assert len(rows) == 2


async def test_submission_exclude_and_clear(db):
    await submissions.save_submission(db, {
        "request_id": "r1", "group_openid": "G1", "user_openid": "U1",
        "pid": "A", "type": "submit", "content": "x", "result": "pending",
    })
    await submissions.save_submission(db, {
        "request_id": "r2", "group_openid": "G1", "user_openid": "U1",
        "pid": "A", "type": "submit", "content": "y", "result": "incorrect",
    })
    rows = await submissions.get_user_problem_submissions(
        db, "G1", "U1", "A", exclude_request_id="r1"
    )
    assert [r["request_id"] for r in rows] == ["r2"]
    removed = await submissions.clear_user_problem(db, "G1", "U1", "A")
    assert removed == 2
    assert await submissions.get_user_problem_submissions(db, "G1", "U1", "A") == []


async def test_cross_group_pull_and_materialize(db):
    await submissions.save_submission(db, {
        "request_id": "ga", "group_openid": "GA", "user_openid": "U1",
        "pid": "A", "type": "submit", "content": "sol", "result": "correct",
        "timestamp": "2026-01-01T00:00:00+08:00",
    })
    pulled = await submissions.get_cross_group_submissions(db, "U1", "A", "GB")
    assert len(pulled) == 1
    assert pulled[0]["group_openid"] == "GA"
    await submissions.insert_synced_submission(db, "GB", pulled[0])
    view = await submissions.get_user_problem_submissions(db, "GB", "U1", "A")
    assert len(view) == 1
    assert view[0]["synced"] is True
    assert view[0]["source_group_openid"] == "GA"
    assert view[0]["request_id"] is None


async def test_bad_reports_append_and_corrupt_raises(db):
    rid = await feedback.append_bad_report(
        db, group_openid="G1", user_openid="U1", note="错了",
        cmd_message_id="m1", target_pid="A", target_record={"reply": "r"},
    )
    assert rid == 1
    reports = await feedback.load_bad_reports(db, "G1")
    assert len(reports) == 1
    assert reports[0]["target"]["record"]["reply"] == "r"
    # corrupt the stored snapshot → load must raise, not reset
    await db.execute(
        "UPDATE bad_reports SET target_record_json='{bad' WHERE id=?", (rid,)
    )
    with pytest.raises(json.JSONDecodeError):
        await feedback.load_bad_reports(db, "G1")


async def test_last_interaction_cache_corrupt_is_empty(db):
    await feedback.set_last_interaction(db, "G1", "U1", {"reply": "hi"})
    assert (await feedback.get_last_interaction(db, "G1", "U1"))["reply"] == "hi"
    await db.execute(
        "UPDATE last_interaction SET record_json='{bad' WHERE user_openid='U1'"
    )
    assert await feedback.get_last_interaction(db, "G1", "U1") is None


async def test_reply_budget_incr(db):
    assert await runtime.get_active_count(db, "G1", "2026-11-17") == 0
    assert await runtime.incr_active(db, "G1", "2026-11-17") == 1
    assert await runtime.incr_active(db, "G1", "2026-11-17") == 2
    assert await runtime.get_active_count(db, "G1", "2026-11-17") == 2


async def test_prefetch_slot_claim_release(db):
    await runtime.set_prefetch_slot(
        db, "G1", slot_id="s1", problem_json='{"pid":"A"}', statement_sha256="sha",
    )
    claimed = await runtime.claim_prefetch_slot(db, "G1")
    assert claimed["problem"]["pid"] == "A"
    assert await runtime.claim_prefetch_slot(db, "G1") is None  # already claimed
    await runtime.release_prefetch_slot(db, "G1")
    assert await runtime.get_prefetch_slot(db, "G1") is None


async def test_excluded_pids_union(db):
    await posts.mark_used(db, "G1", "USED")
    await posts.post_problem(db, "G1", "POSTED")
    await posts.record_solve(db, "G1", "U1", "SOLVED", "n")
    excluded = await posts.get_excluded_pids(db, "G1")
    assert excluded == {"USED", "POSTED", "SOLVED"}


async def test_message_ref_maps_to_pid_group_scoped(db):
    await posts.record_message_ref(db, "G1", "REFIDX_a", "542D", "M1")
    assert await posts.pid_for_ref(db, "G1", "REFIDX_a") == "542D"
    # group-scoped: another group cannot resolve G1's ref
    assert await posts.pid_for_ref(db, "G2", "REFIDX_a") is None
    assert await posts.pid_for_ref(db, "G1", "REFIDX_missing") is None
    assert await posts.pid_for_ref(db, "G1", "") is None
    # upsert: re-posting the same pid under a new ref keeps both resolvable
    await posts.record_message_ref(db, "G1", "REFIDX_b", "542D", "M2")
    assert await posts.pid_for_ref(db, "G1", "REFIDX_b") == "542D"
    # empty ref/pid are no-ops
    await posts.record_message_ref(db, "G1", "", "X")
    await posts.record_message_ref(db, "G1", "REFIDX_c", "")
    assert await posts.pid_for_ref(db, "G1", "REFIDX_c") is None