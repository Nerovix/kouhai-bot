# Kouhai Bot — Development Guide

Instructions for AI coding assistants working on this codebase.

The bot runs on the **official QQ bot API** (`api.bot.qq.com`, unified 2026-08-10;
`api.sgroup.qq.com` still aliases it), not NapCat/OneBot. It is a **pure group bot**
serving **many groups** from one worker; each group is an independent view over a
shared SQLite database. Private judge, 打星/user_groups, curfew, and jev are removed.
The full migration rationale (D1–D16, open questions, 2026-11-18 API audit, self-review
fixes) lives in `docs/official-migration/DECISIONS.md` — read it before changing
architecture. This file is the operative quick reference.

## Operational Constraints

- All changes (code, config, docs) must be prepared in a **new git worktree** on a
  branch and merged by PR. Never edit the live `master` checkout directly. Use the
  `gh` CLI to create the PR; merge only after the owner confirms the PR is correct.
- The live service checkout must remain on `master`. Do not use `uv run restart` or
  otherwise change live service state unless explicitly asked. Running tests is allowed.
- Never commit private information into git-maintained content: QQ numbers, group chat
  numbers, group/user openids, AppID/AppSecret, and API keys must not appear in commits,
  branches, PRs, or any tracked file. Use placeholders in examples and docs.
- `AGENTS.md` is the single agent-instruction file for this repo. Keep it up to date
  whenever architecture or workflow rules change; do not create `CLAUDE.md` or other
  parallel instruction files.
- Judge and review tasks must use the configured highest reasoning capability. Do not
  lower `reasoning_effort`, disable thinking, or switch to a weaker effort to work
  around latency/cost.
- Do not set client-side output token caps, `max_tokens`, or `max_completion_tokens`
  for judge/review requests. For DashScope/百炼 thinking models only, set
  `thinking_budget=100000` so reasoning cannot consume the entire upstream output
  window before `content` starts.
- GLM-5.3 (Zhipu/火山方舟) exposes no thinking-budget parameter — its only depth control
  is `reasoning_effort` (`low`/`high`/`max`) and thinking cannot be disabled — so never
  send it budget-style fields. A long reasoning run can still return empty `content`;
  `glm53` therefore runs with `stream: true` (Ark streams support
  `stream_options.include_usage`) so that case is classified as
  `empty_content_after_reasoning`/`length` and fails over instead of being blindly
  retried. Both completion paths log `finish_reason`, `reasoning_chars`, and `usage`.

## Architecture

```
Official QQ Gateway ──outbound WS──> worker.py (OfficialGateway)
   ├── normalize_event → handlers.process_event(spawn_handlers=True)
   ├── handlers/cmd/*.py  auto-discovered by registry (Ctx)
   ├── per-group GroupServices (lazy on first group event; seeded from group_posts at boot):
   │     ├── NextProblemPrefetcher (one READY slot)
   │     └── editorial_prefetch_maintenance_loop
   ├── scheduler_loop (60s tick, iterates discovered groups)
   └── echo (auto-repeater, per group)
State: SQLite {data_dir}/kouhai.db (db/)   Render: Playwright long-image (render/)
Transport: official/ (tokens/rest/gateway/sender/budget/media)
```

## Core Design

- **Identity (D1)**: PKs are official openids (str). User = `user_openid` (group events
  carry `author.member_openid`, cross-group stable == C2C user_openid). Group =
  `group_openid`. Message id = str (`ROBOT1.0_…`). No int QQ/group numbers, no legacy
  migration, no /bind, no identity_map.
- **Group = view (D3/D9)**: one global `submissions` table (user×problem) +
  `group_posts`/`group_solves`. A group's scoreboard aggregates submissions joined to
  its posts. Solving P in group A records globally; group B learns only when the user
  `/sync`s in B.
- **Database (D3)**: SQLite + aiosqlite, `{data_dir}/kouhai.db`, WAL, FK on, explicit
  `BEGIN IMMEDIATE`. A single shared connection is guarded by a connection-wide
  `asyncio.Lock` — every `execute/fetch_*/transaction` is serialized (transactions on one
  connection cannot interleave). Repos in `db/repos/*.py`; `Database` in `db/database.py`
  (`get_database()`/`set_database()`). Tables: meta/users/groups/problems/editorials/
  group_posts/submissions/group_solves/bad_reports/last_interaction/reply_budget/
  prefetch_slot/group_used/message_refs.
