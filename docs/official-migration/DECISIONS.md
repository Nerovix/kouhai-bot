# 官方 QQ Bot 移植 — 实施决策与开放问题日志

> 本文件记录实施期间的架构决策、偏离原设计之处、待 owner 确认项。
> 每期 PR 更新（满足 require-markdown-docs CI）。全部完成后统一与 owner 讨论。
> 凭证 / QQ 号 / 群号 / openid 绝不写入本文件或任何 git 跟踪内容（用占位符）。

## 架构决策（owner 授权自决的实现细节）

### D1 — 内部身份模型：str openid 原生
- 全局用户主键 = `user_openid`（群事件里取 `author.member_openid`，已实测跨群稳定，== C2C user_openid）。
- 群主键 = `group_openid`（str）。消息 id = str（`ROBOT1.0_…`）。
- 内部不再用 int QQ 号 / int 群号。handler 签名 `group_id: int` → `group_openid: str`，`user_id: int` → `user_openid: str`。
- 无 legacy 迁移、无 /bind、无 identity_map（owner 定：全新建库）。

### D2 — Strangler 迁移策略
- 新 `official/`（传输）+ `db/`（SQLite 状态）+ `render/`（长图）与旧 `napcat/` + JSON 状态并存。
- 逐阶段把消费者（handlers/worker/scheduler/prefetch/picker）迁到新层；每阶段保持树可导入、测试绿。
- 最后阶段（P5）删除 napcat/、JSON 状态助手、friend_requests、curfew、user_groups、private_judge、jev。
- config.py 先**新增** official 字段，旧字段暂留（标 deprecated）以免破坏未迁移消费者；P5 移除。

### D3 — 数据库：SQLite + aiosqlite
- 单文件 `{data_dir}/kouhai.db`，WAL 模式，外键开启。单进程 bot 绝配。
- schema：users / groups / problems / group_posts / submissions / user_solves(派生) / bad_reports / last_interaction / reply_budget / group_ctx / prefetch_slot / group_used。
- 「群=视图」：每群 scoreboard = `submissions ⋈ group_posts WHERE group_openid=G` 的聚合；全局 user_solves 跨群。
- 事务覆盖原 atomic_write_json 集合 + scoreboard read-modify-write（原仅 coordinator 锁保护）。
- 保留语义：bad_reports 损坏 raise；last_interaction 损坏 warn+默认；request_id upsert；save-before-deliver；clear watermark；seq 首杀。

### D4 — 传输适配器
- `official/` 暴露发送 API（send_group_text / send_group_markdown / send_group_media / send_card …），底层 REST + ReplyBudget。
- 内部消息抽象：文本 / markdown(@) / 长图。OneBot segments 不再使用。
  - 错误判定 / clarify / 恭喜 / review@ → markdown（`<qqbot-at-user id=member_openid>`，仅 markdown 生效）。
  - 卡片级（题卡/help/scoreboard/review长/editorial/history）→ 长图（render/，P2）。
  - 纯确认（👀/👌/feedback/offtopic/314）→ 极短群文本/emoji 消息。
- ReplyBudget：被动优先（群 5min/5 次 per msg_id），窗口外/配额内主动（1000/群/天，20/qpm，持久计数）；失败降级。
- reaction 无官方等价 → 极短 emoji 字符消息（314 无文字；123 offtopic；👌 clear/feedback）。
- 撤回：DELETE messages/{id}（自己 <2min；成员需管理）。
- 富媒体：分片上传 /v2/groups/{gid}/files，file_info ttl 管理。

### D5 — 生命周期 / 实例标识
- 官方外连 WS，无监听端口 → 原按 napcat_ws_port 区分实例的机制失效。
- 实例标识改 `app_id`；pidfile `{data_dir}/run/{app_id}.pid`；start/restart/stop/status 按 pidfile + 进程 cwd 判定。
- 日志目录改 `{data_dir}/logs/{app_id}/YYYY-MM-DD.log`（多群共用一个 worker 日志；群级日志可选）。
- prod/test 双 bot = 双 app_id（各自 pidfile/DB）。

