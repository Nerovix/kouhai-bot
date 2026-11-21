# Kouhai Bot — Development Guide

Instructions for AI coding assistants. The bot runs on the **official QQ bot API**
(`api.bot.qq.com`; `api.sgroup.qq.com` aliases it), not NapCat/OneBot. It is a **pure group
bot** serving **many groups** from one worker; each group is an independent view over a
shared SQLite DB. Private judge, 打星/user_groups, curfew, jev, echo are removed. Full
rationale (D1–D17, open questions, API audit, self-review) is in
`docs/official-migration/DECISIONS.md` — read it before changing architecture. This file is
the operative quick reference.

## Operational Constraints

- All changes go in a **new git worktree** on a branch, merged by PR (`gh`). Never edit the
  live `master` checkout directly; merge only after the owner confirms the PR is correct.
- Live service stays on `master`. Do not `uv run restart` or change live service state
  unless explicitly asked. Running tests is allowed.
- **Never commit private information**: QQ numbers, group chat numbers, group/user openids,
  AppID/AppSecret, API keys must not appear in commits, branches, PRs, or any tracked file.
  Use placeholders in examples/docs.
- `AGENTS.md` is the single agent-instruction file; keep it current. No `CLAUDE.md` or
  parallel instruction files.
- Judge/review use the configured **highest** reasoning capability; never lower
  `reasoning_effort`, disable thinking, or weaken effort for latency/cost. No client-side
  `max_tokens`/`max_completion_tokens` for judge/review. DashScope/百炼 thinking models
  only: set `thinking_budget=100000`.
- GLM-5.3 (Zhipu/火山方舟): no thinking-budget field — depth is `reasoning_effort`
  (`low`/`high`/`max`) only, thinking cannot be disabled; never send budget-style fields.
  Long reasoning may return empty `content`, so `glm53` runs `stream: true` to classify that
  as `empty_content_after_reasoning`/`length` and fail over. Both paths log `finish_reason`,
  `reasoning_chars`, `usage`.

## Architecture

```
Official QQ Gateway ──outbound WS──> worker.py (OfficialGateway)
   ├── normalize_event → handlers.process_event(spawn_handlers=True)
   ├── handlers/cmd/*.py  auto-discovered by registry (Ctx)
   ├── per-group GroupServices (lazy on first event; seeded from group_posts at boot):
   │     ├── NextProblemPrefetcher (one READY slot)
   │     └── editorial_prefetch_maintenance_loop
   └── scheduler_loop (60s tick, iterates discovered groups)
State: SQLite {data_dir}/kouhai.db (db/)   Render: Playwright long-image (render/)
Transport: official/ (tokens/rest/gateway/sender/budget/media/keyboard)
```

## Core Design

- **Identity (D1)**: PKs are official openids (str). User = `user_openid` (group events
  carry `author.member_openid`, cross-group stable == C2C user_openid). Group =
  `group_openid`. Message id = str (`ROBOT1.0_…`). No int QQ/group numbers, no /bind. Legacy
  group data hot-migrates by nickname (see `legacy_migration`).
- **Group = view (D3/D9)**: one global `submissions` table (user×problem) +
  `group_posts`/`group_solves`. A group's scoreboard aggregates submissions joined to its
  posts. Solving P in group A records globally; group B learns only when the user `/sync`s.
- **Database (D3)**: SQLite + aiosqlite, `{data_dir}/kouhai.db`, WAL, FK on, explicit
  `BEGIN IMMEDIATE`. One shared connection guarded by a connection-wide `asyncio.Lock` —
  every `execute/fetch_*/transaction` serialized. Repos `db/repos/*.py`; `Database`
  `db/database.py` (`get_database()`/`set_database()`). Tables: meta/users/groups/problems/
  editorials/group_posts/submissions/group_solves/bad_reports/last_interaction/reply_budget/
  prefetch_slot/group_used/message_refs/legacy_migration_map/group_order_floor.