- **Transport (D4)**: `official/OfficialSender` (send_group_text/markdown/at/media/
  image_bytes/image_url, react, recall). No OneBot segments. Content mapping:
  - incorrect verdict / clarify / congrats / short review → **markdown** (`msg_type=2`;
    @ = `<qqbot-at-user id="member_openid"/>`; @all = `<qqbot-at-everyone/>`, works in
    markdown AND text since 2026-10-26, admin + 1/group/day).
  - card-level (problem/help/scoreboard/long review/editorial/history) → **long image**
    (`render/`, Playwright screenshot of a mobile-QQ forward-card HTML replica) uploaded
    as base64 media (`msg_type=7`). Native 图文卡片 `msg_type=8` exists but is unused —
    long-image is the deliberate high-fidelity choice.
  - confirmations (👀 ack / 👌 clear+feedback / 😵 offtopic / 🧐 second-judge) → tiny
    emoji text. 🧐 carries **no text** ("二审" is dev jargon; original was reaction-only).
  - `ReplyBudget`: passive first (group 5min/5 replies per msg_id, independent of active
    quota), active fallback (1000/group/day persisted; two-layer rate limit — per-group
    20/qpm + bot-wide 60/qpm, 30 unverified). Graceful degradation on failure.
  - No official group reaction (表情表态 is channel-only) or poke → tiny emoji message.
    Recall = DELETE messages/{id} (own ≤2min; admin may recall member messages).
- **Media (Q2)**: `official/media.py` MediaCache keys content sha256 → file_info, expires
  ttl−30s (ttl=0 permanent), reused across sends. Upload POST /v2/groups/{gid}/files
  `file_data` base64 + `srv_send_msg:false` → file_info; send `msg_type=7 media:{file_info}`.
  ~100KB card PNGs use base64 direct upload.
- **Render (P2)**: `render/browser.py` RenderBrowser singleton (headless Chromium,
  `screenshot_html(html, width=720, device_scale_factor=2)`); `render/html.py` card HTML;
  `render/forward_card.py` `send_forward_card(...)` renders→uploads→sends image, degrades
  to text on failure and **never raises**. `render/avatars.py` AvatarCache pre-fetches
  `author.avatar` (public `thirdqq.qlogo.cn` JPEG, no auth) to a base64 data URI (parallel,
  LRU+TTL, 60s negative cache); failure → letter-color circle. Card builders: `handlers/cards.py`.
- **Multi-group (D7/D15)**: dispatch serves any group that sends an event (no
  `current_group` filter; membership is the natural gate). Worker keeps
  `group_openid → GroupServices` lazily, **and seeds known groups from `group_posts` at
  startup** so prefetch/editorial resume after restart. C2C/private events ignored.
- **Lifecycle (D5/D16)**: outbound WS has no listen port → instance identity is `app_id`.
  pidfile `{data_dir}/run/{app_id}.pid`; start/restart/stop/status select by pidfile +
  process cwd, and **verify `/proc/<pid>/cmdline` contains `kouhai_bot.worker`** before
  treating a PID as ours (a recycled PID must not be killed). Logs
  `{data_dir}/logs/{app_id}/YYYY-MM-DD.log`. prod/test = separate app_id (pidfile/DB).
- **Commands**: each `handlers/cmd/*.py` exports `register()` → `CommandDef`;
  `registry.discover_commands()` scans via `pkgutil.iter_modules`. Handlers take a single
  `Ctx`. Six aliases only: `/sbm`→submit, `/clrf`→clarify, `/rv`→review, `/np`→newproblem,
  `/pb`→problem, `/fb`→feedback. New commands default `aliases=[]` unless approved.
- **Ctx (D8)**: `handlers/context.py` `Ctx(group_openid, user_openid, message_id,
  raw_text, args, nickname, event)`; properties `db/sender/browser/name` read
  `handlers/runtime.py` `get_runtime()` (worker `set_runtime(...)` at boot; tests inject
  fake). `display_name` falls back to `openid[:8]`, never a generic placeholder. Since
  2026-11-16 group `author` carries `username`/`member_nickname`/`avatar`; `normalize_event`
  resolves nickname (group card preferred) and passes avatar + `mentions`/`message_scene`/
  `msg_elements` through.