### D6 — picker 子进程 DB 化
- picker.py 原是 state.json/used.json/statements/cf_all 的第二写者。改为 DB-aware（读写 SQLite），或收敛为纯 fetch+parse（stdout JSON），由 bot 主进程统一写 DB。倾向后者：picker 只 fetch/parse/选题，持久化收归主进程。

### D7 — 多群运行时
- dispatch 不再 drop `group_id != current_group`；每群独立视图/配置。群消息天然门禁（发言即成员）。
- worker 维护**每群** prefetch slot + editorial 维护；scheduler 每群跑 jobs。
- echo 每群独立缓冲；需群开「获取群内全部消息」（GROUP_MESSAGE_CREATE，同 intent 1<<25）。
- 私聊整边 disable；C2C 代码停用；WS 只订阅群事件（+ GROUP_MEMBER_EVENT 名册）。

### D8 — P3 handler 接口与运行时持有者
- handler 签名统一为 `async def handle(ctx: Ctx)`；`Ctx` 数据类携带 `db / sender / group_openid / user_openid / message_id / nickname / raw_text / args / event`。registry/dispatch 构建 Ctx。
- `handlers/runtime.py` 全局持有活动 `Database` + `OfficialSender` + `RenderBrowser`（worker 启动时 set_runtime）；coordinator/handler 经 `get_runtime()` 取用。测试可注入 fake。
- 群-only：移除 PRIVATE_SCOPE 整边；coordinator 仅按 group_openid(str) 键控。

### D9 — 状态访问直连 DB repo（不重建 legacy dict）
- coordinator 保留并发结构（PendingRequest/seq/supersede/clear watermark/首杀 seq 解析/save-before-deliver/request_id upsert），但状态读写改调 db repos（record_solve / get_user_problem_submissions / group_scoreboard / save_submission …），不再 load_scoreboard 整 dict。
- 移除打星/user_groups/dynamic CD/settle_dynamic_submit_wait；移除 curfew；移除 top5（_send_scoreboard_success 只留 cheer+@winner+reveal）。
- `_update_scoreboard_for_pid` → `posts.record_solve`（已含首杀/order/dedup/加权）；排名/总分用 `posts.group_scoreboard`。

### D10 — 反馈重设计映射（保留原 UX 意图）
- reaction 无官方等价 → 极短 emoji 字符群消息：👀/睁眼 ack→「👀」；offtopic 123→「😵」(face123 语义)；clear/feedback 👌→「👌」；LLM 失败 268→保留失败文本（无 emoji 必要）。
- 314 二审信号 → 仅 emoji 字符消息（如「🧐」），**无文字**（「二审」是开发术语，原设计仅 reaction 无文字）。
- 错误判定 / clarify → markdown `send_group_at`（@asker，短文本；真实中位 50/85 字）。
- 恭喜 cheer + @winner + reveal → markdown @winner（保留 cheer 文案与本题来源揭示）。
- 卡片级（题卡/help/scoreboard/review长/editorial/history）→ 长图 render/send_forward_card。
- review 短回复 → markdown @asker；长回复 → 长图 + 一句「回复较长，已折叠到卡片里啦 👆」@asker。
- 失败/无题面/用法提示 → 群文本（短）或 markdown @asker。

### D11 — 题面缓存入 DB
- statements 存 `problems.statement_json`（picker 纯 fetch/parse，主进程写 DB，D6）。`load_problem_statement_json(pid)`/`load_problem_statement(pid)` 改读 DB；复用 `problem_content.format_problem_statement_for_llm`（单 \n join，pitfall #3）。
- clarify grounding 改用当前 pid 的 verified summary（problems.summary_zh + 指纹门控），替代 legacy groupctx 末条 assistant（更干净，与私聊路径一致）。

### D12 — 昵称兜底（Q9 未决前的实现）
- 官方群事件 author 仅 member_openid，无昵称。暂用 openid 短码兜底（前 8 位）作显示名；`users.nickname` 列预留，未来可加 /nick 自定义指令（待 owner 定）。scoreboard display-time 刷新群昵称无法移植（无 API），改用 users.nickname 快照。

