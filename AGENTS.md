# Kouhai Bot — Development Guide

Instructions for AI coding assistants working on this codebase.

## Architecture

```
NapCat (QQ) ──WS──> worker.py
                         │
                         ├── process_event(..., spawn_handlers=True)
                         ├── cmd/*.py  auto-discovered by registry
                         └── scheduler/ background loop (60s tick)
```

## Key Design Decisions

- **Command auto-discovery**: Each command in `handlers/cmd/*.py` calls `register()` at module load. The `registry.discover_commands()` scans with `pkgutil.iter_modules`. Adding a new command = create a `.py` file with a `register()` function.
- **Limited aliases**: Only six short aliases are supported: `/newproblem`→`/np`, `/problem`→`/pb`, `/submit`→`/sbm`, `/review`→`/rv`, `/clarify`→`/clrf`, `/setproblem`→`/sp`. Old aliases such as `/sb`, `/排名`, and Chinese aliases remain unsupported. New commands default to `aliases=[]` unless explicitly approved.
- **Help auto-generation**: `handlers/cmd/help.py` reads `registry.all_commands()` and builds the help text dynamically. Descriptions must match old bridge.py wording.
  `usage` field = args suffix in /help display (e.g. `usage="你的做法"` → `/submit 你的做法`). Group help hides private-only details for `/setproblem` and `/sync` and only briefly mentions private judge; private help lists the private-judge command set.
  Group and private help are both delivered as merged-forward cards, with direct text
  only as fallback.
- **Scheduler current-group config**: `~/.kouhai-bot/scheduler_config.json` stores job list + time overrides for `CURRENT_GROUP`. Jobs are defined in `scheduler/jobs.py`.
- **Command event log**: `eventlog.py` writes append-only JSONL command events by real local date. `achievements.py` reads those events for the 04:00-to-04:00 daily report. `eventlog_backfill.py` and `tools/backfill_command_events.py` can reconstruct recent saved submit/clarify/review events from `scoreboard.json`.
- **Formula VL**: `problems/fetcher.py` handles CF formula images → Qwen-VL → inline LaTeX. Has white-bg preprocessing, hallucination detection, retry.
- **Stale cache detection**: `picker.py:fetch_statement()` detects caches created before VL pipeline via `_vl_processed` flag. Stale caches with images are re-fetched with Qwen-VL. Problems with non-formula images (tex-graphics / diagrams) are skipped.
- **No hermes cron involvement**: The bot runs its own scheduler loop (`scheduler/engine.py`), not hermes cron jobs.
- **Single worker runtime**: `worker.py` keeps the NapCat reverse-WS connection, dispatches commands, and owns the scheduler in one process. There is no SQLite event queue, ingress supervisor, worker hot-swap, or auto-update loop.
- **Friend request auto-approval**: Normal OneBot `post_type="request"` / `request_type="friend"` events are parsed by `napcat/client.py`, routed by `handlers.process_event()`, and approved via `set_friend_add_request`. QQ/NapCat "doubtful" friend requests are not reliably pushed as request events, so `worker.py` also runs `friend_requests.doubt_friend_request_loop()`, which polls `get_doubt_friends_add_request` every 60 seconds and approves with `set_doubt_friends_add_request`. Both paths approve only after the requester is confirmed to be a member of `CURRENT_GROUP`; lookup failure, malformed events, non-friend requests, and non-members are ignored without approving. Requests that were already consumed by another QQ client may not appear in the doubtful-request poll.
- **User groups**: `user_groups.py` — all users default to `default`; `USER_GROUPS` configures
  non-default groups such as `starred`/`打星`, their members, submit delay, and rejection
  message. `submit_delay_sec > 0` enables dynamic per-user submit waits for that group:
  the configured value is the floor, the first solver's next wait doubles, and other
  configured users' waits halve down to the floor. Runtime wait state lives in
  `scoreboard.json.user_group_waits`; real QQ IDs belong only in local config/runtime data.
  `do_daily_post` writes `state.json` `posted_at`; if missing, cooldown falls back
  to matching `daily_msg.json` mtime. Dynamic-wait users who submit before their group
  wait expires are redirected to private judge instead of being judged in the group.
- **Curfew (宵禁)**: `curfew.py` — `/submit` is blocked during a daily quiet window defined
  by `CURFEW_START_HOUR` and `CURFEW_DURATION_HOURS`. Other commands (clarify, review,
  scoreboard, etc.) are unaffected. Curfew wraps past midnight correctly (e.g. start=22,
  duration=6 → 22:00–04:00).
- **LLM fallback**: `llm.py` — providers are tried in list order defined by
  `llm.providers` in `config.yaml`. Each provider is retried internally
  (`llm.max_retries`) before moving to the next. All providers use the
  OpenAI-compatible `/chat/completions` format. Per-task model overrides
  (`judge_model`, `clarify_model`, etc.) are defined per provider. `thinking`
  and `reasoning_effort` are sent unconditionally; unsupported fields are
  silently ignored by upstream APIs.
- **Official CF tutorials**: Scraped editorials live under `{data_dir}/tutorials/{pid}.json`
  (see `tools/scrape_cf_tutorial.py`). Runtime extraction is in `tutorials.py`. On the
  On **new problem** (`do_daily_post` / `/newproblem`), `schedule_prefetch_editorial(pid)`
  starts background translation (using `summary_model`) into `tutorial_translations/` so first AC
  can deliver without waiting. On **first AC**, congrats is sent in `_finalize_submit`, then
  `schedule_post_solve_editorial_followup()` only **delivers** (awaits in-flight prefetch if
  needed). Neither path uses the state scheduler. `/review` uses English editorial in LLM
  context only.

## Configuration

### config.yaml

Runtime config comes from `config.yaml` at the repo root (or set `KOUHAI_CONFIG=/path/to/config.yaml`).
The file is **never committed** — `config.yaml` is in `.gitignore`.
Copy `config.example.yaml` to `config.yaml` and fill in your values.

All providers use the OpenAI-compatible `/chat/completions` endpoint.
``base_url`` should include any version prefix (e.g. `https://api.openai.com/v1`,
`https://api.deepseek.com`); `/chat/completions` is appended automatically.

#### Top-level keys

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `bot_qq` | int | — | Bot's QQ number |
| `napcat_ws_host` | str | `0.0.0.0` | WS listen host — MUST be `0.0.0.0` when NapCat runs in Docker |
| `napcat_ws_port` | int | 8095 | WS listen port |
| `napcat_http_host` | str | `127.0.0.1` | NapCat HTTP API host |
| `napcat_http_port` | int | 3000 | NapCat HTTP API port |
| `current_group` | int | — | QQ group served by the bot (**required**) |
| `data_dir` | str | `~/.kouhai-bot` | Shared data directory |

#### `llm` section

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `llm.max_retries` | int | 2 | Max retries **per provider** before moving to next |
| `llm.retry_base_delay_sec` | float | 1.0 | Exponential backoff base |
| `llm.retry_max_delay_sec` | float | 8.0 | Max backoff cap |
| `llm.judge_timeout_sec` | int | 1200 | Judge LLM timeout |
| `llm.clarify_timeout_sec` | int | 600 | Clarify LLM timeout |
| `llm.review_timeout_sec` | int | 600 | Review LLM timeout |
| `llm.summary_timeout_sec` | int | 120 | Summary + editorial translation timeout |
| `llm.providers` | list | — | **Ordered** fallback provider list (**required, min 1**) |

Each provider in `llm.providers`:

| Key | Default | Description |
|-----|---------|-------------|
| `name` | — | Provider identifier for logging (**required**) |
| `api_key` | — | API key (**required**) |
| `base_url` | `https://api.openai.com/v1` | Base URL for chat completions |
| `model` | — | Default model (**required**) |
| `judge_model` | `model` | Per-task override for `/submit` |
| `clarify_model` | `model` | Per-task override for `/clarify` |
| `review_model` | `model` | Per-task override for `/review` |
| `summary_model` | `model` | Per-task override for `/summary` + editorial translation |
| `reasoning_effort` | — | OpenAI reasoning effort: `minimal`/`low`/`medium`/`high`/`xhigh` |
| `model_tag` | `""` | Short string appended to every LLM-generated user message (judge/clarify/review/summary/editorial); empty means no tag |

#### `qwen` section

| Key | Default | Description |
|-----|---------|-------------|
| `qwen.api_key` | — | Qwen-VL API key |
| `qwen.base_url` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | Qwen-VL base URL |
| `qwen.model` | — | Qwen-VL model name (**required**) |

#### `problem` section

| Key | Default | Description |
|-----|---------|-------------|
| `problem.min_rating` | 2000 | Min CF rating |
| `problem.max_rating` | 3000 | Max CF rating |
| `problem.newproblem_cooldown` | 300 | `/newproblem` cooldown (seconds) |
| `problem.submit_ac_backdoor` | `""` | If non-empty, matching `/submit` skips judge |
| `problem.daily_post_cron` | `"0 12 * * *"` | Cron expression for daily post |

#### `curfew` section

| Key | Default | Description |
|-----|---------|-------------|
| `curfew.start_hour` | 0 | Start hour (0-23, Beijing time) |
| `curfew.duration_hours` | 0 | Duration in hours; 0 disables curfew |

#### `user_groups` section

A list of non-default user groups. Users not listed are in the `default` group.

Each group entry:

| Key | Default | Description |
|-----|---------|-------------|
| `name` | — | Group name (`[A-Za-z0-9_-]+`) (**required**) |
| `display_name` | `name` | Display name (e.g. `打星`) |
| `user_ids` | `[]` | List of QQ user IDs |
| `submit_delay_sec` | 0 | Minimum post-new-problem submit delay; `>0` enables dynamic per-user waits |
| `submit_delay_message` | — | Rejection message; `{wait}` → formatted delay |

### ⚠️ NAPCAT_WS_HOST — Docker gotcha

NapCat runs in a Docker container and connects to the host via `host.docker.internal`
(Docker bridge IP: 172.17.0.1). If the bot binds to `127.0.0.1`, Docker containers
CANNOT connect — they'll get ECONNREFUSED. Always use `0.0.0.0` when NapCat is in Docker.

### NapCat Docker wiring checklist

This bot is a **reverse WebSocket server** plus a **NapCat HTTP API client**:

- `kouhai_bot.napcat.client.NapCatServer` listens on `napcat_ws_host:napcat_ws_port`;
  NapCat must connect to it through a OneBot11 `websocketClients` entry.
- Message sending uses `napcat_http_host:napcat_http_port` and calls NapCat OneBot11
  HTTP actions such as `send_group_msg`, `send_private_msg`,
  `get_group_member_info`, `set_friend_add_request`,
  `get_doubt_friends_add_request`, and `set_doubt_friends_add_request`; NapCat
  must expose an enabled OneBot11 `httpServers` entry on that port.

When NapCat is deployed with Docker Compose, prefer this pattern:

```yaml
services:
  napcat:
    environment:
      ACCOUNT: "<bot_qq>"  # enables fast login after the first successful login
    extra_hosts:
      - "host.docker.internal:host-gateway"
    ports:
      - "3000:3000"  # OneBot HTTP API for bot -> NapCat actions
      - "8095:8095"  # optional NapCat WS server ports for other clients
      - "8096:8096"
      - "8097:8097"
      - "6099:6099"  # WebUI
```

NapCat OneBot11 config should include both sides:

```json
{
  "network": {
    "httpServers": [
      {
        "enable": true,
        "name": "kouhai-http-api",
        "host": "0.0.0.0",
        "port": 3000,
        "enableCors": true,
        "enableWebsocket": false,
        "messagePostFormat": "array",
        "token": "",
        "debug": false
      }
    ],
    "websocketClients": [
      {
        "enable": true,
        "name": "kouhai-bot-reverse-ws",
        "url": "ws://host.docker.internal:<napcat_ws_port>",
        "messagePostFormat": "array",
        "reportSelfMessage": false,
        "reconnectInterval": 5000,
        "token": "",
        "debug": false,
        "heartInterval": 30000
      }
    ]
  }
}
```

Avoid using a bot `napcat_ws_port` that is already published by the NapCat container
as a WebSocket **server** port. If Docker publishes `8095:8095`, `8096:8096`, and
`8097:8097`, then the bot cannot also listen on host `8097`; `uv run restart` will
fail with `OSError: [Errno 98] ... address already in use` or
`Detached bot failed to bind NapCat WS port ...`. Pick a free host port such as
`8098` for `config.yaml` and point NapCat `websocketClients[].url` at that same port.

Troubleshooting symptoms:

- `getaddrinfo ENOTFOUND host.docker.internal` in NapCat logs means the container
  cannot resolve the host alias. Add Compose `extra_hosts:
  ["host.docker.internal:host-gateway"]` and recreate the container.
- Bot logs show `NapCat connected from ...`, but group replies do not send and
  `send_group_msg failed: Server disconnected`: reverse WS is working, but NapCat's
  OneBot HTTP server is missing or not loaded. Enable `httpServers` on
  `napcat_http_port` and restart NapCat.
- NapCat asks for a QR code after every restart even after a successful login:
  set Compose `ACCOUNT: "<bot_qq>"` so the container starts QQ with `-q <bot_qq>`.

Useful checks:

```bash
uv run status
tail -n 80 ~/.kouhai-bot/logs/<group_id>/$(TZ=Asia/Shanghai date +%F).log
docker logs --tail 160 napcat
docker exec napcat getent hosts host.docker.internal
curl -sS -X POST http://127.0.0.1:3000/get_status \
  -H 'Content-Type: application/json' -d '{}'
```

Healthy state:

- `uv run status` reports `occupied=yes` and `current_worktree_running=yes`.
- Bot log contains `NapCat connected from ...`.
- NapCat log contains `HTTP服务: 0.0.0.0:3000` and
  `WebSocket反向服务: ws://host.docker.internal:<napcat_ws_port>`.
- `get_status` returns JSON with `"online": true` and `"good": true`.

### LLM fallback

Providers in `llm.providers` are tried **in list order**. Each provider is retried
up to `llm.max_retries` times internally (exponential backoff). On exhaustion, the
next provider is tried. The first successful response wins.

Common fallback pattern: OpenAI (better quality, less stable) → DeepSeek (stable backup):
```yaml
llm:
  providers:
    - name: openai
      model: "gpt-5.5"
      reasoning_effort: "high"
    - name: deepseek
      model: "deepseek-v4-pro"
```

All providers receive the same request payload. Non-applicable fields (e.g.
`reasoning_effort` on DeepSeek, `thinking` on OpenAI) are silently ignored by the
upstream API. `thinking={"type": "enabled"}` is always sent when the judge handler
requests it — it's an OpenAI-compatible extension that DeepSeek supports.

### Transient failures

Transient LLM failures (timeouts, `aiohttp` client errors, `408/409/429/5xx`,
malformed empty-choice responses) are retried inside `llm.py` with exponential
backoff at the per-provider level. Non-retryable errors (4xx besides 408/409/429)
cause immediate fallback to the next provider.

