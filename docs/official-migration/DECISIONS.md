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

- Q1 — [已解决] markdown 用原生 `msg_type=2, markdown.content`（template_id/custom_template_id 已废弃）；@ 用 `<qqbot-at-user id="member_openid"/>`、@all 用 `<qqbot-at-everyone/>`。一次请求只能携带一种内容（0/2/7 互斥）；传 markdown 时 content/ark 必须为空。出站长度上限：第三方经验值文本约 2000 / markdown 约 3000 码点，但 E4 实测未触发 40054007（text 至 60000 / markdown 至 8000 均 OK，截断与否未验证）——超长→长图（P2）无论如何安全。被动 msg_id 5min 有效，msg_id+msg_seq 重复发送会失败。
- Q2 — [已解决] 长图用 `file_data`（base64）直传 `/v2/groups/{gid}/files`（`srv_send_msg:false` 仅返回 file_info）；MediaCache 按内容 sha256 缓存 file_info，ttl 到期前 30s 失效，跨群复用（群上传文件仅发群）。大文件可走分片上传，当前题卡 PNG ~100KB 直传足够。
- Q3 — ReplyBudget 主动配额持久化粒度（每群每日计数跨重启）与 20/qpm 节流实现。[P1 已实现：DB 每日计数 + 内存 qpm]
- Q4 — picker 收敛为纯 fetch 后，选题排除集（used ∪ solved ∪ current）的 DB 查询口径。[P1 已实现 get_excluded_pids]
- Q5 — 多群 scheduler_config 迁移（原 JSON 每群 enabled_jobs/rating override → groups 表）。[P1 groups 表已含 enabled_jobs/job_schedules/min_rating/max_rating]
- Q6 — [已解决·整体删除] annotations（离线人工标注工具：首杀导出 bundle + 本地 HTML UI 标「AI 判得对不对」攒评测集）**owner 决定不要了，真删而非 disable**。已删除 src/kouhai_bot/annotations/、tools/annotation_server.py、tools/annotation_web/、tests/test_annotations.py，以及 DB schema 的 annotations 表。无任何运行时消费者（新 submit 早已移除自动导出 hook）。
- Q7 — [已解决·owner 批准] /range 任何群友可设，维持现状（10s 冷却）。不加额外权限。
- Q8 — [已解决·撤销] 不需要群名。group_openid 已唯一确定群；渲染/积分榜只用 bot 署名+用户昵称，无任何处依赖群名。核实（2026-11-18）：官方「获取群基本信息」`GET /v2/groups/{gid}/info` 确实存在（返回 group_name/group_member_num/group_tags 等），但**同样仅白名单可用（11253）**，个人开发者拿不到——故即便想要群名也取不到，撤销决定成立。
- Q9 — [已解决·纠正] **先前「官方拿不到昵称/头像」的结论过时，已推翻。** 官方文档（GROUP_AT_MESSAGE_CREATE / GROUP_MESSAGE_CREATE 页，更新于 2026-11-16）的 author=GroupUser 对象现含 `username`(QQ昵称)/`member_nickname`(群名片)/`avatar`(头像URL)/`member_role`/`member_openid`/`union_openid`；变更记录 20261116 条明确「群聊消息下发事件增加 member_nickname 及头像」「单聊增加发送者昵称、头像」。故**昵称无需白名单接口，直接取群事件 author 即可**。已修：`normalize_event` 解析 nickname（群名片优先→username）、透传 avatar/mentions/message_scene/msg_elements；dispatch 原读 `author.nickname`（错误字段，恒空）改为 member_nickname/username。头像：`author.avatar` 字段已存在，但事件示例尚未含 avatar 实例、社区无实测，**实际下发待抓包验证**；渲染暂仍用首字母色块假头像，验证后再决定是否加载远程头像。「获取群成员信息/列表」REST 接口仍是内邀白名单（11253），个人开发者普遍拿不到——但事件已带昵称，故不影响。owner 看到的「能认人的 bot」两类：协议端(NapCat/LLOneBot/go-cqhttp，完整 QQ 资料) 或官方 bot 取事件 author.username。openid 按应用隔离，拿不到真实 QQ 号。
- Q10 — [延后·需实测，机制已核实可行] 官方**入站** @提及 / 引用回复 现已有结构化字段（2026-11-16 文档）：`mentions[]`(GroupMentionsUser：member_openid/username/member_nickname/scope=all|single/is_you)、`msg_elements[]`(递归 MsgElement，message_type=103 为引用消息)、`message_scene.ext`(msg_idx / ref_msg_idx 引用索引 / auth_token)。normalize_event 已透传这些字段。**2026-11-18 核实关键结论**：① 引用消息正文**随事件下发**——type=103 的 `msg_elements[].content` 即被引消息文本（无需「按 idx 查消息」API，官方根本没有历史消息拉取接口）；② **发消息响应返回 `ext_info.ref_idx`**，故 bot 可把自己发的每张题卡的 ref_idx→pid 存本地映射，用户引用时入站 `ref_msg_idx` 与之匹配即可定位旧题卡（msg_elements 文本兜底）。→ 「review 引用旧题卡定位」**机制上可行**，比原先设想更明确；仍待真实载荷实测校准（尤其小程序卡片等非文本被引类型是否带 content）。出站引用用 `message_reference.message_id`(=REFIDX)。本期 review 仍仅定位最近 solved 题，引用定位延后到实测后。owner 指示：先发真实 @/引用消息抓 WS 载荷原文校准（同时验证 avatar 是否真下发）。PR 暂不合并，测到大体能用再合。

