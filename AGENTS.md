# Kouhai Bot — Development Guide

Instructions for AI coding assistants working on this codebase.

The bot runs on the **official QQ bot API** (api.bot.qq.com / api.sgroup.qq.com),
not NapCat/OneBot. It is a **pure group bot** serving **many groups** from one
worker; each group is an independent view over a shared SQLite database. Private
judge, 打星/user_groups, curfew, and jev have been removed. The full migration
decision log lives in `docs/official-migration/DECISIONS.md` (D1–D16, open questions
Q6–Q10); read it before changing architecture.

## Operational Constraints

- All changes (code, config, docs) must be prepared in a **new git worktree** on
  a branch and merged by PR. Never edit the live `master` checkout directly.
  Use the `gh` CLI to create the PR; merge only after the owner confirms the PR
  is correct.
- The live service checkout must remain on `master`. Do not use `uv run restart`
  or otherwise change live service state unless explicitly asked. Running tests is
  allowed.
- Never commit private information into git-maintained content: QQ numbers, group
  chat numbers, group/user openids, AppID/AppSecret, and API keys must not appear in
  commits, branches, PRs, or any tracked file. Use placeholders in examples and docs.
- `AGENTS.md` is the single agent-instruction file for this repo. Keep it up to
  date whenever architecture or workflow rules change; do not create `CLAUDE.md`
  or other parallel instruction files.
- Judge and review tasks must use the configured highest reasoning capability. Do
  not lower `reasoning_effort`, disable thinking, or switch to a weaker effort to
  work around latency/cost.
- Do not set client-side output token caps, `max_tokens`, or
  `max_completion_tokens` for judge/review requests. For DashScope/百炼 thinking
  models only, set `thinking_budget=100000` so reasoning cannot consume the
  entire upstream output window before `content` starts.
- GLM-5.3 (Zhipu/火山方舟) exposes no thinking-budget parameter — its only depth
  control is `reasoning_effort` (`low`/`high`/`max`) and thinking cannot be
  disabled — so never send it budget-style fields. A very long reasoning run
  can still return empty `content`; `glm53` therefore runs with `stream: true`
  (Ark streams support `stream_options.include_usage`) so that case is
  classified as `empty_content_after_reasoning`/`length` and fails over instead
  of being blindly retried. Both completion paths log `finish_reason`,
  `reasoning_chars`, and `usage` for diagnosis.

## Architecture

```
Official QQ Gateway ──outbound WS──> worker.py (OfficialGateway)
                                          │
                                          ├── normalize_event → handlers.process_event(spawn_handlers=True)
                                          ├── handlers/cmd/*.py  auto-discovered by registry (Ctx)
                                          ├── per-group GroupServices (lazy on first group event):
                                          │     ├── NextProblemPrefetcher (one READY slot)
                                          │     └── editorial_prefetch_maintenance_loop
                                          ├── scheduler_loop (60s tick, iterates discovered groups)
                                          └── echo (auto-repeater, per group)
State: SQLite {data_dir}/kouhai.db (db/)   Render: Playwright long-image (render/)
Transport: official/ (tokens/rest/gateway/sender/budget/media)
```

## Key Design Decisions

- **Identity (D1)**: primary keys are official openids (str). User = `user_openid`
  (group events carry `author.member_openid`, verified cross-group stable == C2C
  user_openid). Group = `group_openid` (str). Message id = str (`ROBOT1.0_…`).
  No int QQ/group numbers internally, no legacy migration, no /bind, no identity_map.
- **Group = view (D3/D9)**: one global `submissions` table (user×problem) plus
  `group_posts`/`group_solves`. Each group's scoreboard is the aggregate of
  submissions joined to that group's posts. Solving P in group A records globally;
  group B learns only when the user `/sync`s in B.
- **Database (D3)**: SQLite + aiosqlite, single file `{data_dir}/kouhai.db`, WAL,
  foreign keys on, explicit `BEGIN IMMEDIATE` transactions. Tables: meta / users /
  groups / problems / editorials / group_posts / submissions / group_solves /
  bad_reports / last_interaction / reply_budget / prefetch_slot / group_used.
  Repos in `db/repos/*.py`; `Database` in `db/database.py`
  (`get_database()`/`set_database()`). Transactions replace the old
  atomic_write_json + coordinator-lock read-modify-write sections.