If all providers are exhausted, the user-facing reply should suggest contacting
an administrator rather than claiming the model is "thinking too long".

### Model tags

Each provider can have a `model_tag` — a short emoji/string (e.g. `🐳`, `֎AI`). When
non-empty, it is appended directly (no newline) to every LLM-generated user message:
`/submit` results (correct and incorrect), `/clarify`, `/review`, daily-problem
summaries, and official editorial translations. The LLM has no knowledge of the tag —
tagging is done entirely by the framework after the response is received.

The tag reflects which provider actually served the request. If the primary provider
succeeded, its tag is used. If a fallback provider was invoked, its tag appears instead.
An empty `model_tag` disables the feature per-provider.

## Data Directory

`~/.kouhai-bot/` — mirrors the old `~/.daily-problem/` structure:
```
groups/<gid>/state.json      # today's problem (+ posted_at unix ts when card was delivered)
groups/<gid>/scoreboard.json # cumulative {solves, user_submissions}
groups/<gid>/daily_msg.json  # forward card payload (msg_id/sample_msg_ids/note_msg_id/snake_msg_id 等, for /problem)
groups/<gid>/problem_summaries.json # saved Chinese problem summaries keyed by pid
groups/<gid>/used.json       # used problem IDs
groups/<gid>/groupctx_*.json # group message context
groups/<gid>/command_events/YYYY-MM-DD.jsonl # structured command event log by real local date
groups/<gid>/problem_ratings.json # cached problem rating by pid for weighted scoreboard totals
private_judge/users/<uid>.json # per-user private judge current problem, history, solved markers, redirect state
annotations/pending/<gid>/<pid>.json # pending human-label bundle for solved problems
annotations/labeled/<gid>/<pid>.json # completed human-label bundle for solved problems
statements/<pid>.json        # cached problem statements
tutorials/<pid>.json         # scraped CF editorials (hint/solution/raw_text/code_blocks)
tutorial_translations/<pid>.txt  # cached Chinese editorial for group cards (per pid)
sessions/                    # per-user session context
scheduler_config.json        # job config for CURRENT_GROUP
```

Current-worktree runtime state is stored under the repository checkout, not under the
shared data directory:

```text
No repository-local runtime queue is used.
```

## Command Handlers

| Command | File | Handler | Lock | Model | Notes |
|---------|------|---------|------|-------|-------|
| `/submit` (`/sbm`) | submit.py | `handle` | ✅ state scheduler | per-provider `judge_model` | Judge solution, save history, serialize only first-blood/scoreboard; configured dynamic-wait group submits redirect to private judge; private AC does not score until synced |
| `/clarify` (`/clrf`) | clarify.py | `handle` | ✅ state scheduler | per-provider `clarify_model` | Clarify problem details (JSON output, anti-spoiler, no original problem identity), using admission-time pid; private uses pid-specific summary |
| `/clear` | clear.py | `handle` | ✅ state scheduler | — | Clear the current user's stored submit/clarify/review history for the admission-time current problem or current private problem |
| `/newproblem` (`/np`) | newproblem.py | `handle` | ✅ post lock | per-provider `summary_model` | Force new problem when solved (or none); unsolved needs exact `/newproblem --force`; samples are forwarded as separate nodes; if statement has `notes`, translate+symbol-normalize and append as a dedicated notes node; commits state only after card delivery succeeds and keeps `daily_msg.json` in sync even on direct-text fallback |
| `/problem` (`/pb`) | stubs.py | `handle_problem` | ❌ | — | Resend current group/private problem via forward card; group path only uses `daily_msg.json` when pid matches `state.json.today`; if solved, add a friendly `/newproblem` hint |
| `/tag` | stubs.py | `handle_tag` | ❌ | — | Show current group/private problem CF tags |
| `/scoreboard` | stubs.py | `handle_scoreboard` | ❌ | — | Cumulative weighted leaderboard; shows the formula at the top, then refreshes latest group nicknames at display time |
| `/help` | help.py | `handle` | ❌ | — | Auto-generated help (forward card) |
| `/review` (`/rv`) | review.py | `handle` | ✅ state scheduler | per-provider `review_model` | Discuss the latest solved group/private problem by default; quoted group problem cards can target older problems |
| `/status` | stubs.py | `handle_status` | ❌ | — | Check whether this group or private judge has active stateful work |
| `/setproblem` (`/sp`) | setproblem.py | `handle` | ❌ | — | Private-only; set current private problem from current group problem, CF pid/link, or `random` |
| `/sync` | sync.py | `handle` | ✅ short group state lock for group writes | — | Sync current group problem history between group and private judge; empty source aborts without overwrite |

### Stateful Command Runtime

Stateful commands (`/submit`, `/clarify`, `/review`, `/clear`) no longer serialize
through one global lock or one per-group FIFO execution queue. Group requests run
through a **per-group state scheduler** implemented in `submit.py`; private judge
requests run through a separate **per-user private coordinator**. `/newproblem` and
`daily_post` use their own per-group post lock and commit current-problem state only
after delivery.

Key rules:

- **Admission-time snapshots**: every stateful request receives a monotonically
  increasing per-coordinator sequence and snapshots its target pid at admission.
  Group `/submit`, `/clarify`, and `/clear` use the then-current group problem;
  private versions use the then-current private problem. Group `/review` uses the
  then-latest solved group problem; private `/review` uses the current private problem
  if solved privately or already solved by the group, otherwise the user's latest
  private solved problem.
- **Parallel compute**: expensive LLM work for `/submit`, `/clarify`, and `/review`
  starts immediately and shares a **global concurrency limit of 8**. Same-user requests
  do not wait for earlier LLM calls to finish.
- **Pending archive context**: when a `/submit`, `/clarify`, or `/review` request is
  admitted for a target problem, its user input is saved immediately to
  group `scoreboard.json.user_submissions` or private
  `private_judge/users/<uid>.json.user_submissions` with `result="pending"` and a
  `request_id`. Later same-user requests load these pending records plus completed
  history; the current request's own pending record is excluded by `request_id`.
  Final handling updates that same record in place. Superseded unanswered `/submit`,
  timeout, and service-failure records therefore remain historical context across
  restarts with empty `reason`/`reply`.
- **Short state critical sections**: JSON read/modify/write endpoints are protected by
  the relevant group/private coordinator async lock. LLM calls never hold this lock.
  `/sync` also uses the group coordinator lock when it writes group `scoreboard.json`.
- **Submit first-blood serialization only**: incorrect/off-topic/failure replies are
  sent as soon as they finish. Correct submits are saved immediately; if earlier
  admission-order submit candidates are still unresolved, the user first gets a short
  “做法判对了～” reply. Only the earliest correct candidate after all earlier candidates
  settle updates `scoreboard.json`, sends rank/top5/reveal, and schedules the official
  tutorial. Later correct candidates log `post_solve_correct` and are saved but do not
  send an extra not-counted message.
- **Same-user submit replacement**: if a user sends another `/submit` or `/clear` for
  the same problem before an earlier `/submit` has reached terminal reply handling, the
  older submit is silently dropped, its local compute task is cancelled, and its command
  event logs `status="stale"`. The 👀/[睁眼] ack does not count as a terminal reply.
  This optimization applies only to older `/submit` requests in that exact same-user,
  same-problem scenario. When superseded by another `/submit`, the older submit's
  persisted pending record is updated to `result="superseded"` so a quick correction or
  addendum can refer to it after a restart, and the received `/submit` still counts as a
  submit attempt in daily achievements for group commands. When superseded by `/clear`,
  it is removed with the rest of that user's current-problem context.