## 官方 API 能力核实（2026-11-18，4 路子 Agent 抓现行文档逐条对照 D1–D16/Q1–Q10）

> 目的：确认「我们以为不能做的真不能做、以为能做的真能做」，校验设计决定的背景知识是否完整。
> 文档站 bot.q.qq.com/wiki（v1.50.0，页面多更新至 2026-11-16）。结论分四类。

### A. 确认「真的不能做」（原假设成立，替代方案有效）
- **群表情回应 reaction**：官方表情表态接口**仅频道**（`PUT /channels/.../reactions/...`，文档明示「目前仅支持频道」；消息类型支持表 群聊❌ 单聊❌ 频道✅）。→ D4/D10 用极短 emoji 文本消息替代 reaction **成立**。
- **拍一拍 poke**：全站 + changelog 无此接口。→ 移除 poke 分支成立。
- **历史消息拉取**：无任何「拉群消息记录/按 idx 查消息」API，只能实时事件 + 本地持久化。→ DB 持久化 + 事件驱动架构成立。
- **群成员信息/列表**：`GET /v2/groups/{gid}/members[/{mid}]` 仍内邀白名单（11253）。返回字段实为 member_openid/username/member_role/bot/joined_at/union_openid（**无 avatar、无 nick**）。→ 不依赖此接口成立。
- **群基本信息**：`GET /v2/groups/{gid}/info` 存在但**亦白名单 11253**（见 Q8）。→ 不需群名成立。
- **真实 QQ 号**：openid 按应用隔离，拿不到真实 QQ 号。→ D1 openid 原生主键成立。