- **Transport (D4)**: `official/` exposes `OfficialSender` (send_group_text /
  send_group_markdown / send_group_at / send_group_media / send_group_image_bytes /
  send_group_image_url / react / recall). No OneBot segments. Content mapping:
  - incorrect verdict / clarify / congrats / short review → **markdown** (`msg_type=2`,
    `<qqbot-at-user id="member_openid"/>` for @, `<qqbot-at-everyone/>` for @all).
  - card-level (problem / help / scoreboard / long review / editorial / history) →
    **long image** via `render/` (Playwright Chromium screenshot of a high-fidelity
    mobile-QQ forward-card HTML replica), uploaded as base64 media (`msg_type=7`).
  - pure confirmations (👀 ack / 👌 clear+feedback / 😵 offtopic / 🧐 second-judge) →
    tiny group text/emoji messages. The 🧐 second-judge signal carries **no text**
    ("二审" is dev jargon; the original was a reaction only).
  - `ReplyBudget` (`official/budget.py`): passive first (group 5min/5 replies per
    msg_id), active fallback outside window/quota (1000/group/day persisted in DB,
    20/qpm in memory); graceful degradation on failure.
  - `react()` has no official reaction equivalent → sends a tiny emoji character
    message. Recall = DELETE messages/{id} (own <2min).
- **Media (Q2)**: `official/media.py` MediaCache keys content sha256 → file_info,
  expires ttl−30s, reused across sends. Upload POST /v2/groups/{gid}/files with
  `file_data` (base64), `srv_send_msg:false` returns file_info only; then send
  `msg_type=7 media:{file_info}`.
- **Render (P2)**: `render/browser.py` RenderBrowser singleton (headless Chromium,
  `screenshot_html(html, width=720, device_scale_factor=2)`); `render/html.py`
  forward-card HTML; `render/forward_card.py` `send_forward_card(sender, group,
  nodes, *, title, footer, msg_id, text_fallback, browser)` renders→uploads→sends
  image, degrades to plain text on failure. Card content builders live in
  `handlers/cards.py`.
- **Multi-group runtime (D7/D15)**: dispatch serves any group that sends an event
  (no `current_group` filter; group membership is the natural gate). Worker keeps
  `group_openid → GroupServices`, created lazily on first event; scheduler/echo
  iterate discovered groups. C2C/private events are ignored (private judge disabled).
- **Lifecycle (D5/D16)**: official outbound WS has no listen port, so instance
  identity is `app_id`. pidfile `{data_dir}/run/{app_id}.pid`; start/restart/stop/
  status select by pidfile + process cwd. Logs `{data_dir}/logs/{app_id}/YYYY-MM-DD.log`.
  prod/test双 bot = 双 app_id (separate pidfile/DB).
- **Command auto-discovery**: each `handlers/cmd/*.py` exports `register()` returning
  a `CommandDef`; `registry.discover_commands()` scans with `pkgutil.iter_modules`.
  Handlers receive a single `Ctx` (D8). Adding a command = new `.py` with `register()`.
- **Limited aliases**: only six short aliases: `/sbm`→submit, `/clrf`→clarify,
  `/rv`→review, `/np`→newproblem, `/pb`→problem, `/fb`→feedback. New commands default
  to `aliases=[]` unless explicitly approved.
- **Handler context (D8)**: `handlers/context.py` `Ctx(group_openid, user_openid,
  message_id, raw_text, args, nickname, event)` with properties `db/sender/browser/name`
  reading `handlers/runtime.py` `get_runtime()`. Worker calls `set_runtime(Runtime(db,
  sender, browser))` at startup; tests inject a fake. `display_name(openid, nick)`
  falls back to `openid[:8]` — never a generic placeholder. Since the 2026-11-16
  official event update, group `author` carries `username`/`member_nickname`/`avatar`;
  `normalize_event` resolves `nickname` (group card preferred, then QQ nickname) and
  passes `avatar` + structured `mentions`/`message_scene`/`msg_elements` through.
- **Dispatch (D14)**: `handlers.process_event` handles only normalized group messages;
  normalizes leading command junk, runs `echo.check_and_echo` for non-command messages,
  resolves the command, enforces a per-(group,user,command) cooldown (`CommandDef.cooldown`,
  silent drop within cooldown), canonicalizes alias→canonical text, builds Ctx, and
  spawns (`create_task`) or awaits the handler. No reaction/notice/poke/friend-request
  branches (no official equivalent).