- **Transport (D4)**: `official/OfficialSender` (send_group_text/markdown/at/media/
  image_bytes/image_url, react, recall). No OneBot segments. Content mapping:
  - incorrect / clarify / congrats / short review → **markdown** (`msg_type=2`; @ =
    `<qqbot-at-user id="member_openid"/>`; @all = `<qqbot-at-everyone/>`, text+markdown since
    2026-10-26, admin + 1/group/day).
  - card-level (help/scoreboard/long review/editorial/history) → **long image** (`render/`
    Playwright screenshot of a mobile-QQ forward-card HTML replica) as base64 media
    (`msg_type=7`). `msg_type=8` 图文卡片 exists but unused. **Problem card** (`/np`+`/pb`)
    is the exception: ONE markdown message, fixed-height thumbnail + buttons same bubble.
  - confirmations (👀 ack / 👌 clear+feedback+sync / 🙅 offtopic / 🧐 second-judge) → tiny
    emoji that **@s the triggering user** (`sender.react(..., user_openid=…)`; no user →
    plain emoji). 🧐 has no other text. Button-callback `/clear`
    (`PendingRequest.from_callback`) sends no 👌 (QQ shows "操作成功" toast); typed keeps 👌.
  - `ReplyBudget`: passive first (group 5min/5 per msg_id), active fallback (1000/group/day
    persisted; per-group 20/qpm + bot-wide 60/qpm, 30 unverified). Graceful degradation.
  - No group reaction (表情表态 channel-only) or poke → tiny emoji. Recall = DELETE
    messages/{id} (own ≤2min; admin may recall member messages).
- **Media (Q2)**: `official/media.py` MediaCache keys sha256 → file_info (ttl−30s; ttl=0
  permanent). Upload POST /v2/groups/{gid}/files `file_data` base64 + `srv_send_msg:false`
  → file_info; send `msg_type=7 media:{file_info}`. **Media library (素材管理)** for
  markdown images: `rest.upload_media_image` prepare→PUT presigned→complete
  (`/v2/media-images/*`) → `qqbot-media://image/v1/<token>` url + image_id (10MB,
  png/jpeg/gif/webp, **100/bot**, 90-day sliding expiry). PUT → COS presigned (own query
  auth, no QQBot header, Content-Type == prepare value). `sender.upload_markdown_image` →
  `(url,image_id)|None`; `delete_markdown_image` frees source after delivery (platform keeps
  a 转存 copy for the sent message; a deleted url cannot be re-referenced in a NEW send →
  re-upload each time; PNG stays prerender-cached).
