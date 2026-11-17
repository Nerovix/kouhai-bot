"""Annotation bundle export/store tests.

The exporter is still JSON-file-based (``scoreboard.json`` + disk statements +
``problem_summaries.json``); the Q6 migration to the SQLite state layer is
deferred. The old auto-export-on-/submit test was dropped: the new official-API
submit handler no longer calls ``export_problem_annotation_bundle`` (backfill
via ``sync_annotation_bundles`` remains the only export entry point).
"""

import json
from pathlib import Path

import pytest

from kouhai_bot.annotations import exporter, store
from kouhai_bot.config import BotConfig
from kouhai_bot.db import set_database
from kouhai_bot.db.repos import posts

GID = 123456
UID = 42
PID = "542D"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    cfg = BotConfig(data_dir=str(tmp_path))
    monkeypatch.setattr("kouhai_bot.config._config", cfg)
    (tmp_path / "groups" / str(GID)).mkdir(parents=True)
    (tmp_path / "statements").mkdir()
    return cfg


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_statement(cfg, pid=PID, data=None):
    _write_json(
        Path(cfg.data_dir) / "statements" / f"{pid}.json",
        data if data is not None else {"name": "D. Superhero's Job", "description": "Test statement", "input": "A"},
    )


def _scoreboard_path(cfg, gid=GID):
    return Path(cfg.data_dir) / "groups" / str(gid) / "scoreboard.json"


def _solve_entry(uid=UID, nickname="Alice", pid=PID, order=1):
    return {
        "user_id": uid,
        "nickname": nickname,
        "date": "2026-05-14",
        "problem": pid,
        "order": order,
    }


def _record(content, result, *, minute=0, pid=PID, reason="", reply="", **extra):
    record = {
        "timestamp": f"2026-05-14T12:{minute:02d}:00+08:00",
        "content": content,
        "result": result,
        "reason": reason,
        "reply": reply,
        "problem": pid,
    }
    record.update(extra)
    return record


def _seed_scoreboard(cfg, records=None, solves=None):
    _write_json(_scoreboard_path(cfg), {
        "solves": solves if solves is not None else [_solve_entry()],
        "user_submissions": {str(UID): records if records is not None else [
            _record("accepted", "correct", minute=0, reason="ok"),
        ]},
    })


def test_collect_problem_annotation_bundle_includes_history(cfg):
    _write_statement(cfg)
    _write_json(_scoreboard_path(cfg), {
        "solves": [_solve_entry()],
        "user_submissions": {
            str(UID): [
                _record("first wrong", "incorrect", minute=0, reason="wrong", reply="try again"),
                _record("what does input mean", "clarify", minute=3, reply="input is A"),
                _record("second correct", "correct", minute=5, reason="good"),
            ],
            "99": [
                _record("other wrong", "incorrect", minute=4, reason="bad", reply="no"),
            ],
        },
    })

    bundle = exporter.collect_problem_annotation_bundle(GID, PID, source="test")

    assert bundle is not None
    assert bundle["problem_id"] == PID
    assert bundle["group_id"] == GID
    assert bundle["first_solver"]["user_id"] == UID
    assert bundle["statement"]["name"] == "D. Superhero's Job"
    # 2 submit rounds for Alice + 1 for user 99; clarify is context only.
    assert len(bundle["rounds"]) == 3
    alice_correct = next(
        item for item in bundle["rounds"]
        if item["model_verdict"] == "correct" and item["user_id"] == UID
    )
    assert alice_correct["round_id"] == f"{PID}:{UID}:2"
    assert len(alice_correct["history_before"]) == 2
    assert alice_correct["history_before"][1]["result"] == "clarify"
    assert alice_correct["human_label"]["expected_verdict"] is None
    assert bundle["first_correct_timestamp"] == alice_correct["timestamp"]


def test_history_item_whitelist_and_model_tag(cfg):
    record = _record("tagged", "correct", reason="ok", model_tag="『M』", request_id="r1")
    item = exporter._history_item(record)
    assert item == {
        "timestamp": record["timestamp"],
        "result": "correct",
        "submission": "tagged",
        "reason": "ok",
        "reply": "",
        "problem": PID,
        "model_tag": "『M』",
    }
    assert "request_id" not in item