- **Echo (auto-repeater)**: `echo.py` per-group `GroupEcho` buffers recent messages;
  when ≥`trigger_count` identical non-command messages stack and no bot entry is in the
  streak, with probability `echo_probability` it repeats the content via
  `sender.send_group_text`. Needs GROUP_MESSAGE_CREATE (全量群消息). Not a user command.
- **Rating range (N4)**: global default from `problem.min_rating/max_rating`; per-group
  override via `/range` (any member). Smart parsing: extract integers, exactly two →
  min/max (validate min<max + reasonable range); separators flexible (-/~/～/到/space);
  three numbers or non-numbers → usage hint. Stored in `groups` table
  (`users.get_group_rating`/`set_group_rating`).
- **Newproblem cooldown + --force kept**: deliberate "不鼓励换题" friction. Plain
  `/newproblem` on an unsolved problem is rejected; exact `/newproblem --force` forces.
- **Removed features**: private judge (whole side), 打星/user_groups/dynamic submit CD,
  curfew, jev/sbmjev, top5 scoreboard, NapCat transport, friend-request auto-approval,
  poke/notice. `/setproblem`(`/sp`), `/testcd`, `/bad` commands deleted.
- **MathJax formula deduplication**: `problems/fetcher.py:normalize_mathjax()` keeps only
  the TeX source wrapped in `$$$...$$$`, discarding rendered/MathML copies. Called from
  `html_to_text()` and tutorial `html_to_markdownish()`. Without it the judge sees
  duplicated numbers.
- **Unified CF HTML transport**: `problems/cf_fetcher.py` tries cloudscraper then
  headless Playwright Chromium on 403/timeout/challenge/placeholder; `content_valid()`
  is the shared usability gate. Editorial scraping (`tools/cf_tutorial_agent.py`,
  `tools/scrape_cf_tutorial.py`) delegates page fetching to it.
- **Editorial extraction matches Div1/Div2 mirrors by title** and distinguishes
  easy/hard versions; anything uncertain → `match=false` (judge correctness depends on
  it). Same-title sibling codes injected from the CF problemset API (7-day cached).

## Configuration

Runtime config comes from `config.yaml` at the repo root (or `KOUHAI_CONFIG=/path`).
The file is **never committed** (`.gitignore`). Copy `config.example.yaml`.

### `official` section (required)

| Key | Default | Description |
|-----|---------|-------------|
| `official.app_id` | `""` | Bot AppID (**required**; env `KOUHAI_APP_ID`) |
| `official.app_secret` | `""` | Bot AppSecret (**required**; env `KOUHAI_APP_SECRET`) |
| `official.bot_name` | `""` | Card attribution display name (no official nickname API) |
| `official.sandbox` | `false` | Use sandbox REST domain |
| `official.api_base` | `https://api.sgroup.qq.com` | REST base |
| `official.token_base` | `https://api.bot.qq.com` | access_token base |
| `official.intents` | `(1<<25)\|(1<<26)\|(1<<24)` | group+C2C / interaction / group-member |

`BotConfig.validate_official()` fails fast if app_id/app_secret missing. `bot_qq` and
`current_group` are now **optional** (default 0); legacy `napcat_*`, `user_groups`,
`curfew`, `jev`, `qwen` fields are still parsed but unused (deprecated stragglers).

### `llm` section

Two ordered fallback queues: `llm.smart_model` (judge/review) and `llm.general_model`
(everything else: clarify incl. image-bearing statements, summaries, editorial
translation, sample-note translation, JSON repair). `multimodal_*` task names only mark
image parts; they route to `general_model`, so those providers must accept images.
Each provider retried `llm.max_retries` times before moving to the next. All providers
use OpenAI-compatible `/chat/completions`; `base_url` includes any version prefix.

| Key | Default | Description |
|-----|---------|-------------|
| `llm.max_retries` | 2 | Retries per provider |
| `llm.retry_base_delay_sec` | 1.0 | Exponential backoff base |
| `llm.retry_max_delay_sec` | 8.0 | Backoff cap |
| `llm.judge_timeout_sec` | 1200 | Judge timeout |
| `llm.clarify_timeout_sec` | 600 | Clarify timeout |
| `llm.review_timeout_sec` | 600 | Review timeout |
| `llm.summary_timeout_sec` | 120 | Summary + editorial translation timeout |