- **Buttons (inline keyboard)**: `official/keyboard.py` builds
  `keyboard.content.rows[].buttons[]`. **Markdown-only** (media/image can't carry buttons).
  Problem card = ONE markdown message: inline thumbnail `![题目 #Wpx #Hpx](qqbot-media://…)`
  + button row same bubble. Thumbnail **fixed display height 132**
  (`_PROBLEM_CARD_THUMB_HEIGHT`; `_thumb_hint(w,h)` → `hw=round(132*w/h)`, taller statements
  shrink width not height). `#Wpx #Hpx` hint required for `qqbot-media://` (else alt text)
  and IS respected. Alt = generic title only, **no CF pid/title** (anti-spoiler).
  Render/upload/send failure → plain text (no buttons). `action.type` 0=open URL,
  1=callback (fires `INTERACTION_CREATE` with `data.resolved.button_data`, no visible
  message), 2=command (inserts `@bot <data>`; `enter=false` pre-fill / `true` auto-send;
  enter/reply type=2/single-chat-only). Command buttons' `@bot` satisfies require-@.
  `permission.type=2`, `render_data.label` ≤10 chars, `unsupport_tips` required, `bot_appid`
  from config (omitted if unset). One row left→right sharing width (1 spans, 2 split).
  Wiring: problem card [提交`/sbm`, 询问题意`/clrf`]; clarify reply [提交`/sbm`,
  继续询问`/clrf`]; incorrect [继续提交`/sbm`(type2), 清空上下文`/clear`(type1)]; congrats
  [跳转原题→CF URL, 再来一题`/np`(type1)] (`congrats_keyboard(pid)`; bad pid drops jump);
  `/np --force` skip notice [跳转原题] (`jump_original_keyboard`); unsolved-`/np` rejection
  [查看当前题目`/pb`(type1)] (`view_problem_keyboard()`).
- **Callback ≡ typed command (D17)**: no-arg 再来一题(`/np`)/清空上下文(`/clear`)/查看当前
  题目(`/pb`) are **type=1 callbacks**. On `INTERACTION_CREATE` (type=11, scene=group)
  `handlers._interaction_to_message` acks (`PUT /interactions/{id} {"code":0}`,
  `sender.ack_interaction`; required for type=11/12, code=0 = "received") then synthesizes an
  equivalent group message (`at_bot=True`, `message_id=""`, `content=button_data`) through
  the **same `process_event`** — cooldowns/`--force` friction/busy reminders byte-identical;
  replies ride active quota. Dedup by `interaction_id` (same LRU). Non-type-11/non-group/
  non-slash/missing identity ignored (no ack). Divergence: callback `/clear` suppresses 👌.
  **Interaction events carry no nickname** → button-only members migrate after first
  chat/typed message (official limitation; owner accepted).
- **Render (P2)**: `render/browser.py` RenderBrowser singleton (headless Chromium,
  `screenshot_html(html, width=720, device_scale_factor=2)`); `render/html.py` card HTML;
  `render/forward_card.py` `send_forward_card(...)` renders→uploads→sends, degrades to text,
  **never raises**. `render/avatars.py` AvatarCache pre-fetches `author.avatar` (public
  `thirdqq.qlogo.cn` JPEG) to base64 data URI (parallel, LRU+TTL, 60s negative cache);
  failure → letter-color circle. Bot avatar built-in `render/assets/bot_avatar.jpg` (READY
  `username` authoritative, no avatar field). Builders `handlers/cards.py`.
- **Figures (题面/题解配图)**: statement diagrams (`statement["images"]` with
  `kind=graphic`; formulas excluded) and editorial figures are rendered inside the card
  long image. `render/figures.py` downloads each remote figure once into
  `{data_dir}/render_cache/statement_images/<sha1>.<ext>` and embeds it as a `data:` URI
  (offline-deterministic render, same trick as the snake image); failures are dropped and
  numbering follows the source position (`图 N`) so a missing figure never renumbers the
  rest. **Figures are inline, never a detached gallery** (owner rejected the trailing
  `题目配图`/`题解配图` gallery: images must sit next to the sentence that refers to
  them). The statement description's figure paragraph is extracted verbatim
  (`problems/fetcher.py:extract_figure_passage`) with `[[FIG_n]]` placeholders, so
  `problem_preparation` carries `figure_passage` + `figure_captions` alongside
  `figure_srcs`; `shared.translate_figure_passage` translates it while preserving every
  marker (deterministic count/order check, per-segment retry when the LLM drops one, source
  text fallback). `cards._figure_passage_nodes` splits the passage on markers and emits
  one text bubble per prose run plus the figures referenced by that run (2-per-row grid,
  `图 N` captions) — text that names those figures contains them. Sample
  explanations often embed diagrams too: `fetcher.extract_figure_notes` keeps
  `[[FIG_n]]` positions in the `notes` text, `translate_sample_notes` preserves/repairs
  the markers, and the notes node renders through the same interleaving. `/pb` rebuilds
  the same payload from the persisted `card_json`, or derives it from the cached
  statement for legacy posts. Editorial: `tools/scrape_cf_tutorial.py` keeps blog `<img>` as
  `[[CFIMG_n]]` markers through markdownish conversion; `localize_figure_markers` rewrites
  the extractor span's markers to deduped 1-based `[[FIG_n]]` and returns `Section.images`
  (translation keeps the markers; delivery interleaves text + figure nodes the same way).
  A gallery is only a fallback when an editorial has images but no markers.
- **Multi-group (D7/D15)**: dispatch serves any group sending an event (no `current_group`
  filter; membership is the gate). Worker keeps `group_openid → GroupServices` lazily **and
  seeds known groups from `group_posts` at startup** (prefetch/editorial resume). C2C/private
  ignored.
- **Lifecycle (D5/D16)**: outbound WS has no listen port → identity is `app_id`. pidfile
  `{data_dir}/run/{app_id}.pid`; start/restart/stop/status select by pidfile + cwd and
  **verify `/proc/<pid>/cmdline` contains `kouhai_bot.worker`** before killing (recycled PID
  must not be killed). Logs `{data_dir}/logs/{app_id}/YYYY-MM-DD.log`. prod/test = separate
  app_id.
- **Commands**: each `handlers/cmd/*.py` exports `register()` → `CommandDef`;
  `registry.discover_commands()` scans via `pkgutil.iter_modules`. Handlers take one `Ctx`.
  **Alias-only**: an aliased command registers ONLY its short alias as a usable trigger; the
  full name is internal identity (dispatch rewrites alias→canonical before parsing) and is
  NOT accepted. Six aliases: `/sbm`→submit, `/clrf`→clarify, `/rv`→review, `/np`→newproblem,
  `/pb`→problem, `/fb`→feedback. Alias-less (tag/scoreboard/status/sync/clear/range/help)
  register their own name. `CommandDef.trigger` = alias[0] if aliased else name. New commands
  `aliases=[]` unless approved. User-facing hints use the public trigger, never full name.
- **Ctx (D8)**: `handlers/context.py` `Ctx(group_openid, user_openid, message_id, raw_text,
  args, nickname, event)`; `db/sender/browser/name` read `handlers/runtime.py`
  `get_runtime()` (worker `set_runtime` at boot; tests inject fake). `display_name` falls
  back to `openid[:8]`, never a placeholder. Since 2026-11-16 group `author` carries
  `username`/`member_nickname`/`avatar`; `normalize_event` resolves nickname (card preferred)
  and passes avatar + `mentions`/`message_scene`/`msg_elements`.
- **Dispatch (D14)**: group messages only; callbacks first synthesized (D17) into the same
  path. Strip leading junk **and `<@bot>` mentions** (quoting a bot card auto-@bot, arrives
  GROUP_MESSAGE_CREATE); dedup by message_id (global LRU, silent drop before @-gate/
  cooldown). **Other-bot guard**: `mentions[]` has a bot (`bot=True`) not us (`is_you=False`)
  and none us → ignore whole message. Every group message feeds
  `legacy_migration.schedule_migration` (fire-and-forget). **Require-@ gate**:
  `_mentions_us(event)` = `at_bot` (GROUP_AT_MESSAGE_CREATE) **or** any `mentions[]`
  `is_you=True` (chip / quoted card / full-message @); un-addressed chatter and bare `/cmd`
  drop. Then per-(group,user,command) cooldown (silent drop), alias→canonical, Ctx, spawn
  (`create_task`) or await. No reaction/notice/poke/friend-request branches.
- **Inbound quote (Q10/E3, 实测)**: inbound @ renders `<@openid>` (**no `!`**).
  `message_scene.ext` = **list of `"key=value"` strings** (`msg_idx`/`ref_msg_idx`/
  `auth_token`); `parse_scene_ext` splits on first `=`. Quote = top-level `message_type=103`;
  `msg_elements[0].content` = quoted text, `.msg_idx` = quoted REFIDX == `ref_msg_idx`. No
  history API — quoted text inline. Send responses return `ext_info.ref_idx`; sender
  captures id→ref_idx. `/np`//`/pb` persist card ref_idx→pid in `message_refs` (PK
  `(group_openid, ref_idx)`); `/review` matches inbound `ref_msg_idx` via `posts.pid_for_ref`
  to target a quoted older **solved** card, else latest solved. Member/group-info REST
  whitelist-only (11253); nickname/avatar from event author.
