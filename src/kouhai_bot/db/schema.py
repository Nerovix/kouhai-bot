"""SQLite schema for the official-API multi-group bot.

Fresh start (no legacy JSON migration). Identity is native openid:
``user_openid`` (== group ``member_openid``, cross-group stable) and
``group_openid`` are TEXT primary keys.

"Group = view" model: submissions are global user×problem records tagged
with the group they were posted in. A group's scoreboard is the aggregation
of its view (native + /sync-materialized submissions). Global solve facts
are derivable from any correct submission; per-group first-blood lives in
``group_solves``.
"""

from __future__ import annotations

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Global user identity (openid native).
CREATE TABLE IF NOT EXISTS users (
    user_openid TEXT PRIMARY KEY,
    nickname    TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Each served group is an independent view with its own config.
CREATE TABLE IF NOT EXISTS groups (
    group_openid  TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    min_rating    INTEGER,
    max_rating    INTEGER,
    enabled_jobs  TEXT NOT NULL DEFAULT '[]',
    job_schedules TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- Global problem catalog: statement + verified summary, fingerprint-bound.
CREATE TABLE IF NOT EXISTS problems (
    pid             TEXT PRIMARY KEY,
    contest_id      INTEGER,
    idx             TEXT NOT NULL DEFAULT '',
    name            TEXT NOT NULL DEFAULT '',
    rating          INTEGER,
    tags            TEXT NOT NULL DEFAULT '[]',
    statement_sha256 TEXT NOT NULL DEFAULT '',
    statement_json  TEXT NOT NULL DEFAULT '{}',
    summary_zh      TEXT NOT NULL DEFAULT '',
    summary_status  TEXT NOT NULL DEFAULT '',
    summary_sha256  TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Global official editorial per pid (source + translation, fingerprint-bound).
CREATE TABLE IF NOT EXISTS editorials (
    pid               TEXT PRIMARY KEY,
    tutorial_url      TEXT NOT NULL DEFAULT '',
    tutorial_title    TEXT NOT NULL DEFAULT '',
    sections_json     TEXT NOT NULL DEFAULT '[]',
    source_sha256     TEXT NOT NULL DEFAULT '',
    translation_zh    TEXT NOT NULL DEFAULT '',
    translation_status TEXT NOT NULL DEFAULT '',
    statement_sha256  TEXT NOT NULL DEFAULT '',
    no_editorial_reason TEXT NOT NULL DEFAULT '',
    search_complete   INTEGER NOT NULL DEFAULT 0,
    updated_at        TEXT NOT NULL
);

-- Per-group posted problems (the view anchor). One current per group.
CREATE TABLE IF NOT EXISTS group_posts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    group_openid  TEXT NOT NULL,
    pid           TEXT NOT NULL,
    posted_at     INTEGER NOT NULL,
    post_date     TEXT NOT NULL,
    card_json     TEXT NOT NULL DEFAULT '{}',
    is_current    INTEGER NOT NULL DEFAULT 0,
    solved_at     INTEGER,
    first_solver_openid TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    UNIQUE (group_openid, pid)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_group_posts_current
    ON group_posts (group_openid) WHERE is_current = 1;
CREATE INDEX IF NOT EXISTS idx_group_posts_group_pid
    ON group_posts (group_openid, pid);

-- Global submission/clarify/review records (request_id upsert key).
CREATE TABLE IF NOT EXISTS submissions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id    TEXT UNIQUE,
    group_openid  TEXT NOT NULL,
    user_openid   TEXT NOT NULL,
    pid           TEXT NOT NULL,
    type          TEXT NOT NULL,
    content       TEXT NOT NULL DEFAULT '',
    result        TEXT NOT NULL DEFAULT 'pending',
    reason        TEXT NOT NULL DEFAULT '',
    reply         TEXT NOT NULL DEFAULT '',
    model_tag     TEXT NOT NULL DEFAULT '',
    synced        INTEGER NOT NULL DEFAULT 0,
    source_group_openid TEXT NOT NULL DEFAULT '',
    seq           INTEGER NOT NULL DEFAULT 0,
    timestamp     TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_submissions_group_user_pid
    ON submissions (group_openid, user_openid, pid);
CREATE INDEX IF NOT EXISTS idx_submissions_user_pid
    ON submissions (user_openid, pid);
CREATE INDEX IF NOT EXISTS idx_submissions_group_pid_result
    ON submissions (group_openid, pid, result);

-- Per-group first-blood ledger (replaces scoreboard.solves).
CREATE TABLE IF NOT EXISTS group_solves (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    group_openid  TEXT NOT NULL,
    user_openid   TEXT NOT NULL,
    pid           TEXT NOT NULL,
    nickname      TEXT NOT NULL DEFAULT '',
    solve_date    TEXT NOT NULL,
    order_num     INTEGER NOT NULL,
    source        TEXT NOT NULL DEFAULT 'native',
    created_at    TEXT NOT NULL,
    UNIQUE (group_openid, user_openid, pid)
);
CREATE INDEX IF NOT EXISTS idx_group_solves_group_order
    ON group_solves (group_openid, order_num);
CREATE INDEX IF NOT EXISTS idx_group_solves_group_user
    ON group_solves (group_openid, user_openid);

-- /feedback reports (append-only; corrupt store must raise, not reset).
CREATE TABLE IF NOT EXISTS bad_reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    scope         TEXT NOT NULL DEFAULT 'group',
    group_openid  TEXT NOT NULL,
    user_openid   TEXT NOT NULL,
    reported_at   TEXT NOT NULL,
    note          TEXT NOT NULL DEFAULT '',
    cmd_message_id TEXT NOT NULL DEFAULT '',
    target_pid    TEXT NOT NULL DEFAULT '',
    target_record_json TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bad_reports_group
    ON bad_reports (group_openid, id);

-- Per-user last delivered interaction (CACHE: corrupt reads as empty).
CREATE TABLE IF NOT EXISTS last_interaction (
    scope         TEXT NOT NULL DEFAULT 'group',
    group_openid  TEXT NOT NULL,
    user_openid   TEXT NOT NULL,
    record_json   TEXT NOT NULL DEFAULT '{}',
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (scope, group_openid, user_openid)
);

-- Active-message daily quota per group (passive window is in-memory).
CREATE TABLE IF NOT EXISTS reply_budget (
    group_openid  TEXT NOT NULL,
    day           TEXT NOT NULL,
    active_count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_openid, day)
);

-- Per-group durable READY next-problem slot.
CREATE TABLE IF NOT EXISTS prefetch_slot (
    group_openid      TEXT PRIMARY KEY,
    slot_id           TEXT NOT NULL DEFAULT '',
    problem_json      TEXT NOT NULL DEFAULT '{}',
    statement_sha256  TEXT NOT NULL DEFAULT '',
    editorial_sha256  TEXT NOT NULL DEFAULT '',
    claimed           INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- Per-group picker exclusion marks (candidate used before posting).
CREATE TABLE IF NOT EXISTS group_used (
    group_openid TEXT NOT NULL,
    pid          TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (group_openid, pid)
);

-- Outbound message ref_idx → problem map (quoted-card targeting). The official
-- send response returns ext_info.ref_idx (REFIDX_…); a user quoting that card
-- delivers the same value as inbound message_scene.ext ref_msg_idx. ref_idx is
-- a per-group index, so the key is (group_openid, ref_idx) — a cross-group
-- collision must not steal another group's mapping.
CREATE TABLE IF NOT EXISTS message_refs (
    group_openid TEXT NOT NULL,
    ref_idx      TEXT NOT NULL,
    pid          TEXT NOT NULL,
    message_id   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    PRIMARY KEY (group_openid, ref_idx)
);
CREATE INDEX IF NOT EXISTS idx_message_refs_group_pid
    ON message_refs (group_openid, pid);

-- Legacy hot-migration identity map (old service group only). One row per
-- (group, openid) once that member's old group data has been migrated; the
-- row is the idempotency marker so a member is migrated exactly once.
CREATE TABLE IF NOT EXISTS legacy_migration_map (
    group_openid TEXT NOT NULL,
    user_openid  TEXT NOT NULL,
    old_user_id  TEXT NOT NULL,
    nickname     TEXT NOT NULL DEFAULT '',
    migrated_at  TEXT NOT NULL,
    PRIMARY KEY (group_openid, user_openid)
);

-- Per-group order_num floor for hot-migration. Migrated solves keep their old
-- global order (1..N); native solves must number strictly above the OLD max so
-- they never collide with a legacy order that has not been migrated yet (a
-- member who never speaks never migrates, so the window never closes on its
-- own). record_solve uses max(current_max, floor)+1. Absent for non-migration
-- groups, where it degrades to a plain monotonic max+1 counter.
CREATE TABLE IF NOT EXISTS group_order_floor (
    group_openid TEXT PRIMARY KEY,
    floor        INTEGER NOT NULL
);
"""