### D13 — picker / preparation / publication 持久化分工
- picker.py 仍是子进程 CF-fetch 边界，写磁盘 statement 缓存镜像（editorial 爬虫也复用该镜像）；选题排除集改由 DB 驱动：主进程把 `get_excluded_pids`（used ∪ posted ∪ solved）写成临时 `pick_exclude.json`，经 `--exclude-file` 传入；picker 用其作完整排除集，不再读/重置磁盘 used.json。
- preparation（`problem_preparation.prepare_problem`）写 DB `problems` 目录 + `group_used`；publication（`/newproblem`）写 `group_posts` + verified summary。磁盘 statement 缓存仅作爬虫镜像保留。
- 题库耗尽（`ProblemPoolExhausted`）时把排除集放宽为 posted ∪ solved 重试一次（原 used-cycle reset 的等价物）。

### D14 — dispatch 群-only 多群 + 冷却
- `handlers.process_event` 只处理归一化群消息（`message_type=="group"`）；C2C/私聊事件整边忽略（private judge disable）。不再 drop 非 current_group——任何来事件的群都服务。
- 别名归一化为规范指令文本后构建 `Ctx(raw_text=…)`；handler 自行解析 raw_text。
- 每-(group,user,command) 冷却在 dispatch 层用内存表强制（CommandDef.cooldown）；冷却内静默丢弃（避免刷屏）。
- 无官方 reaction/notice/poke/friend-request 等价 → 这些事件分支整体移除。

### D15 — 群服务惰性发现
- worker 维护 `group_openid → GroupServices`（每群一个 NextProblemPrefetcher + editorial 维护 loop）。首次见到某群事件时惰性启动该群服务；scheduler/echo 遍历已发现群集合。
- 群无需预配置名单；发言即服务（群消息天然成员门禁）。

### D16 — 实例生命周期按 app_id（D5 落地）
- main.py start/restart/stop/status 改按 `{data_dir}/run/{app_id}.pid` pidfile + 进程存活/cwd 判定，取代原 napcat_ws_port 监听探测（官方外连 WS 无监听端口）。
- 日志 `{data_dir}/logs/{app_id}/YYYY-MM-DD.log`。config 放宽：official 模式不再硬要求 bot_qq/current_group（保留字段供 legacy，validate_official 把关 app_id/app_secret）。

## 开放问题 / 待 owner 确认（实施中收集，最后统一讨论）

- Q1 — [已解决] markdown 用原生 `msg_type=2, markdown.content`（template_id/custom_template_id 已废弃）；@ 用 `<qqbot-at-user id="member_openid"/>`、@all 用 `<qqbot-at-everyone/>`。一次请求只能携带一种内容（0/2/7 互斥）；传 markdown 时 content/ark 必须为空。出站长度上限：文本约 2000 码点 / markdown 约 3000 码点（超长→长图，P2）。被动 msg_id 5min 有效，msg_id+msg_seq 重复发送会失败。
- Q2 — [已解决] 长图用 `file_data`（base64）直传 `/v2/groups/{gid}/files`（`srv_send_msg:false` 仅返回 file_info）；MediaCache 按内容 sha256 缓存 file_info，ttl 到期前 30s 失效，跨群复用（群上传文件仅发群）。大文件可走分片上传，当前题卡 PNG ~100KB 直传足够。
- Q3 — ReplyBudget 主动配额持久化粒度（每群每日计数跨重启）与 20/qpm 节流实现。[P1 已实现：DB 每日计数 + 内存 qpm]
- Q4 — picker 收敛为纯 fetch 后，选题排除集（used ∪ solved ∪ current）的 DB 查询口径。[P1 已实现 get_excluded_pids]
- Q5 — 多群 scheduler_config 迁移（原 JSON 每群 enabled_jobs/rating override → groups 表）。[P1 groups 表已含 enabled_jobs/job_schedules/min_rating/max_rating]
- Q6 — [已解决·整体删除] annotations（离线人工标注工具：首杀导出 bundle + 本地 HTML UI 标「AI 判得对不对」攒评测集）**owner 决定不要了，真删而非 disable**。已删除 src/kouhai_bot/annotations/、tools/annotation_server.py、tools/annotation_web/、tests/test_annotations.py，以及 DB schema 的 annotations 表。无任何运行时消费者（新 submit 早已移除自动导出 hook）。
- Q7 — [已解决·owner 批准] /range 任何群友可设，维持现状（10s 冷却）。不加额外权限。
- Q8 — [已解决·撤销] 不需要群名。group_openid 已唯一确定群；渲染/积分榜只用 bot 署名+用户昵称，无任何处依赖群名。（官方另有「获取群基本信息」API，若未来要显示群名可再接，本期不需要。）
- Q9 — [已解决] 头像：官方 API **不暴露**用户头像 URL（「获取群成员信息」GET /v2/groups/{gid}/members/{member_openid} 返回 member_openid/username/member_role/bot/joined_at/union_openid，无 avatar 字段）→ 渲染用昵称首字母色块假头像（render/html.py 已实现）。昵称：该接口 `username` 即昵称，但属**白名单/内邀能力**（未开通返回 11253，需联系平台运营申请），默认拿不到。owner 定：忽略真昵称，保持 openid[:8] 短码兜底，不做 /nick；未来若申请到白名单可再接 username。
- Q10 — [延后·需实测] 官方**入站** @提及 / 引用回复 的 content 格式未实测确认。review 的「引用旧题卡定位」依赖引用消息携带 pid，官方引用格式未知 → 本期 review 仅定位最近 solved 题，引用定位延后。@提及解析按尽力而为 `<@!openid>` 正则（review 携带他人上下文）。owner 指示：先把别的 issue 弄好，再发真实 @/引用消息实测校准。PR 暂不合并，测到大体能用再合。