### B. 确认「真的能做」（原假设成立）
- 发群消息 `POST /v2/groups/{gid}/messages`；msg_type 0/2/7 互斥，markdown 时 content/ark 须空。✓ Q1
- 原生 markdown `msg_type=2 + markdown.content`（template_id/custom_template_id 已废弃；2026-04-23 起自定义 markdown 开放所有机器人，无需申请模版）。✓ Q1
- @ 某人 `<qqbot-at-user id="member_openid"/>` 仅 markdown 生效。✓
- 被动回复 群 5min/5 次 per msg_id，msg_id+msg_seq 去重（40054005），**不占主动配额**。✓
- @所有人 `<qqbot-at-everyone/>` 需 bot 管理员 + 每群每天 1 次。✓
- 富媒体上传 `POST /v2/groups/{gid}/files` file_data base64 + srv_send_msg:false → file_info（ttl 秒，0=永久）；发图 msg_type=7 media.file_info；file_info 可复用（群上传仅发群）。✓ Q2
- 撤回 `DELETE /v2/groups/{gid}/messages/{mid}`，自己 ≤2min（40064004），管理员可撤成员消息。✓
- 主动 1000 条/群/日。✓（频控细节见 C）
- 网关外连 WS：op0/1/2/6/7/9/10/11；identify token `"QQBot <token>"` + shard[0,1]；READY 含 session_id；resume op6{token,session_id,seq}。✓
- access_token `POST /app/getAppAccessToken` {appId,clientSecret} → expires_in≤7200，到期前 60s 换新，失败也回 HTTP200 须看 body code。✓
- intents：GROUP_AND_C2C=1<<25、INTERACTION=1<<26、GROUP_MEMBER_EVENT=1<<24。✓
- 全量群消息 GROUP_MESSAGE_CREATE（intent 1<<25）：**无需平台特殊申请**，由**群主**在群内 bot 资料页把「可获取的群聊消息范围」设为「获取群内全部消息」即可；个人认证开发者可加自己任群主/管理员的群（上限 2000）。→ D7/D15 多群惰性服务 + echo 成立（限制在「公开使用」而非全量消息）。
- 用户信息 `GET /v2/users/{openid}/info`（20261116 新增）返回 username/avatar/union_openid/allow_proactive_msg，需好友（40184001）。✓ Q9
- mentions[] / msg_elements / message_scene 结构如 Q10。✓
- keyboard 按钮 + INTERACTION_CREATE 存在（我们未用，能力边界已知）。

### C. 纠正 / 过时（已更新代码与文档）
- **C1 域名统一**：changelog 20260810「所有接口域名统一为 `api.bot.qq.com`」。旧 `api.sgroup.qq.com`/`bots.qq.com` 仍别名可用（探针实测 sgroup 能跑通），但已非推荐。**已改** config.py / rest.py 默认 api_base→`https://api.bot.qq.com`，config.example.yaml / AGENTS.md 同步。token_base 本就 api.bot.qq.com。
- **C2 主动频控是双层**：Bot 维度 **60/qpm**（未认证 30）+ 单群接收侧 **20/qpm**。我们 ReplyBudget 只建模了 per-group 20/qpm（正确），**未建模 bot 全局 60/qpm**——多群总量大时需注意（当前规模无碍）。已在 AGENTS.md 注明。
- **C3 @所有人支持纯文本**：changelog 20261026 起 `<qqbot-at-everyone/>` 在**文本+markdown**均生效（旧 `<@userid>`/`@everyone` 将弃用）。我们 contest_check 用 markdown @all 仍正确，无需改。已注明。
- **C4 长度上限非官方数值**：2000/3000 码点仅见第三方实现，官方文档只给错误码 40054007「消息长度超限」无确切数字。**E4 实测（见下）证伪其为硬错误阈值**：text 至 60000 / markdown 至 8000 码点均返回 OK、不触发 40054007；是否静默截断未验证（无读回路径）。超长转长图的保底策略无论如何安全。
- **C5 沙箱基本废弃**：FAQ「管理员可直接体验测试，无需沙箱；旧管理端已不支持添加沙箱群」，改用「内部体验号码」(20 个)。sandbox.api.sgroup.qq.com 仅存旧缓存页。→ sandbox flag 保留但基本无用，config.example 已注明。
- **C6 GROUP_MEMBER_UPDATE 不存在**：群成员事件仅 ADD/REMOVE（+ 新增 GROUP_JOIN_REQUEST 入群申请）。我们 events.py 只处理 ADD/REMOVE，正确。
- **C7 富媒体上传细节更新**：file_data 现支持二进制直传（旧「暂未支持」过时）≤5M；file_type=4 文件已开放；支持 url 上传（平台转存，可能失败 850026）+ 分片上传；图片软限 20MB/硬限 200MB。我们 base64 直传 ~100KB PNG 不受影响。ttl=0=永久。已注明。