- **Dispatch (D14)**: `process_event` handles normalized group messages only; strips
  leading command junk **and leading `<@bot>` mentions** (quoting a bot card auto-inserts
  @bot, arriving as GROUP_MESSAGE_CREATE), dedups inbound by message_id (global LRU,
  silent drop before echo/cooldown — gateway may redeliver), runs `echo.check_and_echo`
  for non-commands, enforces per-(group,user,command) cooldown (silent drop),
  canonicalizes alias→canonical, builds Ctx, spawns (`create_task`) or awaits. Any `/`
  command runs without requiring @bot. No reaction/notice/poke/friend-request branches.
- **Inbound quote (Q10/E3, 实测-verified)**: `mentions[]` = member_openid/username/
  member_nickname/bot/is_you/scope=all|single/member_role; inbound @ renders `<@openid>`
  (**no `!`**). `message_scene.ext` is a **list of `"key=value"` strings**
  (`msg_idx`/`ref_msg_idx`/`auth_token`); `parse_scene_ext` splits on first `=`. A quote is
  top-level `message_type=103`; `msg_elements[0].content` = quoted text, `.msg_idx` =
  quoted REFIDX; `ref_msg_idx` == that msg_idx. No history-fetch API — quoted text arrives
  inline. Send responses return `ext_info.ref_idx` (same REFIDX format); sender captures
  id→ref_idx (`ref_idx_for`). `/newproblem`//`/problem` persist card ref_idx→pid in
  `message_refs` (PK `(group_openid, ref_idx)` — ref_idx is per-group); `/review` matches
  inbound `ref_msg_idx` via `posts.pid_for_ref` (group-scoped) to target a quoted older
  **solved** card, else latest solved pid. Member/group-info REST stays whitelist-only
  (11253); nickname/avatar come from the event author, not those APIs.
- **Echo**: `echo.py` per-group buffer; ≥`trigger_count` identical non-command messages
  with no bot entry in the streak → repeat with probability `echo_probability` (leading
  @bot stripped from the repeated text). Needs GROUP_MESSAGE_CREATE (全量群消息).
- **Rating range**: global `problem.min_rating/max_rating`; per-group `/range` override
  (any member, smart parsing: exactly two ints → min/max, validate; else usage hint).
  Stored in `groups` (`users.get_group_rating`/`set_group_rating`).
- **Newproblem friction kept**: cooldown + plain `/newproblem` on unsolved rejected;
  exact `/newproblem --force` forces. Cooldown falls back to persisted `posted_at` after
  restart.
- **CF/editorial helpers**: `problems/fetcher.py:normalize_mathjax()` keeps only TeX
  source (`$$$...$$$`), discarding rendered/MathML copies (else judge sees duplicated
  numbers). `problems/cf_fetcher.py` tries cloudscraper then headless Chromium on
  403/timeout/challenge; `content_valid()` is the usability gate; editorial scraping
  delegates to it. Editorial extraction matches Div1/Div2 mirrors by title and
  distinguishes easy/hard versions; uncertain → `match=false` (judge correctness depends
  on it). Picker stderr is classified on the **full** text (pool-exhaustion marker prints
  last).
- **Removed**: private judge, 打星/user_groups/dynamic CD, curfew, jev/sbmjev, top5,
  NapCat, friend-request approval, poke/notice; `/setproblem`(`/sp`), `/testcd`, `/bad`.

## Configuration

`config.yaml` at repo root (or `KOUHAI_CONFIG=/path`); **never committed** (`.gitignore`).
Copy `config.example.yaml`.

### `official` (required)

| Key | Default | Description |
|-----|---------|-------------|
| `official.app_id` | `""` | Bot AppID (**required**; env `KOUHAI_APP_ID`) |
| `official.app_secret` | `""` | Bot AppSecret (**required**; env `KOUHAI_APP_SECRET`) |
| `official.bot_name` | `""` | Card attribution name (user nicknames come from events) |
| `official.sandbox` | `false` | Sandbox REST domain (largely deprecated) |
| `official.api_base` | `https://api.bot.qq.com` | REST base |
| `official.token_base` | `https://api.bot.qq.com` | access_token base (`/app/getAppAccessToken`) |
| `official.intents` | `(1<<25)\|(1<<26)\|(1<<24)` | group+C2C / interaction / group-member |