Per provider: `name`/`api_key`/`model` (required), `base_url`, `reasoning_effort`,
`stream` (SSE), `send_thinking` (default true), `temperature`, `extra_body`, `model_tag`.
DashScope/百炼 providers (detected by base URL or name) and `stream:true` providers use
HTTP+SSE. Do not blindly send `stream=true` to every provider — some gateways reject
unknown fields. Transient failures (timeouts, aiohttp errors, 408/409/429/5xx, malformed
empty-choice/SSE) retry with backoff; other 4xx fall back immediately. If all providers
exhaust, the user-facing reply suggests contacting an administrator.

### `problem` section

| Key | Default | Description |
|-----|---------|-------------|
| `problem.min_rating` | 2000 | Global min CF rating |
| `problem.max_rating` | 3000 | Global max CF rating |
| `problem.newproblem_cooldown` | 300 | `/newproblem` cooldown (seconds) |
| `problem.submit_ac_backdoor` | `""` | Non-empty: matching `/submit` skips judge |

`data_dir` (default `~/.kouhai-bot`) holds kouhai.db, statement cache mirror, logs,
pidfiles, and the JSON next-problem READY slot.

### Model tags

Each provider's `model_tag` (e.g. `🐳`, `『GPT5.6』`) is appended (no newline) to every
LLM-generated user message at display time. The LLM never sees tags. Context records
store raw text + a dedicated `model_tag` field; prompt builders run stored reply/reason
through `llm.strip_model_tags` (removes trailing configured tags, preserves mid-text).
`llm.append_model_tag(text, tag)` is the single display-time append helper (rstrips,
refuses to double a tag the text already ends with). History cards render the tag once.

## Data Directory

`{data_dir}/` (default `~/.kouhai-bot/`):
```
kouhai.db                       # SQLite: all persistent state (openid native)
run/{app_id}.pid                # instance pidfile
logs/{app_id}/YYYY-MM-DD.log    # worker log (+8 tz)
statements/<pid>.json           # CF statement cache MIRROR (crawler reuse; DB is source of truth)
groups/<gid>/next_problem.json  # durable READY prefetch slot (format v2; JSON, not DB)
```
The DB is the source of truth for users/groups/problems/editorials/posts/submissions/
solves/feedback/last_interaction/reply_budget/group_used. The disk statement cache is
kept only as a crawler mirror; the picker writes it, the main process writes the DB
catalog (D6/D13). The `prefetch_slot` DB table exists but the live READY slot is the
JSON file (the table is currently unused).

## Command Handlers

All handlers are `async def handle(ctx: Ctx)`. Group-only; no @mentions needed to
trigger; private chat ignored.

| Command | File | Notes |
|---------|------|-------|
| `/submit` (`/sbm`) | submit.py | Judge via `smart_model`; coordinator-scheduled; first-blood scoring; offtopic 😵 leaves no record; second judge 🧐 only when verified editorial cached |
| `/clarify` (`/clrf`) | clarify.py | `general_model`, JSON output, anti-spoiler (no original identity); markdown @asker |
| `/review` (`/rv`) | review.py | Latest solved problem (admission snapshot); `smart_model`; @others' context; English editorial in LLM context only; short→markdown @asker, long→long image |
| `/newproblem` (`/np`) | newproblem.py | Claims READY slot; unsolved needs exact `--force`; cooldown; long-image card; commits state only after delivery |
| `/problem` (`/pb`) | stubs.py | Resend current problem card |
| `/tag` | stubs.py | Current problem CF tags |
| `/scoreboard` | stubs.py | Weighted leaderboard `2^((rating-2000)/300)`; tied ranks; no top5 |
| `/status` | stubs.py | Active stateful work for this group/user |
| `/sync` | sync.py | Cross-group pull of user×current-pid submissions; exact dedup; import tagged synced (no source group shown); correct→score |
| `/clear` | clear.py | Clear caller's current-problem history; 👌 ack; clear watermark |
| `/feedback` (`/fb`) | feedback.py | Mark latest DELIVERED reply (last_interaction cache only); append verbatim snapshot to bad_reports; 👌 ack |
| `/help` | help.py | Auto-generated from registry; long-image card |
| `/range` | range.py | Set per-group rating range (smart parsing) |

### Stateful Command Runtime

Stateful commands (`/submit`, `/clarify`, `/review`, `/clear`) run through a per-group
`GroupCoordinator` (`handlers/cmd/submit.py`); `/newproblem` uses its own per-group post
lock. Key rules (unchanged in spirit from the legacy design, now DB-backed):