### D. 新能力（本期未用，记录备查 / 未来可选）
- **msg_type=8 图文卡片**（card, type=tuwen，20261028）：原生富卡片，可作长图之外的另一卡片形态。我们仍用高保真长图（刻意选择）。
- **引用回复完整机制**（见 Q10）：入站 msg_elements[].content 带被引正文 + 出站响应 ext_info.ref_idx → 可建 ref_idx→pid 本地映射定位旧题卡；出站引用 message_reference.message_id。→ review 引用定位**可行**，待实测后实现。
- **互动召回消息** is_wakeup=true：用户对话后 30 天内按 4 周期各 1 条召回。
- **素材管理** `qqbot-media://` 图片引用 + force_verify_image_resource（20261021）。
- **event_id 被动回复**：发消息可用 event_id（事件被动）替代 msg_id。
- **union_group_openid / union_openid**：绑定三方应用时跨应用关联群/用户（20261116）。
- **GROUP_JOIN_REQUEST 入群申请事件** + 群成员管理/禁言/入群审批接口（近期新增，多需权限）。
- **keyboard 按钮**：markdown 底部挂按钮（≤5行×5），action 0跳转/1回调/2指令，permission 0指定/1管理员/2所有人；回调 INTERACTION_CREATE 须 3s 内 `PUT /interactions/{id}` 回应（同 id 仅一次）。FAQ(11/11) 称已开放所有机器人。

### E. 实施待办（核实暴露的改进点）
- **E1 入站事件去重 [已实现]**：官方提示消息可能重复推送。dispatch 现按 message_id 全局 LRU（8192）去重，重复事件静默丢弃（在 echo/冷却之前），防重复命令（如双 /submit）。`reset_message_dedup_for_tests` + conftest 复位；新增 2 测试。
- **E2 bot 全局 60/qpm [已实现]**：ReplyBudget 在原 per-group 20/qpm 之上叠加 bot 维度 `ACTIVE_BOT_QPM_MAX=60`（未认证 30 可调常量）全局窗口，扣每日配额前先检查；新增测试。单 worker==单 app_id，内存全局窗口正确。
- **E3 引用定位 + avatar 实测 [完成]**：owner 在两实验群（仅 user+bot，均开「获取群内全部消息」+bot 管理员）发真实普通/@/引用消息，探针 capture 抓到原始载荷；另用 `send --recall` 核实出站响应。**全部坐实，无臆想**：
  - **avatar 真下发且可下载**：`author.avatar` = `https://thirdqq.qlogo.cn/bqqthird?key=…&kti=…&s=640`，curl HTTP 200 image/jpeg 100×100 无需鉴权。→ 渲染接远程头像（Python 预取+缓存→data URI，失败回退字母色块）。
  - **昵称真下发**：`author.member_nickname`/`username`（实测「米老鼠」），`member_role`（owner/admin）。dispatch 已正确取 member_nickname→username。
  - **@ 结构**：`mentions[]` 每项含 `member_openid`/`username`/`member_nickname`/`bot`/`is_you`/`scope`(single|all)/`member_role`；入站 content 里 @ 渲染为 `<@openid>`（**无 `!`**）。→ review 原 `_MENTION_RE=<@!…>` 匹配不到真实格式，改用结构化 `mentions[]`（滤 bot/is_you/self/重复）。
  - **引用结构**：顶层 `message_type=103`；`msg_elements[0].content`=被引正文、`msg_elements[0].msg_idx`=被引消息 REFIDX；`message_scene.ext` 是 **`["ref_msg_idx=REFIDX_…","msg_idx=REFIDX_…","auth_token=…"]` 字符串数组**（非 dict），`ref_msg_idx`==`msg_elements[0].msg_idx`。普通消息 ext 仅 msg_idx+auth_token（无 ref_msg_idx）。
  - **出站 ref_idx**：`send_group_message` 响应 `{id:"ROBOT1.0_…", timestamp, ext_info:{ref_idx:"REFIDX_…"}}`，REFIDX 格式与入站完全一致；recall ≤2min 成功。→ 闭环：bot 发卡存 `ext_info.ref_idx→pid`，用户引用入站 `ref_msg_idx` 匹配定位旧题卡。
  - 非消息事件示例：`type=20/data.type=2001/scene=group`（normalize 返回 None，无害忽略）。
  - 探针新增 `send` 子命令（发一条测试消息 dump 完整响应，`--recall` 撤回）；tokens session 关闭修复。
  - **实现（已落地，625 passed）**：
    - 远程头像：`render/avatars.py` AvatarCache（aiohttp 预取→base64 data URI，TTL+LRU，失败回退字母色块）；`render_card_png` 预解析节点 `avatar_url`；html 有 data URI 渲染 `<img class=avatar>` 否则色块；history 卡（/sync）用户节点接 `ctx.event.avatar`。真实头像长图目视验证通过（米老鼠头像渲染正确，bot 节点色块）。
    - 引用定位：`events.parse_scene_ext`（k=v 字符串数组）+ 归一化 `ref_msg_idx`/`msg_idx`/`quoted_text`；DB `message_refs` 表 + `posts.record_message_ref`/`pid_for_ref`（群 scoped）；sender `_send` 捕获 `ext_info.ref_idx`（`ref_idx_for(mid)`，不改返回类型）；newproblem//problem 发卡后记录 ref→pid；review `_resolve_review_pid` 引用已解决旧题→定位该题，否则回退最近 solved。
    - review @mention 改结构化 `mentions[]`（滤 bot/is_you/self/重复），修真实 `<@openid>`（无 !）格式偏差。