- **Rating range**: global `problem.min_rating/max_rating`; per-group `/range` override (any
  member; exactly two ints → min/max validate, else usage hint). Stored in `groups`.
- **Newproblem friction**: cooldown + plain `/np` on unsolved rejected; exact `/np --force`
  forces. Cooldown falls back to persisted `posted_at`.
- **CF/editorial helpers**: `problems/fetcher.py:normalize_mathjax()` keeps only TeX source
  (`$$$...$$$`), discards rendered/MathML copies (else judge sees duplicated numbers).
  `problems/cf_fetcher.py` cloudscraper then headless Chromium on 403/timeout/challenge;
  `content_valid()` usability gate. Editorial extraction matches Div1/Div2 mirrors by title,
  distinguishes easy/hard; uncertain → `match=false`. Picker stderr classified on full text.
- **Removed**: private judge, 打星/user_groups/dynamic CD, curfew, jev/sbmjev, top5, NapCat,
  friend-request approval, poke/notice, echo; `/setproblem`(`/sp`), `/testcd`, `/bad`.

## Configuration

`config.yaml` at repo root (or `KOUHAI_CONFIG=/path`); **never committed** (`.gitignore`).
Copy `config.example.yaml`.

### `official` (required)

| Key | Default | Description |
|-----|---------|-------------|
| `app_id` | `""` | Bot AppID (**required**; env `KOUHAI_APP_ID`) |
| `app_secret` | `""` | Bot AppSecret (**required**; env `KOUHAI_APP_SECRET`) |
| `bot_name` | `""` | Card attribution name (user nicknames from events) |
| `sandbox` | `false` | Sandbox REST domain (largely deprecated) |
| `api_base` | `https://api.bot.qq.com` | REST base |
| `token_base` | `https://api.bot.qq.com` | access_token base (`/app/getAppAccessToken`) |
| `intents` | `(1<<25)\|(1<<26)\|(1<<24)` | group+C2C / interaction / group-member |

`validate_official()` fails fast if app_id/app_secret missing. `bot_qq`/`current_group`
optional (default 0); legacy `napcat_*`/`user_groups`/`curfew`/`jev`/`qwen` parsed but unused.

### `llm`

Two ordered fallback queues: `smart_model` (judge/review) and `general_model` (everything
else: clarify incl. images, summaries, editorial/sample-note translation, JSON repair).
`multimodal_*` tasks only mark image parts; route to `general_model` (must accept images).
Each provider retried `max_retries` times before the next. OpenAI-compatible
`/chat/completions`; `base_url` includes version prefix.

| Key | Default | | Key | Default |
|-----|---------|-|-----|---------|
| `max_retries` | 2 | | `judge_timeout_sec` | 1200 |
| `retry_base_delay_sec` | 1.0 | | `clarify_timeout_sec` | 600 |
| `retry_max_delay_sec` | 8.0 | | `review_timeout_sec` | 600 |
| | | | `summary_timeout_sec` | 120 |