- **New problem visibility**: `/newproblem` / `daily_post` pick and summarize a candidate
  without changing `state.json`. Until the new card is successfully delivered, all
  commands still see the old problem. Failed delivery leaves the old current problem
  intact.
- **New problem serialization/status**: `/newproblem`, `/newproblem --force`, and
  scheduler `daily_post` share a per-group post lock. User-triggered new-problem
  commands are rejected immediately with a busy reminder while another new problem is
  being prepared. `/status` reports this post work as busy while the lock holder is
  building/sending the card.

Status helpers used by `/status`:

- `get_group_lock_status(group_id)` — earliest active stateful request for that group, if any
- `get_private_lock_status(user_id, group_id)` — earliest active private request for that user, if any
- `get_newproblem_status(group_id)` — active `/newproblem`/daily post for that group, if any

### `/clear` — Clear current-problem user context

- Targets the caller's stored `user_submissions` records for the **current problem only**
- Removes that user's saved `/submit`, `/clarify`, and `/review` history for today's pid
- Runs through the state scheduler and records a clear watermark so earlier in-flight
  requests for that user/problem cannot write history after clear
- On group success it reacts to the triggering message with the typed `👌` emoji payload
  (`type=2`, `id=128076`) and sends no extra text. In private chats it cannot use
  message reactions, so it sends a plain text `👌` private message instead of a
  `face` segment.

### Dispatch Pattern

`handlers.process_event()` is the reusable command-dispatch core. The worker calls it with
`spawn_handlers=True`, so the NapCat WS receive loop does not wait for long-running command
handlers. Tests may pass `spawn_handlers=False` to await a handler directly.

Read-only commands (`/problem`, `/tag`, `/scoreboard`, `/help`, `/status`) still do
not enter the state scheduler.
Private dispatch maps DMs to `CURRENT_GROUP`, requires the sender to be a member of that
service group, and only allows the private command whitelist: `/setproblem`, `/problem`,
`/tag`, `/submit`, `/clarify`, `/review`, `/clear`, `/sync`, `/status`, and `/help`.
Private commands do not require @mentions and should not send @ segments back.

Friend request events are not commands and are not logged to command event logs.
`process_event()` handles normal `type="request"` friend requests before message
dispatch. `worker.py` separately polls NapCat's doubtful friend request list because
those requests may only appear through `get_doubt_friends_add_request`. The poller
runs every 60 seconds. Both paths accept only confirmed `CURRENT_GROUP` members and
otherwise return silently; requests already consumed by another QQ client may not be
visible to the poller.

### Command Event Log

`eventlog.py` owns the append-only structured command log under
`groups/<gid>/command_events/YYYY-MM-DD.jsonl`.

- Dispatch writes a `received` event after a group command is recognized. Private
  commands are not written to group command event logs.
- Dispatch writes generic `finished` events (`ok` / `error`) for commands that do not
  publish detailed status themselves.
- Group `/submit`, `/clarify`, and `/review` carry the same event metadata through the group
  state scheduler and write detailed final statuses such as `correct`, `incorrect`,
  `post_solve_correct`, `timeout`, `offtopic`, `stale`, `no_problem`, and
  `no_review_problem`. `post_solve_correct` means a concurrent `/submit` was judged
  correct after an earlier admitted submit had already solved the same problem; it is
  saved to user history but not counted as a new solve and does not send an extra
  user-visible message.
- Group `/sync` writes `status="synced"` or `status="correct"` itself on success. Its
  finished event includes `synced_submit_count`, `synced_clarify_count`,
  `synced_review_count`, and `synced_correct_count` so private-judge records imported
  into the group count in daily achievements.
- Event files are partitioned by the event's real Asia/Shanghai date. Do not store
  04:00 logical-day values in the log; achievement/reporting code should read the
  relevant real-date files and filter by `timestamp`.

`tools/backfill_command_events.py` can backfill recent event logs from existing
`scoreboard.json` `user_submissions`:

```bash
uv run python tools/backfill_command_events.py --days 2
```

Backfilled events use `source="backfill_scoreboard"` and a stable `source_key`, so
the tool is safe to run repeatedly. It only reconstructs saved submit/clarify/review
records; it cannot recover off-topic/no-problem/timeout requests that were never
written to `user_submissions`.

### Daily Achievements

`achievements.py` computes the daily achievement report from command events. It uses
the previous 04:00-to-04:00 window, but that logical window is computed at read time
only. The event log still stores real timestamps and real local-date file partitions.

The built-in scheduler job `daily_achievements` runs at 12:00 and reports:

- earliest/latest `/submit`, including group `/sync` commands that import submit records
- most solved problems (`/submit status="correct"` plus successful scoring `/sync`)
- most `/submit` attempts (received `/submit` commands plus submit records imported by
  `/sync`; superseded `status="stale"` submits still count as attempts)
- most `/review`, including review records imported by `/sync`
- most `/clarify`, including clarify records imported by `/sync`

Existing group scheduler configs that already enable `daily_post` are normalized to
run `daily_achievements` immediately before it. Set
`"disabled_jobs": ["daily_achievements"]` for a group if it should not receive
achievement reports.

### `/submit` — Judge Flow

1. Extract submission text after `/submit`
2. If curfew is active (`curfew_start_hour` → `curfew_start_hour + curfew_duration_hours`),
   reply with a friendly rest message; do not enqueue
3. Enter the group's state scheduler long enough to atomically check dynamic user-group
   submit wait, snapshot today's pid, and snapshot solved state. If the user is still
   inside the effective wait window (`max(submit_delay_sec, saved wait_sec)` after
   `posted_at`) and belongs to a dynamic-wait group, redirect the submit to private
   judge instead of judging/scoring in the group. If the private intro or repeated
   submit text cannot be sent, do not recall the group message, do not judge, and do not
   consume the first-notice marker. Non-dynamic waits still reply with the group's delay
   message and do not enqueue.
4. If not blocked, get a sequence number and enqueue the snapshotted pid/solved state
5. Background compute starts immediately; it loads completed user history plus earlier
   pending user inputs for the same `(group, user, pid)`. The current request's own
   pending archive record is excluded by `request_id`; superseded unanswered `/submit`
   text is visible as `result="superseded"` context.
6. If `SUBMIT_AC_BACKDOOR` is non-empty and the submission contains that string, return a correct verdict before calling the judge LLM
7. Otherwise load user history, react with 👀/[睁眼] ack, and call `judge_submission()`.
   `[睁眼]` is a QQ special emoji reaction (group emoji id `128064` / face id `289`),
   not plain message text. In private judge, send `face` id `289` instead of group
   message reactions or literal `[睁眼]` text. If the judge returns
   `reaction="123"` for spam/off-topic, private judge sends `face` id `123` instead
   of text.
8. Incorrect/off-topic/failure results finalize immediately when compute finishes
9. If the problem was already solved when this `/submit` entered the queue: reply
   "已经有人解出..." without calling the judge.
10. If the problem was unsolved at enqueue time, finalize with the judge result even
   if an earlier admitted submit solves it by now.
11. Save submission record (troll if reaction=123, else correct/incorrect).
12. If correct and earlier submit candidates are still unresolved: first reply
    `做法判对了～`, then wait only for first-blood scoreboard resolution.
13. If correct and no earlier admitted submit solved first: update scoreboard, show rank
    + top5 using weighted score (`2^((rating-2000)/300)`), live nickname lookup, and
    the actual per-problem point gain in the success message (for example `本题 +4 分`);
    equal scores share the same rank, and solve count is display-only → reveal →
    send congrats as normal `send_group_msg`, then
    `schedule_post_solve_editorial_followup()` (does **not** await translation).
