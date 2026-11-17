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
- schema：users / groups / problems / group_posts / submissions / user_solves(派生) / bad_reports / last_interaction / reply_budget / group_ctx / prefetch_slot / annotations。
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

## 开放问题 / 待 owner 确认（实施中收集，最后统一讨论）

- Q1 — [已解决] markdown 用原生 `msg_type=2, markdown.content`（template_id/custom_template_id 已废弃）；@ 用 `<qqbot-at-user id="member_openid"/>`、@all 用 `<qqbot-at-everyone/>`。一次请求只能携带一种内容（0/2/7 互斥）；传 markdown 时 content/ark 必须为空。出站长度上限：文本约 2000 码点 / markdown 约 3000 码点（超长→长图，P2）。被动 msg_id 5min 有效，msg_id+msg_seq 重复发送会失败。
- Q2 — 长图分片上传的 file_info ttl 与复用策略（题卡重发是否复用已上传 media）。
- Q3 — ReplyBudget 主动配额持久化粒度（每群每日计数跨重启）与 20/qpm 节流实现。[P1 已实现：DB 每日计数 + 内存 qpm]
- Q4 — picker 收敛为纯 fetch 后，选题排除集（used ∪ solved ∪ current）的 DB 查询口径。[P1 已实现 get_excluded_pids]
- Q5 — 多群 scheduler_config 迁移（原 JSON 每群 enabled_jobs/rating override → groups 表）。[P1 groups 表已含 enabled_jobs/job_schedules/min_rating/max_rating]
- Q6 — annotations 导出/UI 改 openid + DB 的范围（本期是否含标注工具迁移）。
- Q7 — /range 指令的权限（任何群友可设）是否需要防滥用冷却。
- Q8 — 群名/群头像获取（官方无群信息 API 白名单）：显示用群名从哪来（事件无群名，可能需 owner 配置或省略）。
- Q9 — 用户昵称来源：官方群事件 author 只有 member_openid，无 nickname；无 get_group_member_list 等价 API。scoreboard 显示昵称从何而来？（候选：事件无→用 openid 短码兜底 / owner 配置 / 用户自定义昵称指令）。原设计 display-time 刷新群昵称无法直接移植。

## 实施进度

- P1 地基 [完成]：
  - `db/`：SQLite schema（openid 原生主键，群=视图）+ aiosqlite 连接管理（WAL/显式事务）+ repositories（users/groups、problems/editorials 指纹门控、posts/solves/scoreboard 加权排名、submissions request_id upsert/跨群拉取、feedback 损坏语义、runtime 配额/READY slot）。17 测试。
  - `official/`：tokens（到期前刷新）、rest（gateway/send/media/recall）、events（归一化）、budget（被动优先/主动降级）、sender（text/markdown/@/media/react）、gateway（identify/heartbeat/resume/reconnect）。20 测试。
  - `config.py`：新增 official 字段（app_id/app_secret/api_base/sandbox/bot_name/intents）+ validate_official()；旧字段暂留（strangler）。
  - 全量套件 650 passed。
- 生命周期（main/worker）重写与 handler 迁移强耦合（身份模型 int→openid 渗透所有 handler 签名 + coordinator + dispatch），归入 P3 起点统一做，避免半成品 worker。P1 先交付可测地基。

## 变更日志
- P1 启动：建立决策日志；确定 D1–D7 架构。
- P1 完成：DB + official 传输 + config 地基；Q1 解决；新增 Q9（昵称来源）；生命周期归入 P3。