Per provider: `name`/`api_key`/`model` (required), `base_url`, `reasoning_effort`, `stream`
(SSE), `send_thinking` (default true), `send_response_format` (default true), `temperature`,
`extra_body`, `model_tag`. `send_response_format:false` drops `response_format`
(`json_object`) — needed for **deepseek-flash** (degenerates to whitespace-only `content`
~75% under `json_object`; A/B 6/8 empty with, 0/8 without). Prompts already instruct JSON +
`parse_json_with_llm_repair`, so safe. DashScope/百炼 (by URL/name) and `stream:true` use
HTTP+SSE — don't blindly send `stream=true` to all (some gateways reject unknown fields).
Transient failures (timeouts, aiohttp errors, 408/409/429/5xx, malformed empty-choice/SSE)
retry with backoff; other 4xx fall back immediately. All exhausted → reply suggests
contacting an admin.

### `problem`

| Key | Default | Description |
|-----|---------|-------------|
| `min_rating` | 2000 | Global min CF rating |
| `max_rating` | 3000 | Global max CF rating |
| `newproblem_cooldown` | 300 | `/np` cooldown (sec) |
| `submit_ac_backdoor` | `""` | Non-empty: matching `/submit` skips judge |

`data_dir` (default `~/.kouhai-bot`) holds kouhai.db, statement mirror, logs, pidfiles,
render cache, JSON READY slot.

### `legacy_migration` (old service group only)

Runtime hot-migration of the legacy file-store into SQLite, gated to ONE group. Official API
has no roster/history endpoint (member-list invite-only: 11253) → QQ→openid can't be mapped
in bulk. On every service-group message dispatch captures `(openid, nickname)`, matches
nickname to an old QQ id in a staged source DB, migrates that member's old GROUP solves +
submissions once (atomic, idempotent via `legacy_migration_map`). Private data NOT migrated;
other groups no-op.

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Master switch |
| `group_openid` | `""` | Legacy service group openid (only this group migrates) |
| `source_db` | `""` | Staged `legacy_source.db` path (local runtime, **never committed**) |

`legacy_migration.py`: nickname match exact-stripped, latest preferred then historical;
ambiguous (>1 old QQ)/unmatched skipped + logged. Catalog insert-if-absent (never clobbers
live). Statements stored RAW so `statement_fingerprint` matches old markers;
summaries/editorials `verified` only when old marker sha binds to that fingerprint
(legacy/stale untrusted; `no_editorial` skipped). All SQL parameterized. Accelerated by the
group enabling 「获取群内全部消息」(GROUP_MESSAGE_CREATE) so ordinary chatter triggers
capture. Button callbacks carry no nickname → button-only members migrate after first
chat/typed message. See DECISIONS.md「旧数据热迁移补丁」.

Migrated solves keep old global `order_num`. Source `MAX(order_num)` persisted per group in
`group_order_floor`; `record_solve` numbers new solves `max(current_max, floor)+1` (native
always above old max). `worker.run()` calls `ensure_order_floor(db)` before gateway connect
(closes first-message-solves race). Non-migration groups → plain `max+1`.

### Model tags

Provider `model_tag` (e.g. `🐳`) appended (no newline) to every LLM-generated user message at
display time; LLM never sees tags. Records store raw text + `model_tag` field; prompt
builders run stored reply/reason through `llm.strip_model_tags` (trailing configured tags
only). `llm.append_model_tag(text, tag)` is the single display helper (rstrips, no doubling).

## Data Directory

```
{data_dir}/kouhai.db                    # SQLite: source of truth
{data_dir}/run/{app_id}.pid             # instance pidfile
{data_dir}/logs/{app_id}/YYYY-MM-DD.log # worker log (+8 tz)
{data_dir}/statements/<pid>.json        # CF statement MIRROR (crawler reuse; DB authoritative)
{data_dir}/groups/<gid>/next_problem.json # durable READY slot (JSON v2; prefetch_slot table unused)
{data_dir}/render_cache/problem_cards/<key>/ # pre-rendered /np card (card.png+meta.json; key=version+pid+sha+bot)
```
Picker writes disk mirror; main process writes DB catalog (D6/D13). `legacy_migration_map`
records migrated openid→old-QQ pairs; `group_order_floor` the per-group legacy order floor.
Staged `legacy_source.db` lives outside the repo; holds real QQ ids/nicknames, never committed.

## Commands

All handlers `async def handle(ctx: Ctx)`; group-only, **require @bot** (`at_bot` or
`mentions` `is_you=True`; chips/quoted cards auto-@bot). **Public triggers alias-only** (full
name in parentheses is internal, not usable); alias-less use own name. Table = canonical.