14. If correct but an earlier admitted submit solved first: save the record, log
    `post_solve_correct`, and do not update the scoreboard. Do not send an additional
    not-counted message and **do not** send the official tutorial again.
15. If incorrect: reply with judge's reason.

Private `/submit` uses the same judge path and private history, but a correct result
only marks the problem solved in `private_judge/users/<uid>.json`; it does not update
the group scoreboard or daily achievements. The private success message includes the
problem id (for example `做对了 1234A！`) so users can tell which private problem was
accepted. If the private problem is the current group problem, the success message
tells the user they can `/sync` in the service group to score it if the group has not
already solved it.

### `/clarify` — Anti-Spoiler Clarification

LLM `timeout=600` (10 minutes per HTTP attempt; retries in `shared.py` can extend total wait).
Uses `response_format: json_object`. `thinking: enabled` and `reasoning_effort`
are sent unconditionally; unsupported fields are ignored by the upstream API.
Timeout comes from `llm.clarify_timeout_sec`. Output:
`{"reply": "...", "reaction": ""}`.
- `reaction="123"` for spam/off-topic → react only, no text
- Normal: reply text only, must not leak solution hints
- Must not reveal the original problem identity, including problem ID, title, or contest ID

Group `/clarify` loads **Chinese summary from group context** (last assistant message)
for LLM grounding. Private `/clarify` uses `problem_summaries.json` for the selected
pid, so a private CF link/random problem is not paired with the current group summary.
The target pid is snapshotted at admission. Final saving is protected by the state
scheduler lock, but final reply is not ordered behind unrelated same-group requests.

### `/review` — Latest Solved Problem Discussion

- Targets the group's **most recently solved problem at enqueue time**, not necessarily today's current problem
- Only available after the group has solved at least one problem
- Loads the current user's history for that solved problem as review context
- If the review message @mentions other users, appends those users' same-problem
  context as separate blocks, including saved history plus earlier pending/in-flight
  inputs. Ignore @all, the bot itself, the requester, and duplicate mentions. Do not
  persist anything to mentioned users' histories; only the requester receives the
  saved review record.
- Uses per-provider `review_model` (free text, no JSON format). Timeout from
  `llm.review_timeout_sec`. `thinking: enabled` and `reasoning_effort` are sent
  unconditionally; unsupported fields are silently ignored.
- Long replies (>400 chars) → merged-forward card; short replies → @mention inline
- Saves review interaction to user history under that solved problem's `problem` ID
- If the group has never solved a problem yet, it is rejected before running the expensive review LLM call
- The solved-problem target is snapshotted when the request is admitted so `/review` compute can run in parallel with later same-group requests without being retargeted by a newer AC
- Same-user `/review` and `/submit` requests do **not** serialize on earlier LLM calls; later requests see earlier pending user inputs, and completed bot replies appear only after those requests finish
- If `tutorials/{pid}.json` exists and passes extraction, `_compute_review` appends the **English**
  editorial via `format_editorial_for_review()` (truncated to 12k chars). `REVIEW_PROMPT` states
  that this is official editorial **only the model sees** — do not paste it to the user or spoil
  that the group received a tutorial card. Use it to validate the user's approach and explain WAs.

Private `/review` targets the current private problem when it was solved privately or
the group has solved that same current problem. Otherwise it targets the user's latest
private solved problem. This lets private review become available automatically after
someone solves the current group problem, even if the user only set that problem in
private judge.

### Private Judge

Private judge lives in `private_judge.py` plus `/setproblem` and `/sync` command
modules. It is available only in DMs from members of `CURRENT_GROUP`; group-only
commands are rejected in private with a friendly message.

- Private state is per user in `private_judge/users/<uid>.json`: current problem,
  `user_submissions`, solved markers, latest solved pid, and starred-submit redirect
  notification markers.
- Private and group contexts are independent by default. `copy_records()` is used when
  copying history between sides so later writes do not share dict instances.
- `/setproblem` (`/sp`) is private-only. Empty args select the current group problem;
  `CF2234B`, `2234B`, Codeforces problemset/contest links, path fragments such as
  `/contest/2233/problem/F` and `problem/2230/F`, and `random` are supported.
  If private history is empty and group history exists for the selected pid, it copies
  group history into private. If the group has already solved that pid, it marks private
  review as available. It sends a private problem card, preferring the current group's
  cached forward-card payload when the pid is the current group problem. Generated
  private cards must not expose the original CF id, title, contest id, or rating in the
  card title. If an explicit pid/link fails because the statement fetcher detects
  non-formula images (`tex-graphics` diagrams), tell the user the bot has limited
  ability on image-dependent statements and suggest choosing another problem.
- `/problem` in private resends the selected private problem card. `/tag`, `/status`,
  `/clear`, `/submit`, `/clarify`, and `/review` all operate on private state and do
  not emit group @mentions.
- `/sync` copies the **current group problem only** between group and private judge.
  In a group chat it copies private → group; in private it copies group → private.
  If the source side has no relevant records, it aborts and does not overwrite the
  target side. It rejects while there is an active group or private stateful request
  for the same user/problem.
- `/sync` sends the source history as one forward-card-style history card to the target
  chat after copying. The card title is `<群昵称>在当前的历史记录如下：`; each record shows
  only user-visible content as `👤：...` followed by `🤖：...` on the next line, omitting
  internal type/result/reason fields. If the history is too long for one node, chunk it
  like long `/review` output. If the source is empty, it sends only the friendly abort
  message. On successful group sync, react to the triggering message with `👌`
  (`id=128076`) instead of sending a generic success message; private sync sends no
  extra success text.
- A normal user's private correct submit for the current group problem can score through
  group `/sync` if the group has not already solved that pid. Scoring is performed under
  the group coordinator lock, reveals the original problem source, schedules the official
  editorial follow-up, writes the private records into group `scoreboard.json`, and logs
  the imported records for daily achievements.
- Dynamic-wait/starred users redirected to private judge can still `/sync`. While
  their current group problem submit CD is active, only `clarify` records are copied
  in either direction and submit/review/correct records are ignored. Once the CD
  expires, `/sync` behaves like it does for normal users, including private AC scoring
  when the group has not already solved that pid.

### 8.5. Review parallelism depends on enqueue-time pid snapshot
If `/review` is meant to compute in parallel with later same-group requests, do not
re-read `get_latest_solved_problem_id()` during review compute. Snapshot the target
pid when admitting the request and carry that pid through compute + finalize, or a
newer AC can silently retarget an older review request.

### `/newproblem` & Daily Post

`/newproblem` vs `/newproblem --force` when today's problem is unsolved:

- Plain `/newproblem` is rejected with a reminder to use `/newproblem --force` (exact string, space required).
- `/newproblem --force` runs the same locked force-post path as solved `/newproblem`
  (shared cooldown + per-group post lock).
- Plain `/problem` (or `/pb`) still resends the current problem card.

`/newproblem` and scheduler `daily_post` share the same locked post implementation.
The command path first checks the per-group post lock. If another `/newproblem` or
`daily_post` is already preparing a card, the user gets a short "新的题目正在准备中，别急～"
reply and the request is not queued. Otherwise the command holds the lock while checking
cooldown, checking whether plain `/newproblem` is allowed, and building/sending the card.
Cooldown starts only after a new problem card is successfully delivered and committed.
The scheduler path enters via `do_daily_post(group_id, prefix)`, which waits on the same
lock and wraps the same locked implementation:
1. Reveal yesterday's problem via `picker.py reveal`
2. Pick a candidate problem via `picker.py pick-json --with-statement`; this marks used
   problems and caches statements but does **not** write `state.json`