## 实施进度

- P1 地基 [完成]：
  - `db/`：SQLite schema（openid 原生主键，群=视图）+ aiosqlite 连接管理（WAL/显式事务）+ repositories（users/groups、problems/editorials 指纹门控、posts/solves/scoreboard 加权排名、submissions request_id upsert/跨群拉取、feedback 损坏语义、runtime 配额/READY slot）。17 测试。
  - `official/`：tokens（到期前刷新）、rest（gateway/send/media/recall）、events（归一化）、budget（被动优先/主动降级）、sender（text/markdown/@/media/react）、gateway（identify/heartbeat/resume/reconnect）。20 测试。
  - `config.py`：新增 official 字段（app_id/app_secret/api_base/sandbox/bot_name/intents）+ validate_official()；旧字段暂留（strangler）。
  - 全量套件 650 passed。
- P2 长图渲染器 [完成]：
  - `render/browser.py`：Playwright Chromium 单例（headless，一次启动复用），`screenshot_html` 全页 PNG（width=720, device_scale_factor=2，load + 150ms 稳定）。
  - `render/html.py`：高保真手机 QQ 转发卡样式（灰底 #ededed、白气泡圆角、头像首字母色块、昵称灰字、等宽样例块）；`text_to_html`/`pre_block`/escape。
  - `render/forward_card.py`：`nodes_to_text`（先 br→\n 再剥标签再 unescape，纯文本降级）；`render_card_png`；`send_forward_card`（渲染→base64 上传→发图，失败降级文本）。
  - `official/media.py`：MediaCache（内容 sha256 → file_info，ttl-30s 过期）。
  - `official/sender.py`：新增 `send_group_image_bytes`（base64 上传 + 缓存复用）/`send_group_image_url`。
  - 11 测试（含真实 Chromium 集成）；样例题卡 PNG 目视验证通过。
  - 卡片**内容**构建器（题卡/help/scoreboard/review/editorial/history）随 P3 handler 迁移一并接入渲染器。