| Command | File | Notes |
|---------|------|-------|
| `/submit` (`/sbm`) | submit.py | `smart_model` judge; coordinator; first-blood scoring; offtopic 🙅 no record; 🧐 second judge only if verified editorial cached |
| `/clarify` (`/clrf`) | clarify.py | `general_model` JSON, anti-spoiler; markdown @asker + buttons |
| `/review` (`/rv`) | review.py | Latest solved or quoted older solved card; admission snapshot; `smart_model`; `mentions[]` @others; English editorial model-only; short→markdown, long→image |
| `/newproblem` (`/np`) | newproblem.py | Claims READY slot; unsolved needs exact `--force`; cooldown; image card; commit after delivery |
| `/problem` (`/pb`) | stubs.py | Resend current card; reuses pre-rendered PNG when pid+fingerprint+bot match, else fresh |
| `/tag` | stubs.py | Current problem CF tags |
| `/scoreboard` | stubs.py | Weighted `2^((rating-2000)/300)`; tied ranks; no top5; light-text bubble (`build_scoreboard_html`); migration group adds "数据正在迁移中" note |
| `/status` | stubs.py | Lists ALL in-flight requests plaintext `🔴 …@昵称 的 submit、…` (admission order + active np); idle → 🟢 |
| `/sync` | sync.py | Cross-group pull user×current-pid; exact dedup; tagged synced; correct→score |
| `/clear` | clear.py | Clear caller's current-problem history; 👌 (skip button callback); watermark; args→usage hint |
| `/feedback` (`/fb`) | feedback.py | Mark latest DELIVERED reply (last_interaction cache only); verbatim snapshot→bad_reports; 👌 |
| `/help` | help.py | Auto from registry; themed sections (📚/✍️/🔧) one bubble each, Markdown bullets + bold trigger + muted "short for X"; image card |
| `/range` | range.py | Per-group rating range (smart parsing) |

### Stateful runtime (GroupCoordinator)

`/submit`/`/clarify`/`/review`/`/clear` through a per-group `GroupCoordinator`
(`handlers/cmd/submit.py`); `/np` its own post lock.

- **Admission snapshots under lock**: monotonic seq + target pid snapshot **inside
  `coord.lock`** (submit/clarify/clear → then-current; review → then-latest solved or quoted
  solved). Never re-read target during compute.
- **Parallel compute**: LLM starts immediately, global concurrency 8; same-user doesn't wait.
- **Pending archive**: admitted input saved `result="pending"` + `request_id`; later
  same-user loads pending + completed (exclude own request_id); final upserts that record.
  Superseded/timeout/failure stay context (empty reason/reply).
- **Offtopic no record**: `reaction="123"` → remove pending by request_id, 🙅.
- **Short critical sections**: DB read/modify/write under coordinator lock
  (`run_group_state_update`); LLM never holds it; first-blood wait sleeps **outside**.
- **First-blood serialization**: incorrect/offtopic/failure send immediately. Correct saves
  immediately; earlier unresolved → "做法判对了～" first; only earliest correct after
  earlier settle updates scoreboard + cheer+@winner+reveal + schedules editorial. Later
  correct → `post_solve_correct`, save, no extra message.
- **Same-user replacement**: newer `/submit`/`/clear` same user+problem before terminal drops
  older (cancel compute, `status="stale"`); older pending → `superseded` (or removed by
  /clear). 👀 not terminal.
- **New problem visibility**: commands see old problem until new card delivers; failed
  delivery keeps old.
- **Save-before-deliver**: clarify/review/submit save BEFORE delivering (preserve order).

Status helpers: `get_group_active_requests` (all in-flight, admission order, {nickname,
command, user_openid}), `has_active_problem_request`, `get_newproblem_status`.

### Command flows

- **`/submit`**: snapshot pid/solved under lock → AC backdoor short-circuit → else history,
  👀, judge (`smart_model`). Already-solved at admission → "已经有人解出..." (no judge).
  Offtopic → 🙅 no record. Correct+first → `posts.record_solve` (score/rank, cheer+@winner+
  reveal "本题来自 CF{pid} {name} {rating}✨") then `schedule_post_solve_editorial_followup`
  (background). Incorrect → markdown @asker reason (empty → "reason。再想想？🤔"). Second
  judge only if verified editorial cached; 🧐 emoji-only. `judge_submission()` sends
  `json.dumps({"problem","submission","history"})` (JSON, not plain text); statement joins
  single `\n`.