def test_collect_returns_none_without_solve_or_rounds(cfg):
    _write_statement(cfg)
    # No solves at all.
    _write_json(_scoreboard_path(cfg), {"solves": [], "user_submissions": {}})
    assert exporter.collect_problem_annotation_bundle(GID, PID) is None

    # Solve exists but only clarify history → no judge rounds to label.
    _write_json(_scoreboard_path(cfg), {
        "solves": [_solve_entry()],
        "user_submissions": {str(UID): [_record("question", "clarify", reply="answer")]},
    })
    assert exporter.collect_problem_annotation_bundle(GID, PID) is None

    # Rounds exist but for a different problem.
    _write_json(_scoreboard_path(cfg), {
        "solves": [_solve_entry()],
        "user_submissions": {str(UID): [_record("other", "correct", pid="999X")]},
    })
    assert exporter.collect_problem_annotation_bundle(GID, PID) is None


def test_sync_annotation_bundles_is_idempotent(cfg):
    _write_statement(cfg)
    _seed_scoreboard(cfg)

    first = exporter.sync_annotation_bundles(group_id=GID)
    second = exporter.sync_annotation_bundles(group_id=GID)
    summaries = store.list_bundle_summaries(status="pending", group_id=GID)

    assert len(first) == 1
    assert second == []
    assert len(summaries) == 1
    assert summaries[0]["problem_id"] == PID
    assert summaries[0]["progress"] == "0/1"
    assert summaries[0]["source"] == "backfill"


def test_export_skips_when_bundle_already_labeled(cfg):
    _write_statement(cfg)
    _seed_scoreboard(cfg)
    bundle = exporter.collect_problem_annotation_bundle(GID, PID)
    store.save_bundle(bundle, store.LABELED)

    assert exporter.export_problem_annotation_bundle(GID, PID) is None
    loaded, status = store.load_bundle(GID, PID)
    assert status == "labeled"
    assert loaded["problem_id"] == PID


def test_collect_bundle_prefers_saved_group_summary(cfg):
    from kouhai_bot.handlers.shared import save_problem_summary

    _write_statement(cfg)
    _seed_scoreboard(cfg)
    save_problem_summary(GID, PID, "这是群里发题时生成的中文题意。")
    bundle = exporter.collect_problem_annotation_bundle(GID, PID, source="test")

    assert bundle is not None
    assert bundle["statement"]["summary_zh"] == "这是群里发题时生成的中文题意。"


def test_store_save_moves_between_statuses_and_tracks_progress(cfg):
    _write_statement(cfg)
    _seed_scoreboard(cfg)
    bundle = exporter.collect_problem_annotation_bundle(GID, PID)

    pending_path = store.save_bundle(bundle, store.PENDING)
    assert pending_path.exists()
    assert store.bundle_exists(GID, PID)

    bundle["rounds"][0]["human_label"]["expected_verdict"] = "correct"
    labeled_path = store.save_bundle(bundle, store.LABELED)
    assert labeled_path.exists()
    assert not pending_path.exists(), "saving as labeled must remove the pending copy"

    summaries = store.list_bundle_summaries(group_id=GID)
    assert len(summaries) == 1
    assert summaries[0]["status"] == "labeled"
    assert summaries[0]["progress"] == "1/1"

    with pytest.raises(ValueError, match="invalid annotation status"):
        store.save_bundle(bundle, "bogus")


async def test_db_solves_are_not_visible_to_json_exporter_yet(cfg, db):
    """Q6 deferred: the exporter reads scoreboard.json, not group_solves.

    Documents the current boundary so the deferred migration is explicit:
    a solve recorded only in SQLite does not produce an annotation bundle.
    """
    set_database(db)
    _write_statement(cfg)
    await posts.record_solve(db, str(GID), str(UID), PID, "Alice")

    assert exporter.collect_problem_annotation_bundle(GID, PID) is None
    assert exporter.sync_annotation_bundles(group_id=GID) == []