- **Admission-time snapshots**: each request gets a monotonic per-coordinator sequence
  and snapshots its target pid at admission. Group submit/clarify/clear use the then-current
  group problem; review uses the then-latest solved problem. Do not re-read the target during
  compute (a newer AC must not retarget an older review).
- **Parallel compute**: LLM work starts immediately under a global concurrency limit of 8.
  Same-user requests do not wait for earlier LLM calls.
- **Pending archive**: admitted user input is saved immediately with `result="pending"` +
  `request_id`; later same-user requests load pending + completed history (excluding own
  `request_id`). Final handling upserts that record. Superseded/timeout/failure records
  remain context with empty reason/reply.
- **Offtopic leaves no record**: judge `reaction="123"` → remove pending record by
  `request_id`; tiny 😵 message; nothing in history/prompts/sync.
- **Short state critical sections**: DB read/modify/write under the coordinator lock
  (`run_group_state_update`); LLM calls never hold it.
- **First-blood serialization**: incorrect/offtopic/failure replies send immediately.
  Correct submits save immediately; if earlier admission-order candidates are unresolved,
  the user first gets "做法判对了～"; only the earliest correct candidate after earlier ones
  settle updates the scoreboard, sends cheer+@winner+reveal, and schedules the editorial.
  Later correct candidates log `post_solve_correct`, save, no extra message.
- **Same-user submit replacement**: a newer `/submit`/`/clear` for the same user+problem
  before an earlier submit reaches terminal handling silently drops the older one (cancel
  compute, log `status="stale"`); older pending record → `result="superseded"` (or removed
  by /clear). The 👀 ack is not a terminal reply.
- **New problem visibility**: commands keep seeing the old problem until the new card is
  delivered; failed delivery keeps the old current problem.
- **Save-before-deliver**: clarify/review/submit save the record BEFORE delivering the reply
  so `/feedback` never sees a sent-but-unsaved pending tail. Preserve this ordering.

Status helpers: `get_group_lock_status(group_openid)`,
`has_active_problem_request(group_openid=, user_openid=, pid=)`,
`get_newproblem_status(group_openid)`.

### `/submit` judge flow

1. Extract submission text; admission snapshot pid/solved state under coordinator lock.
2. AC backdoor short-circuit if configured.
3. Otherwise load history, 👀 ack, call judge (`smart_model`). Offtopic `reaction="123"` →
   😵 tiny message, no record.
4. Already-solved at admission → "已经有人解出..." without judge.
5. Save record (troll if offtopic else correct/incorrect). Correct + earlier unresolved →
   "做法判对了～" then wait for first-blood resolution. Correct + first → `posts.record_solve`
   (weighted score, rank, congrats cheer + @winner + reveal "本题来自 CF{pid} {name} {rating}✨"),
   then `schedule_post_solve_editorial_followup` (background, not awaited).
6. Incorrect → markdown @asker with judge reason (empty reply → "reason。再想想？🤔").
7. **Second judge**: a first-pass correct verdict triggers a second judge ONLY when a verified
   official editorial is cached (`get_verified_official_editorial` non-None); the 🧐 signal is
   an emoji-only message (no text). No cached editorial → no second judge, no 🧐.

`judge_submission()` sends `json.dumps({"problem","submission","history"})` — the prompt
expects JSON; do not change to plain text. The statement joins sections with single `\n`
(not `\n\n`).

### `/clarify`

`general_model`, `response_format: json_object`, `thinking:enabled` + `reasoning_effort`
sent unconditionally (unsupported fields ignored upstream). Output `{"reply","reaction"}`;
`reaction="123"` → 😵 only. Must not leak solution hints or original identity (id/title/contest).
Grounding uses the current pid's verified summary (`problems.summary_zh` + fingerprint gate).

### `/review`

Targets the latest solved problem at admission. Loads caller's history; @mentioned users'
same-problem context appended (ignore @all/bot/self/duplicates; nothing persisted to mentioned
users). `smart_model`, free text. If a verified editorial exists, append the **English** source
via `format_editorial_for_review()` (truncated 12k) — model-only, never pasted to the user.
Short reply → markdown @asker; long (>400) → long image + a short @asker note.

### `/sync` — cross-group pull