- **`/clarify`**: `general_model`, `json_object`, `thinking:enabled`+`reasoning_effort`
  unconditional. Output `{"reply","reaction"}`; `123` → 🙅 only. No hints/original identity.
  Grounding = current pid verified summary (`problems.summary_zh`+fingerprint). Reply carries
  [提交][继续询问] buttons.
- **`/review`**: latest solved at admission or quoted older solved card
  (`_resolve_review_pid`: `ref_msg_idx`→`posts.pid_for_ref`, only if solved, else latest).
  Caller history + @mentioned users' same-problem context from `mentions[]` (ignore
  is_you/bot/self/dups; nothing persisted to them). `smart_model` free text; verified
  editorial as **English** (`format_editorial_for_review`, 12k) model-only. Short→markdown
  @asker; long(>400)→image + bare @asker ping.
- **`/sync`**: in G for P pull user's P submissions from other groups
  (`get_cross_group_submissions`, **REPLIED_RESULTS only**), sort by timestamp, **exact
  dedup** (content+result identical → earliest; zero LLM), import tagged `synced` (source
  group hidden). Imported correct + G unsolved → `record_solve(source="synced")`; G solved →
  "只同步记录，不再加分". Idempotent; empty source abort; active same-user/problem reject.
  History image card; 👌.
- **`/feedback`**: scope-local, crosses problem switches, survives `/clear`;
  `last_interaction` cache (DB) ONLY source. Markable = `REPLIED_RESULTS`
  (correct/incorrect/clarify/review/timeout/service_unavailable/no_statement);
  pending/superseded unmarkable. Verbatim snapshot (tags kept) → `bad_reports`. 👌; repeats
  append. Corrupt bad_reports → raise (never reset); corrupt last_interaction → warn+None.
  Classification `shared.record_kind`.
- **`/newproblem`**: post lock; busy → reminder not queued. Cooldown starts after delivery
  (fallback `posted_at`). Claims READY slot (or awaits cold build); previous-problem reveal;
  image card (summary + inline figure passage + samples + optional notes + snake); commits
  `group_posts`+verified summary only after delivery; releases slot, wakes refill. Plain
  unsolved → reject (exact
  `--force`, [查看当前题目] callback). Forced refresh announces skipped problem + cached
  editorial card. No 👀 (prefetched → fast). **No greeting** (lead = summary+tag+snake).
  **Pre-render**: prefetch renders long image once →
  `render_cache/problem_cards/<key>/{card.png,meta.json}` (key=version+pid+fingerprint+bot);
  random snake `post_msg` frozen in meta.json, reused verbatim so image/text match. `/np`
  loads cache → `prerendered_png` through `send_problem_card` (upload media library, markdown
  thumbnail+buttons, skip render); miss renders at post. Best-effort, never blocks READY.
  `/pb` reuses same cache (`_load_problem_card_cache`); miss renders fresh.

### Official tutorials

Offline `tools/tutorial_tools.py crawl|agent|validate`. Runtime `editorial_preparation.py`
(side-effect-free exhaustive search+validate+translate) + `tutorials.py` (DB `editorials`
boundary; verified marker bound to BOTH editorial+statement fingerprints; translation UPDATE
guarded by `source_sha256`). `get_verified_official_editorial(pid)` returns English only when
fingerprints match. Worker maintenance covers current+READY, resumes interrupted, retries
INCOMPLETE with backoff (60s base, 6h cap); only completed exhaustive search writes
`no_editorial`. First-AC delivery via `editorial_followup` (background; warm→immediate; else
await in-flight then deliver if verified; none→silent skip); shutdown cancels tasks. Card
translation (`shared.translate_editorial_to_zh`) omits code, avoids LaTeX, normalizes
symbols; header `📖 {pid} 官方题解` (no 来源 URL). `/review` English(+code) grounding; card
Chinese without code.

### Scheduler

`scheduler/engine.py` multi-group loop (60s) iterates `get_groups()`; per-group job dedup by
date+time; `load_group_config` defaults `contest_check`. `jobs.py:contest_check` →
`shared.check_contests_for_group`: CF contest.list (phase BEFORE/CODING, type CF/ICPC,
exclude unrated/mirror/april fools/kotlin/practice, start ≤ now+24h; **none → silent
return**), sleep 2s, markdown `<qqbot-at-everyone/>` + lines. @all needs admin + 1/group/day.

## Adding a New Command

1. `handlers/cmd/yourcommand.py` with `async def handle(ctx: Ctx)`.
2. `register()` → `registry.register(CommandDef(name, aliases=[], description, usage,
   handler=handle, cooldown))`. `aliases=[]` unless approved. Aliased → ONLY alias usable
   (full name internal); parse `ctx.raw_text` by canonical name; hints use `cmd.trigger`.