`validate_official()` fails fast if app_id/app_secret missing. `bot_qq`/`current_group`
now optional (default 0); legacy `napcat_*`/`user_groups`/`curfew`/`jev`/`qwen` parsed but
unused.

### `llm`

Two ordered fallback queues: `llm.smart_model` (judge/review) and `llm.general_model`
(everything else: clarify incl. images, summaries, editorial/sample-note translation, JSON
repair). `multimodal_*` task names only mark image parts; they route to `general_model`,
so those providers must accept images. Each provider retried `llm.max_retries` times
before the next. All use OpenAI-compatible `/chat/completions`; `base_url` includes any
version prefix.

| Key | Default | | Key | Default |
|-----|---------|-|-----|---------|
| `max_retries` | 2 | | `judge_timeout_sec` | 1200 |
| `retry_base_delay_sec` | 1.0 | | `clarify_timeout_sec` | 600 |
| `retry_max_delay_sec` | 8.0 | | `review_timeout_sec` | 600 |
| | | | `summary_timeout_sec` | 120 |

Per provider: `name`/`api_key`/`model` (required), `base_url`, `reasoning_effort`,
`stream` (SSE), `send_thinking` (default true), `temperature`, `extra_body`, `model_tag`.
DashScope/百炼 (by URL/name) and `stream:true` providers use HTTP+SSE — do not blindly send
`stream=true` to every provider (some gateways reject unknown fields). Transient failures
(timeouts, aiohttp errors, 408/409/429/5xx, malformed empty-choice/SSE) retry with backoff;
other 4xx fall back immediately. All exhausted → user reply suggests contacting an admin.

### `problem`

| Key | Default | Description |
|-----|---------|-------------|
| `problem.min_rating` | 2000 | Global min CF rating |
| `problem.max_rating` | 3000 | Global max CF rating |
| `problem.newproblem_cooldown` | 300 | `/newproblem` cooldown (sec) |
| `problem.submit_ac_backdoor` | `""` | Non-empty: matching `/submit` skips judge |

`data_dir` (default `~/.kouhai-bot`) holds kouhai.db, statement mirror, logs, pidfiles,
and the JSON READY slot.

### Model tags

Each provider's `model_tag` (e.g. `🐳`) is appended (no newline) to every LLM-generated
user message at display time; the LLM never sees tags. Records store raw text + a
`model_tag` field; prompt builders run stored reply/reason through `llm.strip_model_tags`
(trailing configured tags only, mid-text preserved). `llm.append_model_tag(text, tag)` is
the single display-time helper (rstrips, refuses to double an existing trailing tag).

## Data Directory

```
{data_dir}/kouhai.db                    # SQLite: source of truth for all state
{data_dir}/run/{app_id}.pid             # instance pidfile
{data_dir}/logs/{app_id}/YYYY-MM-DD.log # worker log (+8 tz)
{data_dir}/statements/<pid>.json        # CF statement MIRROR (crawler reuse; DB authoritative)
{data_dir}/groups/<gid>/next_problem.json # durable READY slot (JSON v2; prefetch_slot table unused)
```
Picker writes the disk mirror; the main process writes the DB catalog (D6/D13).

## Commands

All handlers `async def handle(ctx: Ctx)`; group-only, no @ needed to trigger.

| Command | File | Notes |
|---------|------|-------|
| `/submit` (`/sbm`) | submit.py | `smart_model` judge; coordinator; first-blood scoring; offtopic 😵 no record; 🧐 second judge only if verified editorial cached |
| `/clarify` (`/clrf`) | clarify.py | `general_model` JSON, anti-spoiler (no original identity); markdown @asker |
| `/review` (`/rv`) | review.py | Latest solved or quoted older solved card; admission snapshot; `smart_model`; structured `mentions[]` @others; English editorial model-only; short→markdown, long→image |
| `/newproblem` (`/np`) | newproblem.py | Claims READY slot; unsolved needs exact `--force`; cooldown; image card; commit after delivery |
| `/problem` (`/pb`) | stubs.py | Resend current problem card |
| `/tag` | stubs.py | Current problem CF tags |
| `/scoreboard` | stubs.py | Weighted `2^((rating-2000)/300)`; tied ranks; no top5 |
| `/status` | stubs.py | Active stateful work for this group/user |
| `/sync` | sync.py | Cross-group pull user×current-pid; exact dedup; import tagged synced; correct→score |
| `/clear` | clear.py | Clear caller's current-problem history; 👌; watermark; args→usage hint |
| `/feedback` (`/fb`) | feedback.py | Mark latest DELIVERED reply (last_interaction cache only); verbatim snapshot→bad_reports; 👌 |
| `/help` | help.py | Auto-generated from registry; image card |
| `/range` | range.py | Per-group rating range (smart parsing) |