- **E4 长度上限实测 [实测完成，含开放问题]**：owner 备两实验群后自主跑 `length --send --recall`（每条发后即撤回，群保持干净；text/markdown 各 ladder）。**结果推翻原经验值假设**：
  - **text**：1500→60000 码点（纯「测」填充）全部 `OK`（返回 id+ref_idx），**未触发 40054007**。
  - **markdown**：1000→8000 码点全部 `OK`，**未触发 40054007**。
  - 即统一 API（2026-08-10）的 `content`/`markdown.content` 在测试范围内**不以 40054007 拒绝超长**；探针「递增定位 40054007 阈值」的前提被证伪——错误码根本不出现。原 C4/Q1 的「文本约 2000 / markdown 约 3000 码点」第三方经验值**不是硬错误阈值**。
  - **开放问题（需 owner 定夺，不臆想）**：返回 OK 不等于内容完整下发——服务端可能**静默截断**。官方无消息读回 API，bot 自身出站消息也不回推网关，capture 探针抓不到自己发的消息，故**无法自主验证是否截断**。需 owner 在群里目视一条超长消息的尾部是否完整（或接受该不确定性）。
  - **设计含义**：长图卡片（P2 已实现）仍是主路径——它把内容渲染成图片，**完全规避任何文本截断风险**，无论上限真假都安全。纯文本降级（`nodes_to_text`）与 markdown @ 消息保持保守尺寸即可；不必为「~2000/3000 硬上限」做激进分片（该上限未被实测证实为错误阈值）。
  - 探针 `length` 新增 `--recall`（每条 OK 后撤回，保持群干净）；docstring 注明错误码可能不出现、需结合目视判断截断。