3. Mutates state/history → coordinator helpers (`run_group_state_update`); DB read/modify/
   write under same per-group lock.
4. Stateful → publish active-request metadata so `/status` stays correct.
5. Auto-discovered, auto-listed in `/help`. Group-only.

## Pitfalls (correctness invariants)

1. **One shared coordinator per group** (submit/clarify/review/clear) — no ad hoc locks/side
   paths. DB read/modify/write under that lock; LLM + first-blood wait sleep **outside**.
2. **Judge user_msg JSON** `{"problem","submission","history"}`; statement single `\n`.
3. **@ needs markdown** (`<qqbot-at-user id=…/>`, msg_type=2); plain-text @ fake. @all
   text+markdown, admin + 1/group/day. Contest @all + 2s delay.
4. **Length not a hard threshold**: ~2000 text/~3000 markdown empirical; E4 实测 no 40054007
   up to 60000/8000, user-quote read-back of 5000-codepoint message intact (no truncation).
   Bot outbound not echoed; user quoting returns delivered text in `msg_elements[0].content`
   (read-back path). Long-image primary; don't chunk for unproven limit. One content type per
   request (0/2/7 exclusive).
5. **Offtopic = model output** (`reaction="123"`), not local substring blacklists.
6. **Save-before-deliver** (clarify/review/submit) for /feedback consistency.
7. **Editorial first AC only, two messages**: congrats markdown @winner; editorial separate
   background image. Never bundle; never await translation in finalize. Review English
   (+code); card Chinese without code.
8. **High-rating caution**: card rating >2800 → short separate warning (hard, bot may be
   wrong, check official editorial).
9. **DB single connection → connection-wide lock**; bad_reports corrupt → raise (never
   reset); last_interaction corrupt → warn+default.
10. **Every message text matters** — wording deliberate, preserve intent. Nickname fallback
    `openid[:8]`, never "群友".

## Testing

```bash
uv sync --group dev
uv run python -m pytest tests/ -v
```
`conftest.py`: async `db` fixture (tmp SQLite); autouse blocks real
`aiohttp.ClientSession._request` (no live network); autouse resets global runtime/database/
cooldowns/message-dedup. Handler tests inject fake runtime
(`set_runtime(Runtime(db, OfficialSender(FakeRest(), ReplyBudget(db)), browser=None))`) +
monkeypatch LLM. Dispatch events default to @bot mention. Covers db repos, transport,
render, dispatch/registry, coordinator/commands, sync, feedback, cards, model tags, config,
main lifecycle, worker, prefetch/preparation, editorial followup/tutorials, scheduler,
shared, keyboard, legacy_migration.

### Live field probe (owner-led 实测)

`tools/official_probe.py` gathers ground-truth payloads — implement live-shape features only
after calibration, never speculate. E3/E4 calibrated (DECISIONS.md). Subcommands:
- `capture` — read-only gateway listener → `{data_dir}/probe/*.jsonl`. Stop live bot first
  (same app_id conflict).
- `send --group <gid> [--kind text|markdown] [--text …] [--recall]` — one message, prints
  FULL response (incl. `ext_info.ref_idx`), optional recall (≤2min).
- `length --group <gid> [--kind …] [--send] [--recall]` — length ladder, dry-run default.
  Clean ladder ≠ untruncated; verify via user-quote read-back of `msg_elements[0].content`.

## GitHub CI

`.github/workflows/require-markdown-docs.yml` fails PRs to `master` unless ≥1 `*.md`
added/modified. Keep `require-markdown-docs` a required check.

## Startup

```bash
cd ~/kouhai-bot
uv run start      # detached (nohup), pidfile by app_id; refuses if pidfile alive
uv run restart    # stop existing app_id instance, start fresh
uv run stop       # stop the app_id instance
uv run status     # action=/app_id=/occupied=/current_worktree_running=/pid=
```
Logs `{data_dir}/logs/{app_id}/YYYY-MM-DD.log`. Foreground debug:
`uv run python -m kouhai_bot.worker`. prod/test separate app_id (pidfile/DB).

## Design Principles

- **No auto daily post catch-up**: missed is missed.
- **New problem delivery**: long-image card + text fallback; commit state only after delivery.
- **Legacy data**: SQLite fresh; old GROUP data hot-migrated by nickname on access (owner
  reversed original "no migration"); private-judge data dropped. Disk statement mirror for
  crawler reuse only.
- **Preserve UX intent**: when an official limitation blocks a legacy feature, understand WHY
  it existed and build a replacement preserving intent (reactions→tiny emoji,
  merged-forward→long images, top5 removed but cheer+reveal kept). Don't speculate (臆想);
  every present AND absent feature has a reason.