In group G for current problem P: pull the user's P submissions from ALL other groups
(`submissions.get_cross_group_submissions`), sort by global timestamp, **exact dedup**
(content+result identical → keep earliest; pure string, zero LLM), import tagged `synced`
via `insert_synced_submission` (source group NOT shown). If imported contains correct and G
has not solved P → `posts.record_solve(source="synced")` (first-blood/rank/congrats reveal);
if G already solved → cheer "只同步记录，不再加分". Idempotent (re-sync with nothing new →
friendly message). Empty source → abort, no overwrite. Active same-user/problem request →
reject. Sends imported history as a long-image card; 👌 ack.

### `/feedback`

Targeting is scope-local, crosses problem switches, survives `/clear`: the per-user
`last_interaction` cache (DB) is the ONLY source — never scans history. Markable = any
record whose live message was delivered (`REPLIED_RESULTS`: correct/incorrect/clarify/review/
timeout/service_unavailable/no_statement); pending/superseded unmarkable. Appends a verbatim
record snapshot (reply/reason keep model tags) to `bad_reports` (DB). 👌 ack. Repeat
invocations append new entries. Corrupt bad_reports → raise (never silent reset); corrupt
last_interaction cache → warn + None. Classification via `shared.record_kind`.

### `/newproblem`

Per-group post lock; busy → short reminder, not queued. Cooldown starts only after successful
delivery. Claims the one-slot READY prefetch (or awaits its single-flight cold build); formats
previous-problem reveal; assembles long-image card (summary + sample nodes + optional translated
notes node + snake image); commits `group_posts` + verified summary only after delivery; releases
slot and wakes refill. Plain `/newproblem` on unsolved → reject (use exact `--force`). Forced
refresh announces the skipped problem and sends its cached editorial card when available.

### Official tutorials

Scraping offline (`tools/tutorial_tools.py crawl|agent|validate`); runtime
`editorial_preparation.py` (side-effect-free exhaustive search + validation + translation) +
`tutorials.py` (DB persistence/read boundary, `editorials` table, verified marker bound to
 BOTH editorial and statement fingerprints). `get_verified_official_editorial(pid)` returns
English source only when fingerprints match; `has_cached_editorial_zh`/`is_no_official_editorial`
are source-bound terminal checks. Worker editorial maintenance covers current + READY pids,
resumes interrupted work, retries INCOMPLETE with exponential backoff (60s base, 6h cap);
only a completed exhaustive search writes a `no_editorial` marker. Delivery on first AC via
`editorial_followup.schedule_post_solve_editorial_followup` (background; warm cache → immediate;
else await in-flight prefetch then deliver if verified; none → silent skip). Group card translation
(`shared.translate_editorial_to_zh`) omits code, avoids LaTeX, normalizes symbols for QQ.
`/review` uses English editorial in LLM context only; the post-AC card uses Chinese without code.

### Scheduler

`scheduler/engine.py` multi-group loop (60s tick) iterates `await get_groups()` (discovered
groups); per-group job dedup by date+time; `load_group_config(group_openid)` defaults
`contest_check`. `scheduler/jobs.py` `contest_check(group_openid)` →
`shared.check_contests_for_group` fetches CF contest.list, builds lines, sleeps 2s, sends
markdown `<qqbot-at-everyone/>` + lines via `sender.send_group_markdown`. @all needs admin.

## Adding a New Command

1. Create `src/kouhai_bot/handlers/cmd/yourcommand.py`.
2. `async def handle(ctx: Ctx)`.
3. `def register()` returning `registry.register(CommandDef(name=..., aliases=[], description=...,
   usage=..., handler=handle, cooldown=...))`.
4. `aliases=[]` unless explicitly approved.
5. If it mutates group state/history, route through the coordinator helpers in `submit.py`
   (`run_group_state_update`); DB read/modify/write under the same per-group lock.
6. Publish active-request metadata if stateful so `/status` stays correct.
7. Auto-discovered, auto-listed in `/help`. Group-only (no private whitelist anymore).

## Pitfalls & Lessons Learned

1. **Coordinator state must be shared**: all group submit/clarify/review/clear entry points
   use the SAME per-group coordinator. No ad hoc locks/side paths, or pending archives, clear
   watermarks, and first-blood resolution diverge.
2. **Judge user_msg is JSON** `{"problem","submission","history"}` — do not change to plain text.
3. **Statement joins with single `\n`** not `\n\n` (affects judging accuracy).
4. **@mentions need markdown**: `<qqbot-at-user id="member_openid"/>` only works in `msg_type=2`
   markdown; plain text @ is fake. @all = `<qqbot-at-everyone/>` (admin, 1/group/day).