### 核实总结
设计决定的背景知识**基本完整且正确**：所有「不能做」的替代方案（reaction→emoji 文本、无 poke、无历史拉取、openid 主键、多群惰性、长图卡片）经核实**全部成立**；「能做」的核心链路（markdown @ / 被动主动配额 / 富媒体 / 撤回 / 网关 / intents / 全量群消息）经核实**全部可行**。唯一实质纠正为 **C1 域名统一**（已修代码默认值）；其余为细节补全（双层频控、@all 文本、长度经验值、沙箱废弃、上传细节）与新能力记录。**最大正向发现**：引用旧题卡定位机制比原设想更明确可行（Q10/E3）。无推翻性错误。

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
- Q9 纠正 + 昵称接线（2026-11-18，子 Agent 上网调查 + 官方文档原文核实）：官方 2026-11-16 起群事件 author 已带 username/member_nickname/avatar，「拿不到昵称/头像」结论过时推翻。修复 dispatch 误读 author.nickname（恒空）→ member_nickname/username；normalize_event 解析 nickname + 透传 avatar/mentions/message_scene/msg_elements；新增 2 测试，全量 612 passed。头像实际下发待实测，渲染暂仍用字母色块。Q10 入站 @/引用已有结构化文档字段（mentions[]/msg_elements type103/ref_msg_idx），透传就绪，仍待实测校准引用定位。
- 官方 API 能力全面核实（2026-11-18，4 路子 Agent 抓现行文档逐条对照 D1–D16/Q1–Q10）：新增「官方 API 能力核实」章节（A 确认不能做 / B 确认能做 / C 纠正过时 / D 新能力 / E 待办）。结论：设计背景知识基本完整正确，所有「不能做」的替代方案与「能做」的核心链路全部核实成立，无推翻性错误。唯一实质纠正 C1 域名统一 api.bot.qq.com（已改 config.py/rest.py 默认值 + example/AGENTS.md）；细节补全 C2 双层频控(bot 60/qpm+群 20/qpm)、C3 @all 支持纯文本、C4 长度上限为经验值、C5 沙箱废弃、C6 无 GROUP_MEMBER_UPDATE、C7 富媒体上传细节。最大正向发现：引用旧题卡定位机制可行（msg_elements[].content 带被引正文 + 出站 ext_info.ref_idx 可建本地映射，Q10/E3）。待办 E1 入站事件去重（官方提示可能重复推送，dispatch 尚未去重）/E2 bot 全局节流/E3 引用+avatar 实测/E4 长度实测。
- E1/E2 落地 + E3/E4 探针（2026-11-18，owner「都去做」）：E1 dispatch 按 message_id 全局 LRU(8192) 入站去重（echo/冷却前静默丢弃重复事件），conftest 复位 + 2 新测试，test_eventlog _event 默认 mid 改自动唯一（真实事件 id 唯一）；E2 ReplyBudget 叠加 bot 全局 ACTIVE_BOT_QPM_MAX=60 窗口（扣每日配额前检查）+ 1 新测试。E3/E4 需 live 实测校准真实载荷，不臆想实现：备 tools/official_probe.py（capture 只读 dump 原始群事件高亮 avatar/mentions/msg_elements/message_scene；length 默认 dry-run、--send 才真发定位 40054007）。引用定位/远程头像加载待 owner 抓真实 @/引用载荷后再实现。全量 615 passed。
- E3 实测校准（2026-11-18，owner 两实验群）：capture 抓普通/@/引用原始载荷 + `send --recall` 核出站响应。坐实 avatar 真下发可下载（thirdqq.qlogo.cn JPEG）、昵称 member_nickname/username 下发、@ 入站 `<@openid>`（无 !，review 正则需改结构化 mentions）、引用顶层 message_type=103 + msg_elements[0].content/msg_idx + scene.ext 字符串数组 ref_msg_idx、出站 ext_info.ref_idx 同格式（闭环可行）。探针加 send 子命令 + tokens 关闭修复。E3 转实现中（远程头像 + review 引用旧题卡定位）。
- E3 实现完成（2026-11-18）：远程头像（AvatarCache 预取 data URI + html img + history 卡接 ctx.event.avatar，真实头像长图目视验证）；引用旧题卡定位（events scene.ext 解析 + message_refs 表 + sender ref_idx 捕获 + newproblem//problem 记录 + review _resolve_review_pid）；review @mention 改结构化 mentions[] 修 `<@openid>` 格式。新增 10 测试，全量 625 passed。
- E4 长度真机校准（2026-11-18，自主跑实验群，length --send --recall 每条发后即撤回）：**证伪原经验值硬上限**——text 1500→60000 码点、markdown 1000→8000 码点全部返回 OK（id+ref_idx），**均未触发 40054007**。统一 API 不以该错误码拒绝测试范围内的超长 content/markdown.content；C4/Q1 的 2000/3000 第三方经验值非硬错误阈值。**开放问题**：返回 OK 不代表完整下发，服务端可能静默截断；无消息读回 API、bot 出站不回推网关，无法自主验证截断，需 owner 目视一条超长消息尾部或接受不确定性。设计含义：长图卡片（P2）完全规避文本截断风险仍为主路径，纯文本降级/markdown @ 保持保守尺寸即可，不必为未证实的硬上限激进分片。探针 length 加 --recall + docstring 注明错误码可能不出现。