3. Generate Chinese summary via `summarize_problem()` → per-provider `summary_model`,
   timeout from `llm.summary_timeout_sec`
4. Self-send summary text; self-send each sample as an independent node; if statement has
   `notes`, translate it to Chinese and append a dedicated `样例解释` node (with LaTeX/Markdown
   artifacts normalized to readable symbols such as `→`, `≤`, `<`, `>`); then append snake
   image and forward all nodes as one merged card to group
5. If delivery succeeds, commit `state.json` with `posted_at` and save `daily_msg.json`
   for `/problem` to resend. A direct-text fallback after forward-card failure still
   counts as successful delivery and must save `daily_msg.json` with `pid`, `post_msg`,
   `sample_messages`, `notes_message`, and `snake_enabled` so `/problem` can rebuild a
   card later. If delivery fails completely, keep the old current problem.
6. Save the Chinese summary to `problem_summaries.json` keyed by pid for later reuse
7. `schedule_prefetch_editorial(pid)` — background editorial translation (not sent yet)

`/newproblem` and scheduler `daily_post` share a per-group post lock so two new posts
do not overlap. They no longer block `/submit`, `/clarify`, or `/review`; those commands
continue using their admission-time problem snapshot while the new card is being built.

### Official tutorials (scrape + runtime)

**Scraping** (offline, not in the bot hot path):

```bash
# Batch: statements/*.json → tutorials/<pid>.json (quality check + 1 retry)
uv run python tools/tutorial_tools.py crawl \
  --statements-dir statements \
  --tutorials-dir ~/.kouhai-bot/tutorials

# Single problem debug
uv run python tools/scrape_cf_tutorial.py \
  --problem-url "https://codeforces.com/problemset/problem/542/D" \
  --output /tmp/542D.json

# Optional audit of existing tutorials/
uv run python tools/tutorial_tools.py validate --heuristic-only
```

**Runtime** (`src/kouhai_bot/tutorials.py`):

| Function | Purpose |
|----------|---------|
| `get_official_editorial(pid)` | Load JSON, extract body (priority: non-placeholder `solution` → `hint` → cleaned `raw_text`; append `code_blocks` for review source only) |
| `prefetch_editorial_zh(pid)` | Background translate on new problem; `{pid}.no_editorial` if none |
| `get_editorial_zh_for_group(editorial, pid)` | Disk cache or `translate_editorial_to_zh()` |
| `has_cached_editorial_zh(pid)` / `is_no_official_editorial(pid)` | Fast paths for delivery |
| `format_editorial_for_review(editorial)` | English block for `/review` LLM user message |

**Group card translation** (`handlers/shared.py:translate_editorial_to_zh`):

- `task="summary"`, `timeout=600`
- Prompt: translate思路/复杂度 only; **omit all program code**; **avoid LaTeX** — prefer Chinese /
  simple symbols (`≤`, `O(n^2)`); keep LaTeX only when unavoidable for clarity
- Post-process: `normalize_editorial_zh_for_qq()` strips `\( \)`, `$$`, common `\le` etc.
- QQ plain text, no Markdown fences in output
- Re-translate after prompt changes: delete `tutorial_translations/{pid}.txt`

**Prefetch** (on new problem + active-worker startup):

- `do_daily_post` calls `schedule_prefetch_editorial(pid)` **immediately after pick** (in parallel
  with `summarize_problem`, not after it — otherwise first AC waits for summary+translation)
- `worker.py` bootstraps `schedule_prefetch_for_current_group()` on startup (covers worker restart without new post)
- Background `prefetch_editorial_zh(pid)` → `tutorial_translations/{pid}.txt` or `{pid}.no_editorial`
- Runs outside the state scheduler (parallel with submit/review/clarify)

**Delivery** (on first AC, `editorial_followup.py`):

- `schedule_post_solve_editorial_followup()` → if cache warm, deliver immediately (no prefetch wait)
- Otherwise await in-flight prefetch, then deliver; cache miss logs and translates (~30s)
- Has cached zh: self-send chunk(s) → `send_group_forward_msg` (low latency)
- No editorial: silent skip (no group message)
- **Not** part of `GroupCoordinator`; do not `await` inside `_finalize_submit`

## Annotation Tooling

Solved problems are exported for human labeling:

- The first accepted `/submit` for a problem triggers `export_problem_annotation_bundle()`
  from `src/kouhai_bot/annotations/exporter.py`
- Exported bundles live under `~/.kouhai-bot/annotations/pending/`
- Bundles include the statement snapshot, per-round `/submit` verdict data, the exact
  `history_before` seen by the judge, and mutable `human_label` fields
- If a saved Chinese summary exists in `problem_summaries.json`, annotation export
  reuses it directly instead of re-translating

Local HTML labeling UI:

- Server entry: `tools/annotation_server.py`
- Static assets: `tools/annotation_web/`
- Start with:
  `uv run python tools/annotation_server.py --host 127.0.0.1 --port 8788`
- For LAN access, bind to `0.0.0.0`
- On startup, the server backfills missing solved-problem bundles
- Detail requests must return quickly; if a bundle lacks `summary_zh`, the UI opens the
  problem immediately and fetches translation asynchronously through
  `POST /api/annotations/{group_id}/{problem_id}/translate`

## Scheduler

- **Engine**: `scheduler/engine.py` — loop at 60s intervals, runs due jobs per group
  configured by `CURRENT_GROUP`; accepts `stop_event` so the single worker can stop cleanly
- **Jobs** (defined in `scheduler/jobs.py`):
  - `daily_achievements` — 12:00: posts yesterday's 04:00-to-04:00 achievement report.
  - `daily_post` — 12:00: checks `should_post_today`. Solved → new problem. Unsolved → reminder.
  - `contest_check` — 12:01: checks CF API for 24h upcoming contests, @all notification with 2s delay.
- Daily post uses the same `do_daily_post` path as `/newproblem`; the post path has a
  per-group lock and commits `state.json` only after successful delivery.

## Adding a New Command

1. Create `src/kouhai_bot/handlers/cmd/yourcommand.py`
2. Define an `async def handle(group_id, user_id, sender, message_id, raw_text, segments, event)`
3. Define `def register()` that calls `registry.register(CommandDef(...))`
4. Set `aliases=[]` unless a new alias is explicitly approved
5. If the command mutates group state or appends to submission history, route it through
   the state scheduler helpers in `submit.py`; read/modify/write JSON sections must
   use the same per-group scheduler lock
6. If the command is usable in private chat, add it to the private whitelist in
   `handlers/__init__.py`, update private `/help` filtering, and avoid group @mentions
   or message reactions in private replies
7. That's it — auto-discovered, auto-listed in /help

## Adding a Scheduled Job

1. Add an async function in `scheduler/jobs.py`
2. Call `register_job(JobDef(name=..., fn=..., schedule="HH:MM"))`
3. Enable it for `CURRENT_GROUP` in `scheduler_config.json`

## Data Format (critical for compatibility)

### scoreboard.json `solves` entries
```json
{
  "user_id": 123456,       // int (not string!)
  "nickname": "Alice",
  "date": "2026-05-13",    // ISO date string
  "problem": "542D",
  "order": 5               // global solve order
}
```