### Stateful runtime (GroupCoordinator)

`/submit`/`/clarify`/`/review`/`/clear` run through a per-group `GroupCoordinator`
(`handlers/cmd/submit.py`); `/newproblem` uses its own per-group post lock. Rules:

- **Admission snapshots under lock**: each request gets a monotonic seq and snapshots its
  target pid **inside `coord.lock`** (submit/clarify/clear → then-current problem; review →
  then-latest solved, or quoted solved card). Never re-read the target during compute.
- **Parallel compute**: LLM work starts immediately, global concurrency limit 8; same-user
  requests don't wait for earlier LLM calls.
- **Pending archive**: admitted input saved immediately `result="pending"` + `request_id`;
  later same-user requests load pending + completed (excluding own request_id); final
  handling upserts that record. Superseded/timeout/failure stay context (empty reason/reply).
- **Offtopic no record**: judge `reaction="123"` → remove pending by request_id, tiny 😵.
- **Short critical sections**: DB read/modify/write under the coordinator lock
  (`run_group_state_update`); LLM calls never hold it. The first-blood scoring wait sleeps
  **outside** the lock.
- **First-blood serialization**: incorrect/offtopic/failure send immediately. Correct saves
  immediately; if earlier candidates unresolved → "做法判对了～" first; only the earliest
  correct after earlier ones settle updates scoreboard + cheer+@winner+reveal + schedules
  editorial. Later correct → `post_solve_correct`, save, no extra message.
- **Same-user replacement**: newer `/submit`/`/clear` for same user+problem before terminal
  handling drops the older (cancel compute, `status="stale"`); older pending →
  `superseded` (or removed by /clear). 👀 ack is not terminal.
- **New problem visibility**: commands see the old problem until the new card delivers;
  failed delivery keeps the old current problem.
- **Save-before-deliver**: clarify/review/submit save BEFORE delivering so `/feedback`
  never sees a sent-but-unsaved pending tail. Preserve this ordering.

Status helpers: `get_group_lock_status`, `has_active_problem_request`,
`get_newproblem_status`.

### `/submit` judge flow

Admission snapshot pid/solved under lock → AC backdoor short-circuit → else load history,
👀 ack, judge (`smart_model`). Already-solved at admission → "已经有人解出..." (no judge).
Offtopic → 😵 no record. Correct+first → `posts.record_solve` (weighted score/rank, congrats
cheer + @winner + reveal "本题来自 CF{pid} {name} {rating}✨") then
`schedule_post_solve_editorial_followup` (background, not awaited). Incorrect → markdown
@asker reason (empty reply → "reason。再想想？🤔"). **Second judge** only when a verified
editorial is cached (`get_verified_official_editorial` non-None); 🧐 emoji-only, no text.

`judge_submission()` sends `json.dumps({"problem","submission","history"})` — prompt expects
JSON, do not change to plain text. Statement joins sections with single `\n` (not `\n\n`).

### `/clarify`

`general_model`, `response_format: json_object`, `thinking:enabled` + `reasoning_effort`
sent unconditionally (unsupported fields ignored). Output `{"reply","reaction"}`;
`reaction="123"` → 😵 only. No solution hints or original identity (id/title/contest).
Grounding = current pid's verified summary (`problems.summary_zh` + fingerprint gate).

### `/review`

Targets latest solved at admission, or a quoted older solved card (`_resolve_review_pid`:
`ctx.event["ref_msg_idx"]` → `posts.pid_for_ref` group-scoped, used only if solved, else
latest solved). Loads caller history; @mentioned users' same-problem context from
structured `mentions[]` (ignore is_you/bot/self/dups; nothing persisted to them).
`smart_model` free text; verified editorial appended as **English** source
(`format_editorial_for_review`, truncated 12k) — model-only. Short→markdown @asker;
long (>400)→image + short @asker note.