- 生命周期（main/worker）重写与 handler 迁移强耦合（身份模型 int→openid 渗透所有 handler 签名 + coordinator + dispatch），归入 P3 起点统一做，避免半成品 worker。P1 先交付可测地基。
- P3 命令移植 + 生命周期 [完成]：
  - handler 签名统一 `async def handle(ctx: Ctx)`；`handlers/runtime.py` 全局持有 Database+OfficialSender+RenderBrowser；`handlers/context.py` Ctx + display_name(openid[:8] 兜底)。
  - 13 指令全部移植（submit/clarify/review/newproblem/problem/tag/scoreboard/status/sync/clear/feedback/help/range）+ 6 别名；移除 setproblem/testcd/sbmjev/bad。
  - GroupCoordinator DB 化（admission snapshot/seq 首杀/save-before-deliver/request_id upsert/clear watermark/supersede）；反馈重设计 D10（markdown @ / 长图 / 极短 emoji，🧐 二审无文字，删 top5）。
  - dispatch 群-only 多群 + per-(group,user,cmd) 冷却（D14）；echo/scheduler 多群 openid 化；contest_check 改 official markdown @all。
  - main.py app_id pidfile 生命周期（D16）；worker.py 官方外连 WS + 每群惰性 GroupServices（D15）；config 放宽 bot_qq/current_group。
- P4 /sync 跨群拉取 [完成]：拉取 user×当前 pid 跨群完整历史→全局时间排序→精确去重（content+result，零 LLM）→import tagged synced（不显示来源群）→含 correct 则本群首杀计分；幂等；空源 abort；active 请求拒绝。
- P5 清 NapCat + 文档 + 测试 [完成]：
  - 删除 napcat/、friend_requests、handlers/notice、private_judge、user_groups、curfew、jev、根 context.py；shared.py 清除休眠 legacy 函数（napcat 卡构建/user_groups 榜/jev）。
  - README.md / AGENTS.md / config.example.yaml 全面改写为官方架构（AGENTS.md 保留运营约束与 LLM 推理规则原文）。
  - 测试套件 overhaul：conftest 改 aiohttp 网络隔离 + 全局 runtime 复位；删除/重写全部 legacy 测试，新增官方架构覆盖。全量 **618 passed**。
  - 实施发现（非 bug，记录备查）：
    - prefetch READY slot 实际仍是 JSON（`groups/<gid>/next_problem.json` v2）；DB `prefetch_slot` 表 + repos 已建但**未启用**（死代码）。DB 仅用于 slot 校验读取（rating/current/solves/statement sha）。是否收敛入 DB 留待 owner 定。
    - annotations 导出当时仍是 JSON 离线工具（Q6）——**后于 2026-11-18 按 owner 决定整体删除**（见 Q6），相关边界测试一并移除。
    - cards.py `_bot_reply_text` 对「reply 内嵌 tag 但无 model_tag 字段」的 legacy 记录会渲染 0 次 tag（AGENTS.md 旧文档说渲染 1 次）。全新建库（D1）下所有记录都带 model_tag 字段，该 legacy 情形不可达，故不改源码。

## 变更日志
- P1 启动：建立决策日志；确定 D1–D7 架构。
- P1 完成：DB + official 传输 + config 地基；Q1 解决；新增 Q9（昵称来源）；生命周期归入 P3。
- P2 完成：长图渲染器（Playwright 高保真 QQ 转发卡）+ MediaCache + base64 发图；Q2 解决；样例题卡目视验证。
- P3 完成：13 指令 + Ctx/runtime + GroupCoordinator DB 化 + 反馈重设计 + dispatch/echo/scheduler 多群 + main/worker 官方生命周期；D8–D16 落地。
- P4 完成：/sync 跨群拉取（群=视图物化，精确去重，含通过加分）。
- P5 完成：删除 NapCat/私聊/打星/宵禁/jev 模块；README/AGENTS.md/config.example 改写；测试 overhaul 全量 618 passed；记录 prefetch JSON-slot/annotations Q6 边界/cards legacy-tag 三项实施发现。
- owner 讨论（2026-11-18）：Q7 批准维持现状；Q8 撤销（group_openid 即唯一身份，不需群名）；Q9 定（官方无头像 URL，昵称仅白名单 username 可得→忽略，openid[:8] 兜底+字母色块头像）；Q6 知悉建议搁置（离线标注工具，核心跑通后再迁）；Q10 延后实测。PR 暂不合并，测到大体能用再合。
- annotations 整体删除（2026-11-18，owner 定「不要了，真删」）：移除 annotations 包/annotation_server/annotation_web/test_annotations + DB annotations 表；Q6 关闭。确认个人开发者 bot 默认仅 openid 可得（昵称/角色需白名单，头像不暴露）。