### scoreboard.json dynamic submit wait state
```json
{
  "user_group_waits": {
    "groups": {
      "starred": {
        "users": {
          "<user_id>": {
            "wait_sec": 1800
          }
        }
      }
    },
    "settled_problems": {
      "542D": 0
    }
  }
}
```

`settled_problems` maps problem id to an integer Unix timestamp and makes wait
settlement idempotent. On successful new-problem
delivery, settle the previous problem before writing the new `state.json`: if the
previous problem has a first solve, double that solver's wait and halve other configured
dynamic-wait users down to their `submit_delay_sec` floor. If no one solved it, do
nothing. If an old in-flight submit is accepted after a newer problem has already been
posted, settle that old pid immediately after writing its first solve. `post_solve_correct`
records do not settle waits.

### submission record format
```json
{
  "timestamp": "2026-05-13T12:00:00+08:00",  // ISO datetime string
  "content": "solution text",
  "result": "correct",      // "correct", "incorrect", "clarify", or "review"
  "reason": "reason text",
  "reply": "reply text",
  "problem": "542D"
}
```

Existing `solves` and submission record fields MUST match the old bridge's format
exactly for backward compatibility with existing `~/.daily-problem` data.
Private judge submission records use the same record shape inside
`private_judge/users/<uid>.json.user_submissions` so group/private history can be copied
without conversion. Do not add private-only fields to individual history records unless
all sync paths intentionally preserve or strip them.

## Pitfalls & Lessons Learned

### 1. Stateful scheduler state must be truly shared
All submit/clarify/review/clear entry points in a group must use the SAME scheduler
state for that group. Do not add an ad hoc lock or side path for one command, or
pending archive updates, clear watermarks, and first-blood resolution will diverge.
Private judge has separate per-user coordinators; do not route private stateful commands
through the group coordinator except for the short group write section inside `/sync`.

### 2. Judge user_msg format is JSON
`judge_submission()` sends `json.dumps({"problem": ..., "submission": ..., "history": ...})`.
The judge prompt was written expecting JSON input. Do NOT change to plain text.

### 3. load_problem_statement joins with \n not \n\n
The problem statement sent to the judge must use single `\n` join between sections.
Using `\n\n` adds extra blank lines that affect judging accuracy.

### 4. @mentions need proper OneBot segments
`build_at(user_id)` + `build_text(" text")` creates a real QQ @mention (with notification).
Using `f"@{nickname}"` in plain text is a fake @mention — no notification.

### 5. Contest notifications need @all
`{"type": "at", "data": {"qq": "all"}}` — the contest check must notify everyone.
Using plain text without @all means nobody sees the notification.

### 6. WS host must be 0.0.0.0 for Docker
See Configuration section above. Binding to `127.0.0.1` breaks Docker NapCat connectivity.

### 7. react_emoji needs int message_id
NapCat's `set_msg_emoji_like` API expects `message_id` as int. Pass string and it
silently fails.

### 8. Friend requests require confirmed service-group membership
Auto-approval must remain fail-closed. Do not call `set_friend_add_request` or
`set_doubt_friends_add_request` unless NapCat confirms the requester is in
`CURRENT_GROUP`; if member lookup fails, ignore the request rather than approving it.

### 9. save_scoreboard needs indent=2
Must pass `json.dump(sb, f, ensure_ascii=False, indent=2)` for backward compatibility
with old scoreboard files.

### 10. save_user_submission keeps full history
Per-user submission history is intentionally unbounded. Records with a `request_id`
update the matching existing record in place; records without one append normally.

### 11. Save Chinese summaries by pid if you need later reuse
The summary shown in the group post is now persisted in `groups/<gid>/problem_summaries.json`.
Annotation export and the HTML labeling UI reuse this first before attempting any new
translation. Do not force synchronous translation on detail-page clicks.

### 12. Daily post uses merged-forward
`do_daily_post()` self-sends summary text, sample nodes, optional translated notes node,
and snake image, then forwards them as one merged card. `daily_msg.json` must persist
all node references (`msg_id`, `sample_msg_ids`, optional `note_msg_id`, `snake_msg_id`)
so `/problem` can resend the same card. If merged-forward fails but direct group text
succeeds, `daily_msg.json` must still persist the current `pid` and rebuild inputs.
`/problem` must ignore stale `daily_msg.json` whose `pid` does not match
`state.json.today`.
Complicated but essential for good UX.

### 13. Dispatch uses create_task
Commands are `asyncio.create_task`'d, not `await`ed. This prevents lightweight
commands from queuing behind locked operations.

### 14. Off-topic submissions are NOT saved
If the judge returns `reaction: "123"`, the submission is marked as troll and
NOT recorded to scoreboard. The save happens AFTER the reaction check. In private
judge this reaction is delivered as QQ `face` id `123`, not the fallback text `😵`.

### 15. Do not add local off-topic blacklists for `/submit` or `/clarify`
Off-topic handling belongs to the model output (`reaction="123"`), not substring
matching in the command entrypoint. Local blacklists are too brittle and can
misfire on normal solution text such as `操作`.

### 16. Nickname fallback is user_id not "群友"
`get_display_name()` / `_nick()` fall back to `str(user_id)` (as a string of digits),
never a generic placeholder like "群友".

### 17. Every message text matters
The old bridge has specific wording for each message. Every difference was caught
in review — validation messages, error messages, reminder text, greeting text.
All must match exactly. See the commit history for the iterative alignment process.

### 18. LaTeX in problem summaries
`summarize_problem()` prompt explicitly forbids LaTeX/markdown. The summary model
sometimes still outputs LaTeX — ping the user if you see this happening.

### 19. Annotation detail pages must not block on translation
The annotation UI can show a placeholder for `summary_zh`, then fetch translation
asynchronously. Clicking a problem in the left pane should open the right pane
immediately; do not make `handle_detail()` await a long translation call.

### 20. Active status tracking prevents confusing UX
The scheduler exposes earliest active request metadata so `/status` can tell users
there is in-flight stateful work. It is not a queue head and does not imply unrelated
commands are blocked.

### 21. Clarify reasoning controls
`/clarify` sends `thinking={"type": "enabled"}` and `reasoning_effort` unconditionally.
The upstream API silently ignores fields it doesn't support. The JSON output format
may occasionally break if extra reasoning content leaks into the response —
`robust_json_parse` handles this.

### 22. `uv run start` / `restart` / `stop` / `status` are selected by NapCat WS port
The `start`, `restart`, `stop`, and `status` entrypoints must use the configured `NAPCAT_WS_PORT` to
identify the target instance. Do not kill by broad process names like
`kouhai_bot.worker`, because production and test instances may run at the same time.
`uv run start` should refuse to launch a second instance if that port already has a
listener. `uv run restart` should stop the existing listener on that port, then
start a fresh detached background instance. `uv run stop` should stop the existing
listener on that port only. `uv run status` should report whether the port is occupied
and whether the listener appears to come from the current worktree.

### 23. Official tutorial: first AC only, two messages
Congrats must stay a direct `send_group_msg` with @mention. Editorial delivery is scheduled
via `editorial_followup` **after** finalize returns (background task). Merged-forward when
editorial exists; nothing sent when not. Never bundle congrats and editorial in one card.
Never `await` editorial translation inside submit finalize — it blocks the whole group queue.

### 24. Tutorial translation cache is per pid
`tutorial_translations/{pid}.txt` is not invalidated when scrape JSON changes. Delete the
cache file to force re-translation after updating prompts or rescraping editorial text.

### 25. Review vs group card use different editorial forms
`/review` gets English source (+ code in extracted text) for internal grounding.
The post-AC card gets Chinese translation without code. Do not send Chinese translation
into review unless product requirements change.