### `/sync`

In group G for current P: pull user's P submissions from all other groups
(`get_cross_group_submissions`, **REPLIED_RESULTS only** — excludes pending/superseded),
sort by global timestamp, **exact dedup** (content+result identical → keep earliest; zero
LLM), import tagged `synced` (source group NOT shown). Imported correct + G unsolved →
`posts.record_solve(source="synced")`; G already solved → "只同步记录，不再加分".
Idempotent; empty source → abort no overwrite; active same-user/problem request → reject.
Imported history as image card; 👌 ack.

### `/feedback`

Scope-local, crosses problem switches, survives `/clear`: per-user `last_interaction`
cache (DB) is the ONLY source — never scans history. Markable = delivered records
(`REPLIED_RESULTS`: correct/incorrect/clarify/review/timeout/service_unavailable/
no_statement); pending/superseded unmarkable. Appends verbatim snapshot (reply/reason keep
tags) to `bad_reports`. 👌 ack; repeats append. Corrupt bad_reports → raise (never reset);
corrupt last_interaction → warn+None. Classification via `shared.record_kind`.

### `/newproblem`

Per-group post lock; busy → short reminder, not queued. Cooldown starts only after
successful delivery (falls back to persisted `posted_at`). Claims READY slot (or awaits
single-flight cold build); formats previous-problem reveal; assembles image card (summary
+ samples + optional translated notes + snake); commits `group_posts` + verified summary
only after delivery; releases slot, wakes refill. Plain on unsolved → reject (exact
`--force`). Forced refresh announces the skipped problem + sends its cached editorial card.

### Official tutorials

Offline scrape `tools/tutorial_tools.py crawl|agent|validate`. Runtime
`editorial_preparation.py` (side-effect-free exhaustive search+validate+translate) +
`tutorials.py` (DB `editorials` boundary; verified marker bound to BOTH editorial and
statement fingerprints; translation UPDATE guarded by `source_sha256`).
`get_verified_official_editorial(pid)` returns English source only when fingerprints match.
Worker maintenance covers current + READY pids, resumes interrupted work, retries
INCOMPLETE with exponential backoff (60s base, 6h cap); only a completed exhaustive search
writes `no_editorial`. Delivery on first AC via `editorial_followup` (background; warm
cache → immediate; else await in-flight prefetch then deliver if verified; none → silent
skip); worker shutdown cancels these tasks before closing handles. Card translation
(`shared.translate_editorial_to_zh`) omits code, avoids LaTeX, normalizes symbols.
`/review` uses English (+code) for grounding; the post-AC card uses Chinese without code.

### Scheduler

`scheduler/engine.py` multi-group loop (60s) iterates `await get_groups()`; per-group job
dedup by date+time; `load_group_config` defaults `contest_check`.
`scheduler/jobs.py:contest_check` → `shared.check_contests_for_group` fetches CF
contest.list, sleeps 2s, sends markdown `<qqbot-at-everyone/>` + lines. @all needs admin.

## Adding a New Command

1. `src/kouhai_bot/handlers/cmd/yourcommand.py` with `async def handle(ctx: Ctx)`.
2. `def register()` → `registry.register(CommandDef(name, aliases=[], description, usage,
   handler=handle, cooldown))`. `aliases=[]` unless approved.
3. If it mutates state/history, route through coordinator helpers (`run_group_state_update`);
   DB read/modify/write under the same per-group lock.
4. Publish active-request metadata if stateful so `/status` stays correct.
5. Auto-discovered, auto-listed in `/help`. Group-only.

## Pitfalls (subtle correctness invariants)

Architecture mechanics are described above; these are the easy-to-break invariants:

1. **One shared coordinator per group** for submit/clarify/review/clear — no ad hoc locks
   or side paths, or pending archives/watermarks/first-blood diverge. DB read/modify/write
   under that lock; LLM calls and the first-blood wait sleep **outside** it.
2. **Judge user_msg is JSON** `{"problem","submission","history"}`; statement joins single
   `\n` (not `\n\n`) — both affect judging accuracy.
3. **@ needs markdown** (`<qqbot-at-user id=…/>`, msg_type=2); plain-text @ is fake. @all
   works in text+markdown but needs admin + 1/group/day. Contest needs @all + 2s delay.