5. **Contest notifications need @all** + 2s delay.
6. **One content type per request**: msg_type 0/2/7 are mutually exclusive; markdown content/ark
   must be empty when sending markdown. Text ~2000 / markdown ~3000 codepoints; longer → long image.
7. **Passive reply window**: msg_id valid 5min/5 replies; same msg_id+msg_seq dup fails. ReplyBudget
   handles passive→active degradation.
8. **Nickname fallback is openid[:8]**, never a generic placeholder like "群友". Since
   2026-11-16 official group events carry `author.member_nickname`/`username` (use them;
   group card preferred); `users.nickname` stores the snapshot. `author.avatar` URL also
   exists but real delivery is unverified — render still uses letter-color circles.
9. **Every message text matters**: validation/error/success wording is deliberate; preserve intent.
10. **Offtopic is model output** (`reaction="123"`), not local substring blacklists.
11. **Save-before-deliver** for clarify/review/submit so /feedback targeting is consistent.
12. **Cards are long-image with text fallback**: `send_forward_card` degrades to plain text when
    rendering/upload fails; problem card content must persist so `/problem` can rebuild.
13. **Dispatch uses create_task** so lightweight commands don't queue behind locked operations.
14. **Private chats are ignored** (pure group bot); no @ segments/reactions in any private path
    because there is no private path.
15. **DB writes are transactional**: use repos + `Database.transaction()`; bad_reports corrupt →
    raise, last_interaction corrupt → warn+default.
16. **Editorial: first AC only, two messages**: congrats is a direct markdown @winner; editorial is
    a separate background long-image card. Never bundle them; never await translation in finalize.
17. **Review vs card editorial forms differ**: review gets English (+code) for grounding; the card
    gets Chinese without code.
18. **High-rating caution**: after a card for rating >2800, send a short separate warning that the
    problem is hard and the bot may be wrong; check the official editorial.
19. **Active status tracking**: stateful commands publish active-request metadata for `/status`.
20. **Instance identity is app_id pidfile**, not a WS port (official outbound WS has no listener).
    Do not kill by broad process names; prod/test coexist via separate app_id.

## Testing

```bash
uv sync --group dev
uv run python -m pytest tests/ -v
```
`tests/conftest.py` provides an async `db` fixture (tmp SQLite), an autouse fixture blocking
real `aiohttp.ClientSession._request` (no live network), and an autouse fixture resetting global
runtime/database/echo/cooldowns between tests. Handler tests inject a fake runtime
(`set_runtime(Runtime(db, OfficialSender(FakeRest(), ReplyBudget(db)), browser=None))`) and
monkeypatch LLM helpers. The suite covers db repos, official transport, render, dispatch/registry/
echo, coordinator/commands, sync, feedback, cards, model tags, config, main lifecycle, worker,
prefetch/preparation, editorial followup/tutorials, scheduler, shared helpers.

## GitHub CI

- `.github/workflows/require-markdown-docs.yml` runs on PRs targeting `master`; it fails unless
  the PR adds/modifies at least one `*.md` file. Keep `require-markdown-docs` a required check.

## Startup

```bash
cd ~/kouhai-bot
uv run start          # detached background instance (nohup), pidfile by app_id
uv run restart        # stop existing app_id instance, start fresh
uv run stop           # stop the app_id instance
uv run status         # action=/app_id=/occupied=/current_worktree_running=/pids=
```
Logs: `{data_dir}/logs/{app_id}/YYYY-MM-DD.log`. Foreground debug:
`uv run python -m kouhai_bot.worker`. If the app_id pidfile is already alive, `start` refuses
to launch a second instance. prod/test use separate app_id (and thus separate pidfile/DB).

## Design Decisions

- **No auto daily post catch-up**: missed is missed; don't auto-recover.
- **New problem delivery**: long-image card assembled from custom nodes + text fallback; commit
  state only after delivery.
- **Fresh start, no legacy migration**: the SQLite DB is built fresh; old `~/.kouhai-bot` JSON
  chat records are NOT migrated (owner decision). The disk statement cache mirror is retained
  for crawler reuse only.
- **Preserve UX intent**: when an official-API limitation blocks a legacy feature, understand
  WHY it existed and build a replacement that preserves the intent (e.g. reactions → tiny emoji
  messages, merged-forward cards → long images, top5 removed but congrats cheer+reveal kept).
  Do not speculate (臆想); every present AND absent feature has a reason.