### 26. Private judge sync must not erase on empty source
`/sync` uses the other side as source and overwrites the current side for the current
group problem only. If the source side has no records, abort with a friendly message and
leave the target untouched. This protects users from accidental history loss.

### 27. Private chats use messages, not group affordances
Private command handlers must not send @ segments or call `react_emoji`. Use plain
private messages or face segments (`build_face`) instead. Long private review/history
responses can use private merged-forward cards.

### 28. Private problem cards should not reveal source identity
When `/setproblem` builds a private card for an explicitly selected or random CF
problem, the card title should stay generic. Do not include the original problem id,
title, contest id, or rating there; anti-spoiler clarification also assumes the bot does
not reveal the original problem identity to the user.

### 28.5. High-rating problem cards need a caution
After sending a problem card for a problem with rating greater than 2800, send a short
follow-up warning that the problem is hard, the bot's reasoning may be limited, and the
user should check the official/editorial solution if the bot seems wrong. Keep this as
a separate message after the card, not inside private card titles where source identity
could leak.

### 29. Private judge state writes must be atomic
`private_judge/users/<uid>.json` is user history, not a disposable cache. Write it via a
same-directory temp file and `os.replace`, and log JSON/IO load failures before falling
back to defaults so corruption or permission problems are diagnosable.

## Testing

```bash
uv sync --group dev
uv run python -m pytest tests/ -v
```

Tests mock `data_dir`, group state files, and LLM responses for end-to-end
command testing. The suite covers submit, clarify, review, problem, tag, scoreboard,
newproblem, annotation export, napcat parsing, registry discovery, and
`tutorials.py` extraction/translation (`tests/test_tutorials.py`; submit/review
editorial paths in `tests/test_commands.py`).

## GitHub CI

- `.github/workflows/require-markdown-docs.yml` runs on PRs targeting `master`
- It fails unless the PR adds or modifies at least one `*.md` file
- To make this merge-blocking in GitHub itself, keep `require-markdown-docs` registered as a required status check in branch protection or the repository ruleset

## Startup

```bash
cd ~/kouhai-bot
uv run start
```

`uv run start` launches a detached background instance. stdout/stderr are appended to:

```text
~/.kouhai-bot/logs/<CURRENT_GROUP>/YYYY-MM-DD.log
```

If the configured `NAPCAT_WS_PORT` is already in use, `uv run start` should print a
short status report and leave the existing instance alone.

The detached instance is launched with `nohup`, and its working directory should be the
current repo root so `uv run status` can later tell whether the listener belongs to the
current worktree.

For a foreground debugging session, you can still run:

```bash
cd ~/kouhai-bot
uv run python -m kouhai_bot.worker
```

The worker starts the WS server, discovers commands, registers scheduler jobs,
prefetches the current group's tutorial if needed, and runs the scheduler. No
external cron or process manager needed.

For an already-managed local instance, prefer:

```bash
cd ~/kouhai-bot
uv run restart
```

`uv run restart` selects the target instance by the configured `NAPCAT_WS_PORT`, so
it is the safer choice when production and test bots coexist on the same machine.
It stops the old listener on that port, launches a detached replacement, and prints
a short status summary (`stopped_existing`, `started`, `pid`, `log`, etc.).

`uv run stop` should print whether it found and stopped a listener on that port.

`uv run status` should print a short machine-readable summary like:

```text
action=status
port=<selected-ws-port>
occupied=yes|no
current_worktree_running=yes|no|unknown
pids=...
```

Interpretation:
- `occupied=yes` means the configured WS port is currently listening.
- `current_worktree_running=yes` means at least one listener on that port has `cwd`
  equal to this repo worktree root.
- `current_worktree_running=no` means the port is occupied, but the listener appears
  to belong to another worktree or process.
- `current_worktree_running=unknown` means the port is occupied, but process cwd
  inspection was not reliable enough to decide.

## Deployment & Groups

The served group is configured via `config.yaml`:
- `current_group` — the single QQ group where commands work and scheduled posts are sent

Start with a test group first; switch `current_group` to the production group after verification.

Production and test bot instances must use separate NapCat reverse WebSocket ports.
When starting or restarting a bot, use the production port for the production bot and
the test port for the test bot. If an instruction says only "start" or "restart" and
the intended instance/port is unclear, ask the developer which bot profile and port to
use before taking action. Do not use broad process-name restarts when multiple bot
instances are running; stop or start only the selected instance.

**Start / Restart / Stop / Status procedure:**
```bash
cd ~/kouhai-bot
uv run start
```

```bash
cd ~/kouhai-bot
uv run restart
```

```bash
cd ~/kouhai-bot
uv run stop
```

```bash
cd ~/kouhai-bot
uv run status
```

**Verify connection:**
```bash
ss -tnp | grep <selected-ws-port>              # should show ESTAB from the NapCat container
sudo docker logs napcat --tail 5 | grep <selected-ws-port>  # should NOT show errors for the selected port
```

## Data Migration

Old data lives at `~/.daily-problem/`. New data at `~/.kouhai-bot/`.
The old directory is preserved as backup — never delete it.

To migrate a group's data:
```bash
cp -r ~/.daily-problem/groups/<gid> ~/.kouhai-bot/groups/<gid>
```

The picker subprocess is the in-repository `src/kouhai_bot/problems/picker.py`.
It uses `~/.kouhai-bot` as its default data directory, with group-specific state
selected by the `--group` flag.

## Design Decisions

- **No catch_up_daily_post**: The old bridge had a startup catch-up that posted if
  started after 12:00 with no problem. This was intentionally REMOVED — it makes
  workflow unpredictable. Missed is missed; don't auto-recover.
- **Daily post delivery**: Must use merged-forward (self-send text + snake image →
  forward card + daily_msg.json). Plain-text direct send is fallback-only; if it
  succeeds, it still needs current-problem `daily_msg.json` so `/problem` never resends
  stale cached cards.
- **Old bridge.py behavior is the ground truth**: All behavior, message text, data
  formats, and edge cases must match the legacy bridge implementation. When in doubt,
  compare against the old code if it is available in your local environment.

## Review Checklist

When making changes, verify against old bridge.py:

1. **Data format**: scoreboard entry fields (user_id int, date, order), submission
   record fields (timestamp ISO, result string, clarify/review result types), save_scoreboard indent=2
2. **@mentions**: Use `build_at` + `build_text` segments for real QQ mentions
3. **Message text**: Every validation/error/success message verbatim from old bridge
4. **Scheduler lock sharing**: submit/clarify/review/clear use the same per-group
   scheduler; JSON read/modify/write sections use the per-group scheduler lock
5. **Judge format**: user_msg is JSON `{"problem","submission","history"}`
6. **Contest**: @all notification, 2s delay
7. **Dispatch**: create_task (not await), skip own messages, extract_text with @mentions
8. **WS**: bind 0.0.0.0 for Docker, max_size=2**26, ping_interval=30, ping_timeout=10
9. **History persistence**: save_user_submission does not trim; records with request_id upsert
10. **Nickname fallback**: `card or nick or str(user_id)` — never "群友"
11. **Status visibility**: new stateful commands MUST publish active request metadata
    so `/status` still reports in-flight work correctly
12. **Official tutorial**: first AC only; congrats + separate forward card; translation
    omits code; review uses English editorial in LLM context only, not user-visible spoiler
13. **Private judge**: private commands are service-group-member only, private history is
    independent, `/sync` aborts on empty source, and private AC never scores unless a
    normal user syncs the current group problem back to the group before it is solved