4. **Length is not a hard threshold**: ~2000 text / ~3000 markdown are third-party empirical;
   E4 实测 saw no 40054007 up to 60000/8000 and a user-quote read-back of a 5000-codepoint
   bot message was fully intact (no silent truncation). Bot outbound isn't echoed to the
   gateway, but a user quoting it returns the delivered text in `msg_elements[0].content` —
   the read-back verification path. Long-image cards are primary; don't chunk for an
   unproven limit. One content type per request (0/2/7 exclusive; markdown content/ark empty).
5. **Offtopic is model output** (`reaction="123"`), not local substring blacklists.
6. **Save-before-deliver** (clarify/review/submit) so /feedback targeting stays consistent.
7. **Editorial first AC only, two messages**: congrats markdown @winner; editorial separate
   background image card. Never bundle; never await translation in finalize. Review gets
   English (+code) for grounding; the card gets Chinese without code.
8. **High-rating caution**: after a card for rating >2800, send a short separate warning
   (hard problem, bot may be wrong, check official editorial).
9. **DB single connection → connection-wide lock**; bad_reports corrupt → raise (never
   reset), last_interaction corrupt → warn+default.
10. **Every message text matters** — validation/error/success wording is deliberate;
    preserve intent. Nickname fallback `openid[:8]`, never "群友".

## Testing

```bash
uv sync --group dev
uv run python -m pytest tests/ -v
```
`tests/conftest.py`: async `db` fixture (tmp SQLite); autouse blocks real
`aiohttp.ClientSession._request` (no live network); autouse resets global
runtime/database/echo/cooldowns/message-dedup. Handler tests inject a fake runtime
(`set_runtime(Runtime(db, OfficialSender(FakeRest(), ReplyBudget(db)), browser=None))`) and
monkeypatch LLM helpers. Covers db repos, transport, render, dispatch/registry/echo,
coordinator/commands, sync, feedback, cards, model tags, config, main lifecycle, worker,
prefetch/preparation, editorial followup/tutorials, scheduler, shared.

### Live field probe (owner-led 实测)

`tools/official_probe.py` gathers ground-truth payloads — implement live-shape features
only after calibration, never speculate. E3 (avatar/mentions/quote/ref_idx) and E4 (length)
are calibrated (see DECISIONS.md). Subcommands:
- `capture` — read-only gateway listener; dumps raw group events to `{data_dir}/probe/*.jsonl`,
  highlights avatar/mentions/msg_elements(103)/scene.ext. Stop the live bot first (same
  app_id gateway conflict).
- `send --group <gid> [--kind text|markdown] [--text …] [--recall]` — posts one message,
  prints FULL response (incl. `ext_info.ref_idx`), optionally recalls (≤2min).
- `length --group <gid> [--kind …] [--send] [--recall]` — increasing-length ladder; dry-run
  default, `--send` posts, `--recall` recalls each OK. A clean ladder alone doesn't prove
  untruncated delivery — verify via user-quote read-back of `msg_elements[0].content`.

## GitHub CI

`.github/workflows/require-markdown-docs.yml` fails PRs to `master` unless they add/modify
≥1 `*.md` file. Keep `require-markdown-docs` a required check.

## Startup

```bash
cd ~/kouhai-bot
uv run start      # detached (nohup), pidfile by app_id; refuses if pidfile alive
uv run restart    # stop existing app_id instance, start fresh
uv run stop       # stop the app_id instance
uv run status     # action=/app_id=/occupied=/current_worktree_running=/pid=
```
Logs `{data_dir}/logs/{app_id}/YYYY-MM-DD.log`. Foreground debug:
`uv run python -m kouhai_bot.worker`. prod/test use separate app_id (pidfile/DB).

## Design Principles

- **No auto daily post catch-up**: missed is missed; don't auto-recover.
- **New problem delivery**: long-image card from custom nodes + text fallback; commit state
  only after delivery.
- **Fresh start, no legacy migration**: SQLite built fresh; old JSON chat records NOT
  migrated (owner decision). Disk statement mirror retained for crawler reuse only.
- **Preserve UX intent**: when an official-API limitation blocks a legacy feature,
  understand WHY it existed and build a replacement preserving the intent (reactions →
  tiny emoji, merged-forward → long images, top5 removed but congrats cheer+reveal kept).
  Do not speculate (臆想); every present AND absent feature has a reason.