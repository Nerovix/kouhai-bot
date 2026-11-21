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
- 需群开「获取群内全部消息」（GROUP_MESSAGE_CREATE，同 intent 1<<25）供热迁移在每条群消息上捕获身份。（原 echo 每群复读缓冲已砍，见 2026-11-20「砍复读 + 命令必须 @bot」决策。）
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
- reaction 无官方等价 → 极短 emoji 字符群消息：👀/睁眼 ack→「👀」；offtopic 123→「🙅」(face123 语义)；clear/feedback 👌→「👌」；LLM 失败 268→保留失败文本（无 emoji 必要）。
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
- worker 维护 `group_openid → GroupServices`（每群一个 NextProblemPrefetcher + editorial 维护 loop）。首次见到某群事件时惰性启动该群服务；scheduler 遍历已发现群集合。
- 群无需预配置名单；发言即服务（群消息天然成员门禁）。

### D16 — 实例生命周期按 app_id（D5 落地）
- main.py start/restart/stop/status 改按 `{data_dir}/run/{app_id}.pid` pidfile + 进程存活/cwd 判定，取代原 napcat_ws_port 监听探测（官方外连 WS 无监听端口）。
- 日志 `{data_dir}/logs/{app_id}/YYYY-MM-DD.log`。config 放宽：official 模式不再硬要求 bot_qq/current_group（保留字段供 legacy，validate_official 把关 app_id/app_secret）。

### D17 — 无参按钮用 type=1 回调，且与直发命令完全等价
- owner「把 np，clear 的按钮接成 type1 回调更好一点，因为这两个指令没有参数」+「我要求那个按钮回调过去跟直接发送的效果一模一样，比如 np 也要有基本的冷却」。
- 再来一题(`/np`)、清空上下文(`/clear`)、查看当前题目(`/pb`) 由 type=2 enter=true 自动发送按钮改为 **type=1 回调**（`callback_button`）：点击不产生可见聊天消息，而是下发 `INTERACTION_CREATE`(type=11)。
- **等价性靠共享 dispatch 保证**（而非另写 handler）：`handlers._interaction_to_message` 先 `PUT /interactions/{id} {"code":0}` 止 loading（`rest.respond_interaction`/`sender.ack_interaction`，type=11/12 必须 ack 否则客户端转到超时；code=0 仅表示「收到」），再合成等价群命令消息（`at_bot=True`、`message_id=""`、`content=button_data`）走**同一个 `process_event`**——冷却、`/np` 未解出需 `--force` 摩擦、占锁 busy 提示与手打逐字一致；回复走主动配额（回调无入站 msg_id）。按 `interaction_id` 去重（复用 message_id LRU）。非 type=11 / 非 group scene / 非 slash button_data / 缺身份 → 忽略且不 ack。命令自身的文本回复承载成败，与手打无异。
- 能力依据：INTERACTION intent (1<<26) 已订阅；`data.resolved.button_data`/`button_id` 为按钮 action.data/id。`events._interaction` 提取 interaction_type/scene/button_data/button_id（resolved 非 dict 时防御置空）。worker `_on_event` 对 interaction 也 `_ensure_group`。

## 开放问题 / 待 owner 确认（实施中收集，最后统一讨论）

- Q1 — [已解决] markdown 用原生 `msg_type=2, markdown.content`（template_id/custom_template_id 已废弃）；@ 用 `<qqbot-at-user id="member_openid"/>`、@all 用 `<qqbot-at-everyone/>`。一次请求只能携带一种内容（0/2/7 互斥）；传 markdown 时 content/ark 必须为空。出站长度上限：第三方经验值文本约 2000 / markdown 约 3000 码点，但 E4 实测未触发 40054007（text 至 60000 / markdown 至 8000 均 OK），且引用法验证 5000 码点完整下发无截断——超长→长图（P2）无论如何安全。被动 msg_id 5min 有效，msg_id+msg_seq 重复发送会失败。
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
- 全量群消息 GROUP_MESSAGE_CREATE（intent 1<<25）：**无需平台特殊申请**，由**群主**在群内 bot 资料页把「可获取的群聊消息范围」设为「获取群内全部消息」即可；个人认证开发者可加自己任群主/管理员的群（上限 2000）。→ D7/D15 多群惰性服务 + 热迁移身份捕获成立（限制在「公开使用」而非全量消息；原 echo 复读已砍）。
- 用户信息 `GET /v2/users/{openid}/info`（20261116 新增）返回 username/avatar/union_openid/allow_proactive_msg，需好友（40184001）。✓ Q9
- mentions[] / msg_elements / message_scene 结构如 Q10。✓
- keyboard 按钮 + INTERACTION_CREATE 存在（我们未用，能力边界已知）。

### C. 纠正 / 过时（已更新代码与文档）
- **C1 域名统一**：changelog 20260810「所有接口域名统一为 `api.bot.qq.com`」。旧 `api.sgroup.qq.com`/`bots.qq.com` 仍别名可用（探针实测 sgroup 能跑通），但已非推荐。**已改** config.py / rest.py 默认 api_base→`https://api.bot.qq.com`，config.example.yaml / AGENTS.md 同步。token_base 本就 api.bot.qq.com。
- **C2 主动频控是双层**：Bot 维度 **60/qpm**（未认证 30）+ 单群接收侧 **20/qpm**。我们 ReplyBudget 只建模了 per-group 20/qpm（正确），**未建模 bot 全局 60/qpm**——多群总量大时需注意（当前规模无碍）。已在 AGENTS.md 注明。
- **C3 @所有人支持纯文本**：changelog 20261026 起 `<qqbot-at-everyone/>` 在**文本+markdown**均生效（旧 `<@userid>`/`@everyone` 将弃用）。我们 contest_check 用 markdown @all 仍正确，无需改。已注明。
- **C4 长度上限非官方数值**：2000/3000 码点仅见第三方实现，官方文档只给错误码 40054007「消息长度超限」无确切数字。**E4 实测（见下）证伪其为硬错误阈值**：text 至 60000 / markdown 至 8000 码点均返回 OK、不触发 40054007；并用 owner 引用法验证 5000 码点完整下发、**无静默截断**。超长转长图的保底策略无论如何安全。
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
- **E4 长度上限实测 [实测完成，截断已验证]**：owner 备两实验群后自主跑 `length --send --recall`（每条发后即撤回，群保持干净；text/markdown 各 ladder）。**结果推翻原经验值假设**：
  - **text**：1500→60000 码点（纯「测」填充）全部 `OK`（返回 id+ref_idx），**未触发 40054007**。
  - **markdown**：1000→8000 码点全部 `OK`，**未触发 40054007**。
  - 即统一 API（2026-08-10）的 `content`/`markdown.content` 在测试范围内**不以 40054007 拒绝超长**；探针「递增定位 40054007 阈值」的前提被证伪——错误码根本不出现。原 C4/Q1 的「文本约 2000 / markdown 约 3000 码点」第三方经验值**不是硬错误阈值**。
  - **截断已验证（owner 引用法，2026-11-18）**：bot 发一条 **5000 码点**长消息（每 1000 字嵌 `【N000】` 标记，结尾 `【5000】`，不撤回），owner 长按引用它。capture 抓到引用事件 `msg_elements[0].content` = **完整 5000 码点**，`【1000】…【5000】` 全部在、结尾正是 `【5000】`。→ **服务端未静默截断**（至少到 5000，2.5× 原经验值），消息完整存储且可被引用读回。原「OK 不代表完整下发」的开放问题**就此关闭**。方法学：bot 收不到自身出站（实测 80+ 条出站零回推），但**用户引用 bot 消息时入站 `msg_elements[0].content` 携带该消息实际下发的完整正文**——这是无读回 API 下验证出站内容的可行路径。附带坐实：引用 bot 消息时 QQ 客户端自动插入 @bot（`mentions=1`，bot 的 member_openid），dispatch 需把这种「引用+@bot」当普通入站处理。
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

## 代码自审修复（2026-11-18，端到端验证前）

> owner 指示「先自己 review 一波再端到端验证」。三路子 Agent 分审并发/传输/数据格式/安全，
> 主进程逐条核实后修复**确证 bug**；设计取舍/被证伪前提/低危项延后到统一讨论（见下）。
> 全量 **629 passed**（+4 新测试）。以下均已落地并补测。

### 并发 / 协调器
- **DB 单连接事务串行化（关键）**：单条共享 aiosqlite 连接上 `BEGIN IMMEDIATE` 事务此前可被其它协程的语句插入而错乱。`Database` 新增连接级 `asyncio.Lock`，`execute/executemany/fetch_one/fetch_all/fetch_val/transaction` 全部串行化；`fetch_val` 内联 `conn.execute`（不再调 `fetch_one`，避免非重入锁自死锁）。核实仅 4 处 transaction（posts×2 / runtime×2）内部均用裸 conn，无重入。
- **首杀计分自旋不再持锁 sleep**：`_resolve_submit_scores` 的 `asyncio.sleep(0.1)` 移出 `coord.lock`（原在锁内 sleep 会阻塞同群所有状态临界区）。
- **admission 快照入锁**：clarify/clear/review 的目标 pid 快照改在 `coord.lock` 内读取后再 `_enqueue_locked`（原在锁外读，存在被新 AC/新题重定向的竞态）；review 的 pid_resolver 在锁内调用。

### 数据完整性
- **/sync 跨群导入只拉已回复记录**：`get_cross_group_submissions` 过滤 `REPLIED_RESULTS`（排除 pending/superseded），避免把对方未决/被取代的占位记录重复物化进本群。
- **message_refs 跨群主键碰撞**：ref_idx 是每群索引，PK 改复合 `(group_openid, ref_idx)`；`record_message_ref` 用 `ON CONFLICT(group_openid, ref_idx)` upsert；`pid_for_ref` 群 scoped。fresh-start schema 无迁移。
- **editorial 译文绑定源指纹**：`set_editorial_translation` 增 `source_sha256` 守卫（UPDATE 加 `AND source_sha256=?`），`_commit_prepared_editorial` 一次算源指纹同时绑源+译文，防重爬源后旧译文误标 verified。
- **/newproblem 冷却重启绕过**：内存冷却表缺失时回退持久化 `posted_at`（`state.current_posted_at`），重启不再绕过冷却。

### 传输 / dispatch
- **dispatch 剥离前导 `<@bot>`**：官方引用 bot 卡片会自动插入 @bot（入站 `<@openid>` 无 `!`，且走 GROUP_MESSAGE_CREATE）。dispatch 先剥前导 mention 再归一化指令，任何 `/` 指令均响应（无需 @bot，与「服务 every group」一致）。新增 dispatch 测试锁定。
- **echo 兼容无 `!` @ 格式**：`_LEADING_AT_RE` 容忍 `<@openid>`/`<@!openid>`；复读输出剥前导 mention（避免复读时再 @bot）。
- **review @mention 保真**：保留原始 openid（不再 `.upper()`），跳过 `scope=="all"`，self 用原始 `ctx.user_openid`。
- **/clear 误成功 ack**：正则不匹配（带多余参数）发改用提示而非假 👌。
- **REST 网络异常统一降级**：`aiohttp.ClientError`/`asyncio.TimeoutError` 包成 `OfficialApiError(0,…)`，sender 各路径一致降级；session 加 `total=60s` 超时。
- **send_forward_card 永不 raise**：渲染/发图/文本降级三段各自 try/except，任一失败返回 None 而非抛断命令。
- **头像负缓存 + 并行**：AvatarCache 失败 URL 60s 负缓存（防反复重试拖慢渲染），`asyncio.gather` 并行预取，网络请求在锁外。
- **Playwright driver 泄漏**：launch 失败 `pw.stop()`；close 用 try/finally 确保 `_pw.stop()`。
- **picker stderr 全量分类**：题库耗尽标记打在 stderr 末尾，原 head 截断会漏判→耗尽放宽永不触发；改对完整 stderr 分类。

### 生命周期
- **C#6 worker 停机取消 editorial 后台任务**：`stop()` 在关闭 browser/DB/REST 前 `await cancel_background_tasks()`，长爬取+翻译不会_outlive 其写入资源。
- **C#9 worker 启动恢复已知群服务**：`_seed_groups()` 从 `SELECT DISTINCT group_openid FROM group_posts` 恢复每群 prefetch+editorial 维护（原重启后须等下一条入站消息才启动，READY slot/当前题解永不刷新）。
- **C#7 pidfile 身份核验**：`_pid_is_worker` 读 `/proc/<pid>/cmdline` 含 `kouhai_bot.worker` 才视为本 bot（cmdline 不可读→fail-safe 非本 bot），防陈旧 pidfile 指向被 OS 复用的无辜进程时 stop/restart 误杀、start 误拒。
- **C#8 停机等待 10→40s**：worker 优雅 drain 预算 30s，原 main 等 10s 即 SIGKILL 会打断 drain；放宽到 40s。
- **删除死常量**：sender `TEXT_MAX_CHARS=2000`/`MARKDOWN_MAX_CHARS=3000` 无任何引用，且 E4 已证伪其为硬上限——移除（含误导注释）。

### 延后到统一讨论（非确证 bug / 被证伪前提 / 低危）
- A#3 /sync 去重键 `(content,result)`：属已封「精确去重」设计决定，非 bug。
- C#2 文本降级长度前提：E4 证伪硬上限，非硬失败，保守尺寸即可。
- B#7 `send_group_at` 仅 markdown 无纯文本降级：依赖部署（markdown 已全量开放），暂可接受。
- B#6 feedback 缓存读在锁外：可容忍（cache 语义，损坏读空）。
- 低危备忘：scheduler 分钟跳变/双触发；`_passive`/`_cooldowns`/media 内存表无界；gateway 干净关闭后 1s backoff 重置；日志无午夜轮转；problem prefetch 无失败 backoff；posts 重发不重置 solved_at；`prefetch_slot` 表死代码（slot 仍 JSON）；链式 /sync 来源 provenance 丢失；/newproblem 非法用法仍发 👀；仅用法提示也消耗冷却。

## 渲染 UX 校准 + bot 身份（2026-11-18，owner 真机对比反馈）

owner 对比真机手机转发卡与我们的 /help 长图，指出三处缺陷，逐一修复并目视验证：

- **底部大片空白**：根因是固定 viewport height=800，`full_page` 截图至少 800×dsf 高，短内容也撑出空白。修复：`set_content` 后量 `document.body.scrollHeight`，把 viewport 高度设为内容实际高度再 `full_page` 截图（`render/browser.py`）。
- **字体太小/一行字太多**：`_CSS` 整体放大——气泡正文 28px/line-height 1.5、`max-width: min(520px,100%)`（约 17–19 个汉字/行）、昵称 20px、头像 52px、pre 24px、header 28px、页脚 20px、消息间距 28px（`render/html.py`）。
- **bot 头像+名称未渲染**：见下「bot 身份实证」。

### bot 身份实证（纠正「官方完全拿不到 bot 头像/名称」的笼统结论）
- **名称**：网关 READY 的 `d.user.username` 是权威 bot 名（实测 "KouhaiBot"），但 **READY user 无 avatar 字段**。worker `_on_ready` 用 username 覆盖 `sender.bot_name`（空 username 不覆盖配置兜底名），并记录 `sender.bot_id`；gateway 加 `on_ready` 回调。
- **头像**：官方 API 不暴露 bot 头像。`thirdqq.qlogo.cn/g?b=qq&nk=<bot_id>&s=640` 返回的是**默认 QQ 企鹅占位图**（971 字节 PNG），不是控制台设置的头像。人类头像则走每事件下发的签名 `thirdqq.qlogo.cn/bqqthird?key=...` URL（可下载，已接）。
- **解决方案**：把 owner 提供的 bot 头像（动漫插画）作为**内置资产** `src/kouhai_bot/render/assets/bot_avatar.jpg`（85852 字节 JPEG，hatchling wheel 自动打包包内二进制资产），不依赖会过期的签名 URL。渲染优先级：节点自带 avatar → 配置 `official.bot_avatar_url`（可选远程）→ 内置默认资产 → 字母色块兜底。`_resolve_avatars` 按 `nickname==bot_name` 判定 bot 节点注入。
- config 新增 `official.bot_avatar_url`（默认空，example 注明官方不暴露 bot 头像、nk=bot_id 得企鹅）；sender 加 `bot_avatar_url`/`bot_id`。
- 新增/改测试：test_worker `_on_ready` 采用权威 username；test_render 内置资产加载/配置 url 优先/无资产字母兜底；test_config bot_avatar_url 解析+默认。全量 **634 passed**。
- 真机验证：worker 带新代码重启 READY，owner 实验群 /help（22:23）渲染+发送成功，无 render/send 警告，长图与验证图一致。

## 长图底部重复标题去除 + 当前题重爬（2026-11-19，owner 真机反馈）
- **底部重复标题**：发长图时 `send_forward_card` 传 `content=title`，官方媒体消息的 `content` 字段被 QQ 渲染成图片**下方**附带文字，与图内顶部 header 标题重复（如「题目」「帮助」）。修复：发图不再带 `content`（`render/forward_card.py`），图内 header 保留即足够。`test_render` 加断言媒体 payload 无 `content`。
- **当前题 1856E1 重爬**：最初发题时聚合站 gemini/deepseek 全 502 → `summary_status=incomplete`（卡片正文仅占位「刷新了一道新题🌟」）、样例解释英文原文。general 队列还原（gemini ✦ 第一 + 官方 deepseek-flash 🐳 第二）后重跑 `prepare_problem_summary` + `build_notes_message`：摘要 verified（175 字中文）、样例解释译中文，更新 DB `problems`/`group_posts.card_json`（保留原 posted_at）并重发修正卡。题解源此前已 verified（2509 字中文）intact，首 AC 投递。
- **遗留坑（owner 确认非移植引入，先 note 不管）**：样例解释翻译时模型按附带的题面图片插入 `[[IMAGE_1: graphic]]` 占位符，但文字节点未把占位渲染成真实图（题面树图）。属遗留 multimodal notes 渲染缺口，留待统一讨论。

## 命令精简为别名 + help 分点美化（2026-11-19，owner 要求）
- **诉求**：owner「尽量减少指令数量……只使用别名，不再使用完整名称」，help 里简单提一句「short for 什么」；并嫌旧 help「所有指令挤在一起」难看，要 Markdown 分点美化，但**保留聊天记录长图截图形式**。
- **决策（alias-only 接口）**：`registry.register()` 改为——有别名的命令**只注册短别名**作为可用触发词，canonical 全名仅作内部身份（dispatch 仍 alias→canonical 重写 `raw_text`，handler 内部正则按全名解析不变），全名本身不再被 `get()` 命中、用户发全名会被静默忽略。无别名命令（tag/scoreboard/status/sync/clear/range/help）注册自身名。新增 `CommandDef.trigger`（别名[0] 或名）供展示。
  - **UX 意图保留**：减少用户记忆负担（只记短名），同时不破坏 handler 解析与历史兼容；help 用「short for X」灰字注明全称，保留可追溯性。
  - **连带必改**：所有面向用户的提示文案 full→alias（否则提示用户发一个已失效的全名）——submit/clarify/review/newproblem/feedback/stubs 的用法与引导语。无别名命令的提示（/sync /clear /range /tag）保持原名。
- **help 重设计**：按主题分 3 节（📚 刷题 / ✍️ 判题·讨论 / 🔧 其他），每节一个独立气泡节点；节内 Markdown `<h3>` 标题 + `<ul><li>` 列表，触发词 `<strong>` 加粗 + 用法 + 描述 + 灰字 `.short`「short for <name>」。`render/html.py` 加 `.bubble ul/li/strong/.short` 样式。纯文本降级同样分节分点。仍是 `send_forward_card` 长图。
- **测试**：test_registry 改 alias-only 语义（get(canonical)=None、_registry 键=别名+无别名名、trigger 性质）+ 新增无别名命令注册测试；test_commands help 断言改分节格式 + 新增「dispatch 拒绝别名命令全名」测试；test_eventlog 两处用全名 dispatch 的用例改用别名；hint 文案断言（test_commands/test_feedback）full→alias。全量 **636 passed**。
- **待 owner 真机目视**：重启 e2e worker 后发 /help 看新分点长图效果。PR #119 仍未合并。

## off-topic 表情纠正：摇手指（2026-11-19，owner 真机反馈）
- **现象**：owner 随便 /sbm 了一条，判题返回 `reaction=123`（离题），bot 发了「😵」字符消息，owner 觉得「挂了」且表情不对——要求用 legacy 的 QQ 表情**摇手指**。
- **核实（官方表情列表原文）**：系统表情 EmojiType=1 id `123` 官方名「NO」，即摇手指；`322`=拒绝、`289`=睁眼、`314`=仔细分析、`128076`(type2)=👌。legacy 群聊 off-topic 正是 `react_emoji(msg_id, "123")`。
- **平台限制（核实）**：官方群聊发送仅 msg_type 0/2/7（text/markdown/富媒体），**不能**发 QQ 系统表情，reaction/表态接口仅频道。故无法在群里复刻系统表情 123 贴纸或打在用户消息上的反应。
- **决策**：off-topic 替代字符由 😵（晕脸，语义不符）改为 **🙅**（unicode「NO/不行」手势，对应系统表情 123「NO」的语义）。README 原本就写「摇手指表情（😵）」——意图一直是摇手指，仅字符选错。`_OFFTOPIC_EMOJI="🙅"`，测试/AGENTS.md/README 同步。
- **遗留**：🙅 是「NO」语义近似，非摇手指贴纸原图；官方群聊无系统表情/reaction 能力，此为平台硬限制下的最佳替代。待 owner 真机看 🙅 是否接受。

## 跨 bot @mention 干扰修复（2026-11-19，owner 真机反馈）
- **现象**：owner 在实验群 @ 另一个 bot（AstrBot「b猫bot完全体」）发 `/help`，**我们的 KouhaiBot 也响应了**——两个 bot 同时回 help。
- **根因**：官方网关把「@ 别的 bot 的命令」作为 `GROUP_MESSAGE_CREATE`（非 AT）下发，`mentions[]` 里带那个 bot（`bot=True, is_you=False`）。dispatch 原先只剥离开头 `<@openid>`  markup 就做斜杠判断，**没检查 @ 的目标是谁**，于是把发给别人的命令当成自己的。
- **决策**：新增 `_addressed_to_other_bot(event)` 守卫——`mentions[]` 含 `bot=True 且 is_you=False` 的成员、且**没有任何 `is_you=True`** 时，整条消息忽略（返回 None）。置于 **echo 之前**：发给别的 bot 的消息既不触发命令，也不进入我们的复读缓冲。@ 我们（含同时 @ 我们+别的 bot）、@ 普通人类、无 mention 的裸命令均照常 dispatch。非 dict 的 mention 项容忍跳过。
- **意图保全**：legacy「@bot 才触发」在官方变成「任何 / 命令都触发」（D14），但**绝不响应明确发给其他 bot 的命令**——这是多 bot 共存群的基本礼貌，也是 owner 的预期。
- **测试**：test_eventlog 新增 `test_command_addressed_to_other_bot_ignored`（@别的bot忽略 / @我们dispatch / @两者dispatch / @人类dispatch / 裸命令dispatch / 非dict容忍）；test_echo 新增 `test_process_event_other_bot_messages_skip_echo`（发给别的bot的消息零复读零缓冲）。全量 **638 passed**。AGENTS.md Dispatch 节同步。PR #119 仍未合并。

## 恭喜+题解图合并探索（2026-11-19，owner 问「能不能同一条消息」→ 真机实测后否决）
- **诉求**：owner 看到 AC 后「恭喜 markdown」与「题解长图」是两条气泡，问能否合到一条。
- **核实 + 真机实测（不臆想）**：官方 markdown **支持内嵌图片**（`![说明 #宽px #高px](url)`）。三种情况实测：
  - **公网 URL 图**：群聊 markdown **正常渲染成图片**（用官方文档示例图实测，@+恭喜+图完美同一条气泡）。→ 群聊 markdown 确实渲染图片，非频道专属。
  - **`qqbot-media://` 引用无尺寸 hint**：退化为 alt 纯文字 `[官方题解]`，**图不显示**。
  - **`qqbot-media://` + 尺寸 hint**（`#720px #3132px`，源图 1440×6264）：**渲染成功**（owner「可以了」）。→ qqbot-media 内嵌需带 hint。
  - **素材管理接口我们 bot 有权限**：`POST /v2/media-images/prepare`{file_name,file_size,content_type,md5,sha1}→{upload_id,presigned_url,expires_in}；`PUT presigned_url` 二进制（Content-Type 须与 prepare 一致，返回 ETag=MD5）；`POST /v2/media-images/complete`{upload_id,md5}→{image_id,url(qqbot-media://image/v1/<token>),ttl,preview_url}。限 10MB/张、png/jpeg/gif/webp、**100 张/bot**、滑动 90 天过期。**发送后平台转存副本，删原图（`DELETE /v2/media-images/{image_id}/delete`）不影响已发消息**→可「上传→发→删」避免配额累积。
- **决策（owner 定）**：真机对比后 owner 认为**合并效果不如分开** → **维持两条消息**（恭喜 markdown @winner 即时 + 题解长图独立）。AC 流程本就未改；移除为合并临时加的 rest.py 素材管理接口（死代码）。上述 API 形状记此备查（未来若需 markdown 内嵌图可用）。
- **连带简化**：review 长回复后的提示「回复较长，已折叠到卡片里啦 👆」在长图形态下冗余（图已承载内容）→ 简化为**仅一个 @asker**（保留通知，去文字）。`submit.py:_finalize_review` 改 `_send_plain(req, "")`；test_commands 断言改 `texts[-1].strip()==at_tag()`；AGENTS.md /review 节同步。

## /np 卡片预渲染 + 去掉新题 greeting（2026-11-19，owner 要求）
- **诉求**：owner「np 的提前准备，把渲染图片也包括进去吧，不然慢慢」+「刷新了一道新题 这句话也去掉吧」。
- **去 greeting**：新题卡 lead 文案不再带 `刷新了一道新题🌟`（默认 prefix）/`来看看这道新题吧！`（fallback）。`newproblem.py` 删除 `prefix` 参数与 greeting 拼装；lead = 摘要 + model_tag + snake，统一走 `cards.build_problem_post_msg(summary, model_tag)`。空摘要时跳过空 lead 气泡（`build_problem_card_nodes`/`problem_card_text` 过滤空串）。
- **预渲染（关键设计）**：长图渲染（Playwright 截图）是 /np 发布时的慢点。改为在后台预取阶段渲染一次并缓存：
  - `NextProblemPrefetcher._build_and_store` 在 `prepare` 之后调 `_prerender_card`（best-effort，无 runtime/浏览器失败仅 warn，绝不影响 READY 槽）。
  - `cards.prerender_problem_card` 渲染 PNG 写 `{data_dir}/render_cache/problem_cards/<key>/card.png` + `meta.json`（原子写）。**缓存键 = 渲染版本 + pid + 题面指纹 + bot_name + bot_avatar_url**（稳定键，不含随机内容）。
  - **随机 snake 固化**：`snake_replace` 每 emoji 10% 概率变 🐍 是随机的——若发布时重算 snake，缓存键/图会对不上。故预渲染时把算好的 `post_msg` 存进 `meta.json`，发布时 `load_prerendered_problem_card` 取回 **(png, post_msg) 一并复用**，图与持久化卡文永远一致。无需改 `PreparedProblem` 结构。
  - 发布：`newproblem._post_new_problem_locked` 先查缓存，命中则 `send_problem_card(prerendered_png=png, post_msg=缓存值)` → `send_forward_card(png_override=...)` **跳过渲染**直接上传发送；未命中（冷启动/legacy/浏览器挂）回退发布时渲染，行为同旧。
  - `send_forward_card` 新增 `png_override` 参数；其余发送/文本降级逻辑不变（never raises）。
- **测试**：test_cards 新增 6 测试（post_msg 拼装+snake、预渲染读写 roundtrip+键绑定、空卡不写、png_override 跳过渲染、空 lead 跳过）。全量 **644 passed**。AGENTS.md /newproblem 节 + 数据目录同步。PR #119 仍未合并。

## /scoreboard 气泡内轻量文本增强（2026-11-19，owner 真机反馈「plaintext 不好看、2^ 公式丑」→「矫枉过正，像 /help 稍微增强即可」）
- **诉求**：owner 看到 /scoreboard 长图里是纯文本气泡，公式 `2^((rating-2000)/300)` 丑；明确**保留聊天记录形式**（不要独立海报），只美化气泡内容，参照之前 /help 的美化风格。
- **第一版过度设计（已撤）**：先做了金/银/铜渐变行卡 + 奖牌徽章 + 蓝色积分 pill + 公式灰底 chip + `#n` 紫 pill 的「富文本排行榜」。owner 真机看后反馈「矫枉过正了，不用完全做成前端设计，就像 /help 那样稍微做点文本增强就行」→ **撤掉全部 `.sb-*` 行卡/pill/chip 样式**。
- **最终实现（/help 同款轻量）**：`render/html.py:build_scoreboard_html(ranked)` 产出**气泡内轻量富文本**，复用 /help 已有的 `.bubble h3/ul/li/strong/.muted` 样式，仍走 `send_forward_card` 聊天卡片 chrome：
  - `<h3>` 加粗标题「📊 累计解题排行榜」+ `.muted` 灰字「共 N 人」。
  - 公式作 `.muted` 灰字一行：「计分：2000 分 = 1 分，每 +300 分翻倍（2<sup>(rating−2000)/300</sup>）」——**真上标**替代丑 `2^` caret（此项 owner 明确要求保留），减号用 −；无 chip 边框/底色。
  - `<ul><li>` 简单列表：前三名 🥇🥈🥉 奖牌（纯 emoji 文本）、其余 `#n`；`<strong>` 加粗昵称 + 普通文本「— N 题 / X 分」。长昵称自然换行（聊天文本，不截断）。
  - 仅保留 `_SB_MEDALS` + `_fmt_points`；删除 `_SB_ROW_CLS` 与全部 `.sb-*` CSS（只留一条 `.bubble sup` 上标字号）。
- **文本降级**：stubs 保留 plain-text fallback（渲染失败时），公式行 readable「计分：2000 分 = 1 分，每 +300 分翻倍」（plain 无上标）；名次行不变。
- **否决的 alternative**：① 独立排行榜海报（渐变头+积分条）——owner 要保留聊天形式，撤；② 气泡内重样式行卡/pill——owner 嫌矫枉过正，撤。最终收敛到 /help 同款轻量分点。
- **测试**：test_render 3 测试改锁轻量 markup（奖牌/上标/`<strong>昵称</strong> — N 题 / X 分`/`<h3><ul><li>`、断言无 `sb-score`/`sb-gold`；并列名次共享奖牌；昵称转义）；test_commands 仍只断言纯文本降级。真机渲染预览目视确认为 /help 风格分点文本。全量 **648 passed**。AGENTS.md /scoreboard 行同步。PR #119 仍未合并。

## /pb 复用预渲染卡片缓存（2026-11-19，owner 问「/pb 会重新生成长图还是复用 cache」）
- **诉求**：owner 确认 /pb（重发当前题）是否复用 /np 预渲染缓存；若是重新生成则改为复用。
- **现状核实**：/np 发布已走预渲染缓存（见上节），但 `/pb`（`stubs.resend_current_problem_group`）原先每次调 `send_problem_card` 都重新渲染长图——慢且浪费。
- **实现**：`stubs._load_problem_card_cache(rt, db, pid)` 按当前 pid 查 `problems.get_problem` 取 `statement_sha256`，用与发布/预取**完全相同**的键（pid + 题面指纹 + bot_name + bot_avatar_url）调 `load_prerendered_problem_card`，命中返回 PNG。`resend_current_problem_group` 把该 PNG 作 `prerendered_png` 传入 `send_problem_card` → `send_forward_card(png_override=...)` **跳过渲染**直接上传发送。未命中（legacy/冷题/指纹缺失/浏览器挂）回退现场渲染，行为同旧。
- **意图保全**：/pb 与 /np 复用同一张冻结图（含随机 snake 固化的 post_msg），重发即秒发，且图与持久化卡文一致。缓存键稳定不含随机内容，故 /pb 无需重算 snake。
- **测试**：test_cards 已覆盖 `send_problem_card(prerendered_png=...)` 跳过渲染；test_commands 新增 `test_problem_reuses_prerendered_cache`（真实预渲染写缓存→/pb 命中→发媒体消息而非文本降级，env 禁用渲染故命中必跳过 render）。全量 **648 passed**。AGENTS.md /problem 行 + 预渲染节同步。PR #119 仍未合并。

## /status 列出所有在处理请求（2026-11-19，owner 要求「详细一点，包含谁的什么类型请求」）
- **诉求**：原 /status 忙时只报最早一条的命令名（「🔴 bot 当前有 submit 请求还在处理中～」）。owner 要列出**所有**正在处理的请求，谁 + 类型即可（不必具体到哪条），例「目前正在处理的请求有 @A 的 submit，@B 的 clarify，@C 的 review」；**保持 plaintext**（判题队列通常不长，不做卡片）。
- **实现**：
  - `submit.get_group_active_requests(group_openid)` 返回该群所有在途 stateful 请求（按 admission seq 排序，滤 discarded），每项 `{nickname, command, user_openid}`。nickname 取 `PendingRequest.nickname`（admission 时 `ctx.name`，已是 display_name）。
  - `newproblem.enqueue_new_problem` 新增 `nickname` 参数并存入 `_newproblem_active`（两个 handle 调用点传 `ctx.name`），使 /status 也能显示换题请求的发起者。
  - `stubs.handle_status` 合并 stateful 列表 + 活跃 newproblem（nickname 经 `display_name` 兜底），拼 plaintext「🔴 目前正在处理的请求有：@昵称 的 submit、@昵称 的 clarify～」（顿号分隔）；空则原 🟢 空闲文案。命令名用 canonical（submit/clarify/review/clear/newproblem），与 owner 示例一致。
  - **清理死代码**：原 `get_group_lock_status` / `GroupCoordinator.status()` / `PendingRequest.status_dict()` 仅服务于旧「最早一条」逻辑，被取代后无消费者→删除。
- **plaintext @ 说明**：owner 示例用「@A」且明确 plaintext 即可，故昵称前缀 `@` 仅为文本标记（非真实 @ 通知，/status 纯信息展示，无需 markdown @）。
- **测试**：test_commands 单请求断言改新文案；新增 `test_status_lists_multiple_active_requests`（submit+clarify 同时在途→「@Alice 的 submit、@Bob 的 clarify」，释放后回 🟢）。全量 **649 passed**。AGENTS.md /status 行 + status helpers 同步。PR #119 仍未合并。

## 旧数据热迁移补丁（2026-11-19，owner 反转原「放弃兼容/不迁移」决定）
- **诉求**：owner 改主意——旧文件系统数据要迁进新 SQLite，但**只迁服务群那一份，私聊直接丢掉**（原「把 private 全部 sync 进群」作废）。因官方 openid 只在成员互动时下发、且无 roster/history 接口，无法批量映射 QQ 号→openid，故做成**运行时「热迁移」补丁**：服务时每收到一条服务群消息就捕获 `(openid, 群昵称)`，用昵称匹配旧 QQ 号，命中即刻把该成员的旧群数据迁过来，并持久化 openid→QQ 映射；**仅旧服务群启用**（其他群无旧数据，config 门控）。同时：祝贺消息去掉「目前排第几」那半句（**所有群**）；服务群 /scoreboard 在算分行下加一行灰字「数据正在迁移中，排名可能不完整。」。
- **身份阻塞（已实证，非臆想）**：member-list 接口 `code 11253 应用无接口访问权限`（邀请制接口权限，非群开关，翻开关后复测仍拒）；群历史消息无接口（`/v2/groups/{g}/messages`→405、`/messages/history`→405、`/history`→404）。结论：只能靠成员真实发言实时捕获 openid，昵称匹配旧 QQ。
- **整理（throwaway 脚本，/tmp 不提交）**：`/tmp/kouhai-migration/stage.py` 读旧 `~/.kouhai-bot/groups/<服务群>` → `legacy_source.db`（本地运行时数据，含真实 QQ/昵称，**绝不提交**）。表：`legacy_users/legacy_solves/legacy_submissions`（按旧 QQ）、`legacy_problems/legacy_editorials`（全局目录）、`nickname_index`（昵称→旧 QQ，标 latest）。结果：61 人 / 1044 solves / 2047 submissions / 3262 题（1045 有 rating、396 verified 摘要、684 legacy 摘要、7 stale、348 verified 题解）。**私聊不读不迁**。
- **指纹绑定（实证一致）**：新 `statement_fingerprint` 对**原始旧题面**（`title` 键 intact、`_` 前缀键剔除）算出的 sha 与旧 marker 一致——349/354 题解 `.verified`、396/403 新式摘要绑定成功（少量 mismatch 是题面后来重爬的 stale 数据，新运行时同样会拒）。故目录存**原始题面**、按计算指纹绑定：摘要/题解仅当旧 marker sha == 计算指纹才标 `verified`；legacy 无绑定摘要存 `status=legacy`（归档不采信）；stale 存 `status=stale`；`no_editorial` marker 无绑定且对已解题价值低→**跳过**。`problems.name` 列取旧 `title`（列不影响指纹），`statement_json` 保持原始以保绑定。
- **运行时补丁**（`src/kouhai_bot/legacy_migration.py`，提交）：
  - config 新增 `legacy_migration.{enabled,group_openid,source_db}`；`is_migration_group()` 廉价门控（无 IO）。
  - dispatch 在 echo 后、slash 判定前对**每条群消息**（不止命令）`schedule_migration()` fire-and-forget（不阻塞收包），按 openid 去重在途任务。
  - 命中昵称（**精确 strip 匹配**，latest 优先、再 historical；>1 旧 QQ  claimed 同昵称→歧义跳过并 log 待手动）→单事务迁 `group_solves`（保留旧 `order_num`、`source='migrated'`、`ON CONFLICT(group,user,pid) DO NOTHING`）+ `submissions`（`request_id=NULL` append、`synced=0`）+ 写 `legacy_migration_map`（幂等标记，事务内复检防并发重复 append）。
  - 目录一次性 `INSERT … ON CONFLICT(pid) DO NOTHING`（**绝不覆盖** live 已预爬题，保护当前题新指纹），单事务批量。
  - 捕获即 `upsert_user(openid, nickname)`→scoreboard 显示真名。源 DB 只读 aiosqlite 缓存连接，`aclose_source()` 供停机/测试 teardown。
  - `legacy_migration_map` 表入 schema（ additive，SCHEMA_VERSION 仍 1）。全程参数化 SQL（恶搞昵称 `…DROP TABLE…` 实测 inert）。
- **昵称匹配实测**：精确 strip 下 **0 歧义**（「吴昊恒」两号前缀不同；「ExplodingKonjac」希腊 ο vs Latin o 不同串；「杜子腾」仅属一号），**10 个无昵称旧用户** owner 决定丢弃（见下）。
- **关键决定/遗留（待统一讨论）**：
  - **order_num 重号已根治（2026-11-20，owner 追问后修复）**：原「窗口内可能重号、迁完自愈」的判断**前提错误**——有人一直不说话就永远不迁，COUNT 永远到不了旧最大值，窗口永不关闭。改为：新增 `group_order_floor(group_openid PK, floor)` 表，`_ensure_catalog` 从源 `MAX(order_num)`（旧全局最大号，本数据=1044）落库为该群下限；`record_solve` 由 `COUNT+1` 改为 `max(当前MAX, floor)+1`，于是 native 新号永远从 1045 起，跟任何旧号（哪怕那人永不发言）都不撞，且时间先后天然正确（新题都在历史之后）。`worker.run()` 在网关启动前 `await legacy_migration.ensure_order_floor(db)` 落库，关掉「首条消息即判对（如 AC backdoor）抢在迁移加载前」的竞态。非迁移群无 floor 行→退化为普通 `MAX+1` 单调计数（无删除路径，与原 `COUNT+1` 等价）。`latest_solved_pid`/scoreboard 均不再受迁移窗口影响。
  - 同一旧 QQ 被两个 openid 命中（理论上昵称唯一匹配只一号）会各自迁一份→openid 稳定一人一号，实际不发生；若 owner 手动映射需留意。
  - 服务群 `group_openid` 待 owner 拉群后提供再填 config（旧 config 只有数字 QQ 群号 `current_group`，从未有过 openid，非「弄丢」；官方无 QQ群号→openid 转换接口，只能进群收消息时由网关下发，worker 日志 `[group_<openid>] discovered` 可读取）。**10 个无昵称旧用户 owner 决定直接丢弃**（他们 0 解题、旧库无昵称可供匹配，发言也匹配不上，且数据量小不影响排行榜）。
- **祝贺去排名 + scoreboard 灰字**：`submit._send_scoreboard_success` 删 `，当前第 {rank}` 及无用 rank 行（保留 current 算总分）；`render/html.build_scoreboard_html(..., migration_note=)` 公式行下加 `.muted` 灰字（escape）；`stubs.handle_scoreboard` 按 `is_migration_group` 门控，HTML + plaintext fallback 都加。
- **测试**：新增 `tests/test_legacy_migration.py`（合成源 DB，无真实数据；latest/historical/歧义/无匹配/空昵称/注入 inert/幂等/目录不覆盖 live/指纹绑定/scoreboard/群门控/dispatch 非命令钩子/去重 + **order_num 下限：native 高于旧最大号/无人迁移也生效/迟迁不撞号且 latest 正确/非迁移群普通计数**）+ test_render 灰字 + test_commands 祝贺去排名 & 服务群灰字门控；conftest autouse 复位补丁状态。全量 **670 passed**。PR #119 仍未合并。

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
- Worker 启动冒烟（2026-11-18，自主，bounded 12s 自停）：停 capture 探针释放网关会话后，用 fresh data_dir + 占位 LLM 配置前台跑 `python -m kouhai_bot.worker`。日志确认 `Official worker running` → `Scheduler started` → token 刷新 → `gateway identifying (intents=117440512)` → `gateway READY (session=…)`，**无 traceback**；exit 0，无残留进程。证明新 worker 完整装配（config 校验/DB/set_runtime/网关/调度）可真机启动并连上官方网关。fresh data_dir 无群发现→无 prefetch/LLM/群消息（contest_check 仅 12:01），冒烟零外部副作用。**仍待 owner**：在实验群发指令做端到端 UX 验证（bot 自身出站不触发 dispatch，需 user 发消息），及确认「大体能用」后合并 PR #119。
- E4 截断验证关闭开放问题（2026-11-18，owner 引用法）：先实测确认 bot 出站零回推（E4 期间 80+ 条出站，capture 仍仅 9 条 user 事件）→ 无法直接读回自身消息。改用引用法：bot 发 5000 码点长消息（每 1000 字嵌【N000】、结尾【5000】，不撤回），owner 长按引用；capture 抓引用事件 `msg_elements[0].content`=**完整 5000 码点**、【1000】…【5000】齐全 → **服务端无静默截断**（至少到 5000）。开放问题关闭。附带坐实引用 bot 消息时客户端自动 @bot（mentions=1）。测试消息发后即撤回，群保持干净；探针/监听已停，网关会话释放。C4/Q1/E4 + AGENTS.md pitfall#6/探针节同步更新为「已验证无截断」。
- 代码自审修复（2026-11-18，owner「先自己 review 一波再端到端验证」）：三路子 Agent 分审并发/传输/数据格式/安全，逐条核实修确证 bug——DB 单连接事务串行化（连接级锁，关键）、首杀计分 sleep 出锁、clarify/clear/review admission 快照入锁、/sync 只拉已回复记录、message_refs 复合主键、editorial 译文绑源指纹、/newproblem 冷却回退 posted_at、dispatch/echo 剥前导 `<@bot>`（无 !）、review mention 保真、/clear 误 ack 改用法提示、REST 网络异常统一降级、send_forward_card 永不 raise、头像负缓存+并行、Playwright driver 泄漏、picker stderr 全量分类、worker 停机取消 editorial 任务+启动恢复已知群、pidfile 身份核验防误杀、停机等待 10→40s、删死常量 TEXT/MARKDOWN_MAX_CHARS。新增「代码自审修复」章节 + 延后讨论清单。全量 **629 passed**（+4 新测试）。PR #119 仍未合并（待端到端验证 + owner 确认）。
- 渲染 UX 校准 + bot 身份（2026-11-18，owner 真机对比）：修长图底部空白（viewport 量 `body.scrollHeight` 适配内容）、放大字体收窄气泡（28px/`min(520px,100%)`）、渲染 bot 头像+名称。实证 READY `username` 权威但无 avatar 字段、`nk=bot_id` 得默认企鹅→bot 头像内置资产 `render/assets/bot_avatar.jpg`（优先级 自带→config url→内置→字母兜底）；gateway `on_ready` + worker 采用 username + sender `bot_id`/`bot_avatar_url` + config `official.bot_avatar_url`。新增/改测试，全量 **634 passed**。真机 /help 渲染+发送验证通过。PR #119 仍未合并。
- 长图底部重复标题去除 + 当前题重爬（2026-11-19，owner 真机反馈）：发图去掉 `content=title`（QQ 把它渲染成图下重复标题），图内 header 保留；test_render 加无 content 断言。e2e 当前题 1856E1 因早期聚合站 502 致摘要 incomplete/样例英文，general 队列还原（gemini ✦ + 官方 deepseek-flash 🐳）后重跑摘要+样例翻译、更新 DB 重发修正卡（题解源已 verified intact）。记遗留坑：样例解释 `[[IMAGE_x]]` 占位未渲染成真实图（owner 确认非移植引入，先搁置）。PR #119 仍未合并。
- 命令精简为别名 + help 分点美化（2026-11-19，owner 要求）：registry 改 alias-only（别名命令只注册短别名，全名仅内部身份不被接受），新增 `CommandDef.trigger`；面向用户提示文案 full→alias；help 重设计为主题分节（📚/✍️/🔧）每节独立气泡 + Markdown 列表加粗触发词 + 灰字「short for X」，保留长图形式；render/html.py 加列表样式。测试改 alias-only 语义 + 新增 2 测试，全量 **636 passed**。待 owner 真机目视 /help。PR #119 仍未合并。
- off-topic 表情纠正（2026-11-19，owner 真机反馈）：核实官方表情列表系统表情 123=「NO」(摇手指)，legacy 群 off-topic 即 react 123；官方群聊不能发系统表情/reaction（仅频道）→ 替代字符 😵 改 **🙅**（NO 手势，语义对应）。代码/测试/AGENTS.md/README 同步。PR #119 仍未合并。
- /np 去掉 👀 ack（2026-11-19，owner 要求）：READY 槽预取缓存后发题很快，👀 确认纯属噪音→移除 newproblem.handle 的 react；submit/clarify/review 因判题慢保留 👀。AGENTS.md /newproblem 节注明。636 passed。PR #119 仍未合并。
- 跨 bot @mention 干扰修复（2026-11-19，owner 真机反馈）：@ 别的 bot 发 /help 时我们也响应→dispatch 新增 `_addressed_to_other_bot` 守卫（mentions 含 bot=True/is_you=False 且无 is_you=True 时整条忽略，置于 echo 前），@我们/@人类/裸命令照常。新增 2 测试，全量 **638 passed**。AGENTS.md Dispatch 节同步。PR #119 仍未合并。
- 恭喜+题解图合并探索（2026-11-19，owner 问→实测→否决）：真机证实群聊 markdown 可内嵌图片（公网 URL 直接渲染；qqbot-media:// 需尺寸 hint，无 hint 退化 alt 文字），素材管理接口有权限、可上传→发→删避免 100 配额。owner 对比后认为合并不如分开→**维持两条消息**，AC 流程未改，移除临时素材管理接口（API 形状记决策日志备查）。连带：review 长回复提示「回复较长…👆」冗余→简化为仅 @asker。测试/AGENTS.md 同步。PR #119 仍未合并。
- /np 卡片预渲染 + 去 greeting（2026-11-19，owner 要求）：新题卡去掉「刷新了一道新题🌟」greeting（lead=摘要+tag+snake）；后台预取阶段预渲染长图缓存（键=版本+pid+题面指纹+bot身份），随机 snake 的 post_msg 固化进 meta.json，/np 发布命中缓存直接发图跳过渲染（send_forward_card 加 png_override），未命中回退发布时渲染。新增 6 测试，全量 **644 passed**。AGENTS.md /newproblem 节+数据目录同步。PR #119 仍未合并。
- /scoreboard 气泡内轻量文本增强（2026-11-19，owner 反馈两轮）：保留聊天记录形式。第一版做成金/银/铜渐变行卡+积分 pill+公式 chip 的重样式→owner「矫枉过正，像 /help 稍微增强即可」→撤掉全部 `.sb-*` 样式，收敛到 /help 同款轻量：`<h3>` 加粗标题+灰字人数、`.muted` 公式行（真上标 2^(rating−2000)/300 替代丑 2^，此项保留）、`<ul><li>` 奖牌/#n + 加粗昵称 + 「— N 题 / X 分」。独立海报方案亦早撤。test_render 3 测试改锁轻量 markup，全量 **648 passed**。AGENTS.md 同步。PR #119 仍未合并。
- /pb 复用预渲染缓存（2026-11-19，owner 问）：/pb 重发当前题原先每次重新渲染长图→改为复用 /np 预渲染缓存（stubs._load_problem_card_cache 按 pid+题面指纹+bot身份取 PNG，prerendered_png 跳过渲染），未命中回退现场渲染。与 /np 共用同一冻结图（snake 固化），重发秒发。新增 test_problem_reuses_prerendered_cache，全量 **648 passed**。AGENTS.md /problem 行+预渲染节同步。PR #119 仍未合并。
- 旧数据热迁移补丁（2026-11-19，owner 反转「放弃迁移」决定）：私聊丢弃、只迁服务群。实证 member-list 11253 邀请制 + 无历史接口→运行时按访问热迁移：dispatch 每条服务群消息捕获 openid+昵称，精确 strip 匹配旧 QQ（latest 优先，歧义跳过），单事务迁 group_solves(保留旧 order/source=migrated)+submissions(NULL request_id)+legacy_migration_map 幂等标记；目录一次性 insert-if-absent（不覆盖 live）；指纹绑定实证一致（原始题面，349/354 题解+396/403 摘要 verified，stale/legacy 归档不采信，no_editorial 跳过）。config legacy_migration 门控仅服务群。祝贺去「当前第 N」（所有群）+ 服务群 scoreboard 公式下灰字「数据正在迁移中，排名可能不完整。」。整理脚本 /tmp 不提交、源 DB 本地运行时数据。实测 0 昵称歧义、10 无昵称用户待手动映射。新增 test_legacy_migration + render/commands 测试，全量 **666 passed**。待 owner：服务群 group_openid + 拉 bot 进真实群开全量消息 + 指认 10 无昵称用户。PR #119 仍未合并。
- /status 列出所有在处理请求（2026-11-19，owner 要求）：忙时由「只报最早一条命令名」改为 plaintext 列出全部在途请求「🔴 目前正在处理的请求有：@昵称 的 submit、@昵称 的 clarify～」（谁+类型，admission 序，含活跃 newproblem）。新增 submit.get_group_active_requests；newproblem active 带 nickname；删死代码 get_group_lock_status/coord.status()/status_dict()。新增多请求测试，全量 **649 passed**。AGENTS.md 同步。PR #119 仍未合并。
- 热迁移 order_num 重号根治（2026-11-20，owner 追问「不是全局的吗/有人一直不说话怎么办」）：纠正前一条「迁完自愈」的错误前提（沉默成员永不迁→窗口永不关）。新增 `group_order_floor` 表存旧源 `MAX(order_num)` 为该群下限；`record_solve` 改 `max(当前MAX, floor)+1`，native 新号恒从旧最大号+1（1045）起，与任何旧号永不撞、时序天然正确；`worker.run()` 网关启动前 `ensure_order_floor` 落库关掉首条即判对的竞态；非迁移群退化为普通 MAX+1。owner 另定：10 无昵称旧用户直接丢弃；服务群 openid 待拉群后提供。新增 4 个下限测试，全量 **670 passed**。schema/AGENTS.md 同步。PR #119 仍未合并。
- 官方「指令」菜单 payload 实测（2026-11-20，owner 问「点指令会不会拨掉斜杠/奇怪形式打架」）：临时探针录真实入站字节证实——点击注册的命令 chip 发的是普通群文本 `<@bot_openid> /help `（**斜杠原样保留** + 前置 `<@…>` 提及段 + 尾空格），mentions 数组带 `is_you=True`；手打则是裸 `/help`。两者经现有 `_LEADING_MENTION_RE` 剥提及后都命中 `/help`，端到端跑通（owner 确认最近一次 help 即官方指令发出）。**与纯文本解析完全兼容，无需改解析**。注册指令仅是客户端可点 chips 的便利（全量消息模式下裸打别名本就触发）；该对话框上限 10 个，私聊类 /sp /sync /testcd 已禁用不注册。探针验证后已移除。PR #119 仍未合并。
- /help 精简 /np 描述（2026-11-20，owner 要求）：去掉冷却时间（「大家不关心」）+ 整个括号含 --force 提示（没写 force 刷新不了时 bot 本就会提醒）；措辞「刷一道新题」→「来一道新题」（更口语亲切）。newproblem 注册 description="来一道新题"、usage=""（--force 功能不变，仅 help 不再展示）；help.py 删除 newproblem 特判/_format_duration/cooldown 管线，所有命令统一用注册 description。test_commands 改锁「/np — 来一道新题」+ 无「冷却」/「--force」。全量 **670 passed**。PR #119 仍未合并。
- 砍复读 + 命令必须 @bot（2026-11-20，owner 要求）：两项一起落地。
  - **砍 echo**：owner「复读功能直接去掉吧，算了，砍掉」。删 `src/kouhai_bot/echo.py` + `tests/test_echo.py`；dispatch 移除 `import echo` 与 `echo.check_and_echo` 调用；conftest 移除 echo 复位。config 本无 echo 字段（无需改）。AGENTS.md（架构图/Dispatch/Echo 节/Commands/测试节）+ README 复读机注记同步删除/改写。无关的「echo」（llm model-tag echo、probe 出站不回推）不动。
  - **命令必须 @bot**：owner「改成必须@这个BOT吧…如果不@可能会打架」。dispatch 新增 `_mentions_us(event)` = `at_bot`（GROUP_AT_MESSAGE_CREATE）**或** `mentions[]` 任一项 `is_you=True`（指令 chip / 引用 bot 卡 / 全量消息 @，实测三者都带 is_you）。门禁置于 `legacy_migration.schedule_migration` **之后**、slash 判定**之前**——热迁移仍捕获每条群消息身份，但未 @bot 的裸命令/普通聊天/别的 bot 命令在此静默丢弃。保留 `_addressed_to_other_bot` 守卫与全量消息 intent（供热迁移）。无配置开关，恒生效。
  - **测试**：test_eventlog `_event` / test_commands `group_event` / test_feedback 内联事件默认带 `mentions=[{is_you:True}]`（chip 形态）；`test_command_addressed_to_other_bot_ignored` 改 require-@ 语义（@人类only/裸命令→忽略，@人类+我们→dispatch）；新增 `test_commands_require_at_bot`（裸忽略 / at_bot 形态 dispatch / chip is_you dispatch / 引用卡 dispatch / 仅别的bot忽略）。test_legacy_migration 非命令裸消息仍验证迁移钩子（门禁在其后，不受影响）。删 test_echo 后全量 **650 passed**。AGENTS.md/README/DECISIONS（D7/D15/能力节）同步。PR #119 仍未合并。
- 消息按钮（inline keyboard）接线（2026-11-20，owner 要求「题图下加 提交/询问题意，打回加 继续提交/清空上下文，通过题目+force 加 跳转原题；都放一排，单按钮占整行、双按钮对半分」）：
  - **能力核实 + 探针**（task #38）：官方文档「仅 markdown 消息支持消息按钮」→ media/长图**不能**同消息带按钮。临时探针真机验证载荷形状：markdown+keyboard 接受；markdown `content=""` 被拒 **40034011 无效 markdown content**，`content=" "` 接受（按钮专用消息用单空格）；media+keyboard 虽接受但渲染端大概率忽略。`action.type` 0=跳 URL、2=指令（点击插入 `@bot <data>`，`enter=false` 仅预填、`enter=true` 直接发送）；指令按钮插入的 `@bot` 天然满足 require-@ 门禁。`permission.type=2`（所有人）、`render_data.label`≤10 字、`unsupport_tips` 必填、`bot_appid` 取自 config。布局 `rows[].buttons[]` 左→右共享行宽：单按钮占整行、双按钮对半分。探针验后移除。
  - **实现**（task #39）：新增 `official/keyboard.py`（`cf_problem_url`/`command_button`/`url_button`/`keyboard` + 三个便捷构造器 `problem_action_keyboard`/`reject_action_keyboard`/`jump_original_keyboard`；`_appid()` 空配置返回 None→省略 bot_appid，避免测试/未配置崩溃）。`sender.send_group_at` 加 `keyboard` 透传 markdown。接线四处：① 题卡（`/np`+`/pb` 共用 `cards.send_problem_card`）长图后**追发一条按钮专用 markdown**（`content=" "`）[提交 `/sbm`、询问题意 `/clrf`]；② submit 两条 incorrect 路径（reply 分支 + reason「再想想？🤔」兜底）markdown 带 [继续提交 `/sbm`、清空上下文 `/clear` enter=true]；③ 恭喜 `_send_scoreboard_success` markdown 带单按钮 [跳转原题→CF URL]；④ `/np --force` 跳过通知由 `send_group_text` 改 `send_group_markdown` 带 [跳转原题]（pid 提取移到发送前）。
  - **关键决定/遗留（待统一讨论）**：长图与按钮**无法同消息**（官方 markdown-only 限制）→ 题卡按钮只能追发一条独立 markdown 消息（视觉上紧贴图下），非「图所在那条消息内」。这是官方能力边界，非实现取舍；若 owner 介意两条消息，唯一替代是把题卡也改成 markdown 内嵌图（放弃长图高保真），需 owner 定夺。`跳转原题` 用 problemset URL（`/problemset/problem/{contest}/{index}`），pid 不可解析时返回 None→该消息不带按钮（降级为纯 markdown）。
  - **测试**：新增 `tests/test_keyboard.py`（URL 解析/按钮形状/行布局/三构造器/空 appid 省略）；test_commands 加 incorrect×2 + 恭喜 + force 通知按钮断言、`/pb` 文本降级与预渲染缓存改锁「图/文 + 追发按钮 markdown」；test_cards sender fixture 注入 config + 媒体路径追发按钮断言。全量 **670 passed**。AGENTS.md 新增 Buttons 节。PR #119 仍未合并（待 owner 真机目视按钮渲染/布局）。- 题卡改单条 markdown 缩略图 + 按钮（2026-11-20，owner 真机校准后定）：解决上一条「长图与按钮无法同消息」的遗留——owner 选择把题卡本身改成 markdown 内嵌图，放弃独立长图媒体消息，换取「图 + 按钮同一气泡」。
  - **尺寸校准（真机多轮）**：markdown 内嵌图语法 `![alt #宽px #高px](url)`（官方素材管理 + Markdown 文档核实）；公网 URL 直接渲染，`qqbot-media://` 引用**必须带尺寸 hint** 否则退化 alt 文字，hint 客户端确实生效。owner 先要 1/4（180×528）「没变小」（原图太大撑爆对话框，0.25 视觉上仍大）→再缩到当前 1/4≈0.0625（**45×132**）「这次真的变小了」。清晰度无损（同一张 PNG，点开看大图）。
  - **固定高度规则（owner 最新要求）**：「保持它这个高度，如果消息太长了，把 scale 按比例减少」→ 缩略图**显示高度恒定 ~132px**，不随题面长度变化。`cards._thumb_hint(w,h)`：源 dsf=2 自然显示=源/2，`scale=132/(h/2)`，`hw=round((w/2)*scale)`、`hh=round((h/2)*scale)`（即 `hw=round(132*w/h)`、`hh≈132`）。1440×4226→45×132；更高的卡宽度按比例收窄、高度不变。`_png_size` 解析 PNG IHDR(bytes16:24) 取真实宽高；非 PNG/过短→默认 (45,132)。
  - **素材管理上传 + 发后即删**：重新引入 `rest.upload_media_image`（prepare→PUT presigned→complete，`/v2/media-images/*`）+ `rest.delete_media_image`；`sender.upload_markdown_image`→`(url,image_id)|None`、`delete_markdown_image`。发送成功后删源图释放 100/bot 配额（平台为已送达消息保留转存副本，删除不影响已发卡片）；**被删 url 不能在新发送中再引用**→每次发送重新上传（不缓存 url），但 PNG 本身仍走预渲染缓存（不重复渲染）。PUT 走 COS presigned（自带 query auth、无 QQBot 头，Content-Type 须等于 prepare 值）。
  - **实现**：`cards.send_problem_card` 重写——取 PNG（prerendered_png 或 render_card_png，try/except→None）→upload_markdown_image→`md=f"![题目 #{hw}px #{hh}px]({url})"`（**alt 仅通用标题，无 CF pid/标题**，防剧透身份泄露）→`send_group_markdown(md, keyboard=problem_action_keyboard())`→成功则 delete_markdown_image 返回 mid；任一环节失败降级 `send_group_text(fallback)`（纯文本，无按钮）。不再追发 `content=" "` 按钮消息。`/np`+`/pb` 共用此路径。判错/恭喜/force 三处本就是单条 markdown+按钮，无需改。
  - **测试**：FakeRest（test_cards + test_commands）加 upload_media_image/delete_media_image；fake_render/no_render fixture 同时 patch `forward_card.render_card_png` 与 `cards.render_card_png`（cards 直接 import 了 render_card_png，仅 patch forward_card 会漏→真渲染 Chromium 致挂起，已修）；test_cards 媒体路径改锁单条 markdown 缩略图+按钮+发后即删、新增 upload 失败降级/_png_size/_thumb_hint 固定高度/高图缩放 4 测试；test_commands /pb 文本降级（无追发）+ 预渲染缓存改锁 markdown 缩略图。全量 **674 passed**。AGENTS.md（Transport/Media/Buttons/预渲染节）+ README 按钮注记同步。
  - **待 owner**：真机目视生产题卡（缩略图固定高度 + 同气泡按钮 + 点开大图清晰）；清理探针留在实验群的旧缩放版本消息 + 素材库源图 image_id（owner 定夺后删）。PR #119 仍未合并。
- 恭喜消息加「再来一题」按钮（2026-11-20，owner 要求）：通过祝贺由单按钮 [跳转原题] 改为一排两按钮 **[跳转原题 → CF URL] + [再来一题 → `/np` enter=true]**（点击直接发 /np 换下一题；AC 后题已解出，裸 /np 即可）。新增 `keyboard.congrats_keyboard(pid)`：pid 可解析→两按钮对半分；不可解析→丢掉跳转、[再来一题] 占整行。submit `_send_scoreboard_success` 改用 congrats_keyboard（jump_original_keyboard 仍用于 /np --force 跳过通知，不变）。test_keyboard +2（两按钮/坏 pid 单按钮）、test_commands 恭喜断言改锁两按钮（type/data/enter）。AGENTS.md Buttons 节 + README 同步。PR #119 仍未合并。
- 官方题解卡去掉「来源」URL（2026-11-20，owner 真机反馈「图片里的大家又点不了，无人在意」）：`editorial_followup._editorial_payload` 删除 `来源: {tutorial_url}` 行，卡片 header 仅留 `📖 {pid} 官方题解`。连带清理死参数：`_editorial_payload(pid)` 与 `deliver_official_tutorial(group_openid, pid)` 不再收 editorial（调用方仍取 verified editorial 做 None 门控，只是不再传入）；3 处调用 + editorial_followup 的 OfficialEditorial import 同步移除。**保留** `tutorials.format_editorial_for_review` 的「来源：」行——那是 /review 喂给模型的英文题解上下文（仅模型可见，非用户卡片），URL 对模型有用。test_editorial_followup 改锁 header 无来源 + 4 处 deliver 调用去参。全量 **676 passed**。PR #119 仍未合并。
- 确认表情统一 @ 触发者（2026-11-20，owner「👀/👌 的时候 at 一下对应的人」+「看题解审核和 nonsense 的两个表情也要 at」）：四个确认表情 👀(ack)/👌(clear+feedback+sync)/🙅(offtopic)/🧐(二审) 全部 @ 触发用户。`sender.react(group, emoji, *, msg_id, user_openid="")`：带 user_openid → markdown `<qqbot-at-user id="…"/> <emoji>`（官方 @ 仅 markdown 渲染）；不带 → 退化为纯 emoji 文本。submit `_react(req, emoji)` 统一传 `req.user_openid`；sync/feedback 的 👌 调用补 `user_openid=ctx.user_openid`。🧐 仍仅表情无其它文字。test_official +`test_sender_react_mentions_user`（markdown 形状）保留无 user 纯文本用例；test_commands/test_feedback 全部表情断言改 `f"{at_tag()} <emoji>"`。AGENTS.md confirmations 节同步。
- clarify 回复加按钮（2026-11-20，owner「clarify 的回复后面也加两个按钮，一个是提交，一个是继续询问」）：新增 `keyboard.clarify_action_keyboard()` = 一排 [提交 `/sbm`、继续询问 `/clrf`]（均 type=2 enter=false 预填）。submit `_finalize_clarify` 正常回复 `_send_plain(..., keyboard=clarify_action_keyboard())`（offtopic 🙅 分支不带按钮）。test_keyboard +`test_clarify_action_keyboard_one_row_two_buttons`；test_commands clarify 回复断言锁按钮 labels/data/enter。AGENTS.md Buttons 接线同步。
- 无参按钮改 type=1 回调（2026-11-20，owner 要求与直发完全等价，见 D17）：再来一题(`/np`)、清空上下文(`/clear`) 由 type=2 enter=true 改 **type=1 回调**。新增 `rest.respond_interaction`(PUT `/interactions/{id}` `{code:0}`)、`sender.ack_interaction`、`keyboard.callback_button`；`events._interaction` 提取 interaction_type/scene/button_data/button_id（resolved 非 dict 防御）；`handlers._interaction_to_message` 先 ack 再合成等价群命令消息走同一 `process_event`（冷却/force 摩擦/busy 提示逐字一致，回复走主动配额，按 interaction_id 去重）；worker `_on_event` 对 interaction 也 `_ensure_group`。`congrats_keyboard` 再来一题、`reject_action_keyboard` 清空上下文改 callback_button（无 enter）。**测试**：test_eventlog FakeRest +`respond_interaction`，+11 回调等价用例（dispatch+ack/无需@/别名规范化/与手打共享冷却/点击间冷却/零冷却重复/interaction_id 去重/非11忽略/非group忽略/非slash忽略/缺身份忽略）；test_commands FakeRest +acks，+`test_interaction_clear_callback_equals_typed_clear`（真 /clear 回调清历史+👌+ack）；test_official +`test_sender_ack_interaction`/`test_rest_respond_interaction_puts_ack`，normalize interaction 用例改真实 resolved dict 并断言 button_data；test_keyboard +`test_callback_button_shape`，reject/congrats 断言改 type=1 无 enter。全量 **693 passed**。AGENTS.md（confirmations/Buttons/D17/Dispatch）+ DECISIONS D17 同步。PR #119 仍未合并（待 owner 真机目视按钮 + 回调冷却）。
- 未解出 /np 拒绝消息加 [查看当前题目] 回调按钮（2026-11-20，owner 真机反馈：拒绝消息「当前题目还没有人解出来呢～…可使用 /pb…」下面加按钮，「回调映射到 pb」）：新增 `keyboard.view_problem_keyboard()` = 单个整行 [查看当前题目] **type=1 回调 → `/pb`**（无参，与直发等价，复用 D17 合成 dispatch）。`newproblem._notify` 加 `keyboard=None` 透传（有 user→send_group_at 带 keyboard；无 user 但带 keyboard→send_group_markdown；否则纯文本）；未解出拒绝分支（`not force and has_unsolved_problem`）带 `view_problem_keyboard()`。busy/冷却通知不带按钮（不变）。test_keyboard +`test_view_problem_keyboard_single_callback`；test_commands +`test_unsolved_np_rejection_carries_view_problem_callback`（拒绝文案+按钮 type=1/data=/pb/无 enter，且点击回调 /pb 真重发题卡+ack）。全量 **695 passed**。AGENTS.md Buttons/D17 + README 同步。PR #119 仍未合并。
- 按钮回调 /clear 去掉 👌（2026-11-20，owner「使用按钮的 clear 可以不用回复👌，因为 qq 会给一个操作成功」）：type=1 回调点击后 QQ 客户端已给点击者「操作成功」toast，再发 👌 确认纯属冗余。`PendingRequest` 加 `from_callback: bool=False`；`enqueue_clear_request` 置 `from_callback=ctx.event.get("event_type")=="INTERACTION_CREATE"`；`_finalize_clear` 两处 👌（无 pid 早退 + 正常清除）均 `if not req.from_callback` 门控。手打 /clear 仍发 👌（无 toast）。test_commands 该用例更名 `test_interaction_clear_callback_clears_without_ok_ack`，断言 `sent_texts==[]`（无 👌）+ 历史已清 + ack `[("INT_CLEAR",0)]`；两个回调 dispatch 测试加 `registry.discover_commands()` 自给注册（修复孤立/-k 运行时全局 registry 为空导致 dispatch 静默丢弃的既有测试顺序脆弱性，非本次改动缺陷）。全量 **695 passed**。AGENTS.md（confirmations//clear 行/D17）+ README 同步。PR #119 仍未合并。
- clarify 兜底失败根因 + send_response_format 开关（2026-11-20，owner 真机「配了 ds 兜底这也能挂？」）：日志显示兜底接线**正常**——gemini-3.8-flash（api.site 代理）502 耗尽→切 deepseek-flash（官方），但 deepseek 两次都 `returned no text after cleanup finish_reason=stop reasoning_chars=514/154`（空 content）。**精确复现**（真实 CLARIFY_PROMPT + 858E 题面/摘要 + 同一问题）：同 payload 跑多次间歇返回纯空白 content（如 74 个空格），`strip_leaked_thinking` 清成空→判可重试失败，e2e `max_retries=1` 两次都撞上即耗尽。**A/B 对照定位触发器**：带 `response_format=json_object` 6/8 空（75%），去掉该模式 0/8 空（100% 正常）；thinking 开/关无关（ON 4/6、OFF 5/6 空，均带 json_object）。短 prompt 正常→是大 prompt + json_object 结构化输出模式让 deepseek-flash 退化。**修复**：新增 per-provider `send_response_format`（默认 true，对称既有 `send_thinking`；`llm_config.py` 字段+解析、`llm.py` payload 门控 `if response_format and provider.send_response_format`），e2e deepseek general 设 `send_response_format: false`。clarify/summary prompt 本就要求 JSON + `parse_json_with_llm_repair` 兜底解析，关模式安全；不做按模型名硬编码分支（遵 AGENTS.md「用 config 而非 per-model 分支」）。test_shared +`test_provider_send_response_format_false_suppresses_json_mode`、test_config +默认 true/解析 false 断言。全量 **697 passed**。config.example.yaml（deepseek-flash 注释示例+说明）+ AGENTS.md provider 字段节同步。**待 owner**：生产 config.yaml 的 deepseek general 兜底也应加 `send_response_format: false`（若用 deepseek-flash）；探针脚本 /tmp 不提交。PR #119 仍未合并。
- 复活 private judge 的通道选型核实（2026-11-20，owner 问「建频道走频道内私信能否实现之前的 private judge」）：对照官方文档（消息收发概述/发送私信/事件页）核实两条私聊通道，**结论：频道私信能接但对 private judge 很不友好；QQ 单聊 C2C 才是自然 fit**。硬数据——被动回复有效期/次数：单聊 **60min/4次**、群聊 60min/5次(admin50)、文字子频道 **5min**、频道私信 **5min**(被动无条数限制)；主动配额：单聊 **1000条/用户/天**(20/qpm,10/qps)、群 1000/群/天、频道私信 **2条/用户/天 + 200条/bot/天总计**、文字子频道 20/子频道/天。身份：C2C `user_openid` == 群 `member_openid`（知识库已实测跨群稳定）→ 会员门禁 + /sync 跨群可直接复用；频道私信用 guild `author.id`（另一套命名空间，需用户进 QQ频道，与群 openid 不互通）。intent：C2C 走 GROUP_AND_C2C=1<<25（**已订阅**）；频道私信需 DIRECT_MESSAGE=1<<12（未订阅）。**频道私信致命点**：judge 慢（e2e judge_timeout 7200s）常超 5min 被动窗口→被迫走主动，而主动仅 2/用户/天，多轮判题+AC 题解跟进秒耗尽；且沙箱不支持私信事件（需用户白名单调试）。**C2C 优势**：60min 被动窗口覆盖几乎所有 judge；1000/用户/天主动充裕；openid 互通；另有「群 msg_id→单聊私聊回复」捷径（要求用户与 bot 为好友，按群聊规则计、与群内回复共用 msg_id 次数）。当初砍私聊是**产品决定（纯群 bot）非能力不行**。**注意点**：C2C 富媒体上传接口与群聊不互通（长图需单独上传）；用户可在客户端关「允许主动发送」→主动一律失败；C2C markdown 是否支持 inline keyboard 按钮未实测。**待 owner 定夺**：是否复活 private judge、走 C2C（推荐）还是频道私信；本次仅能力核实，未改代码。PR #119 仍未合并。
- private judge 通道准入限制核实（2026-11-20，owner 追问「个人开发者单聊是否只能对至多 20 人开放、还要去管理端逐个加」）：对照 bot.qq.com 首页/平台入驻文档/机器人 FAQ/账号注册页。**「20」属实但是白名单上限，不是绝对天花板**：① 管理端「消息列表单聊用户白名单上限 20 条」（频道/群白名单 likewise 各 20）；② 「内部体验号码 20 个」用于开发内邀，配置后不受服务范围影响。机制=每场景「使用范围」可设**全量**或**白名单**：白名单模式需要在 q.qq.com 手动加具体 QQ 用户（≤20）；全量模式允许任何用户经机器人资料卡添加单聊，但**需通过首次上线审核**（白名单→全量切换也要审核，全量→白名单立即生效）。账号注册页（2025-06，部分过时——其「群聊仅企业」与 owner 个人主体群 bot 现状矛盾）措辞：**单聊【定向邀请】**、频道企业/个人均可。**结论**：当前未上线 dev 状态下，个人主体单聊≈定向邀请/体验白名单（≤20 手动加白）；要服务全群须上线审核拿「全量」，个人主体能否过审拿单聊全量文档未明示、需 owner 在自己管理端「发布设置/使用范围」实测确认。**对上一条推荐的修正**：C2C 在能力上仍是 private judge 最佳 fit（60min 被动/1000 主动/openid 互通），但**准入上个人主体可能被卡在 ≤20 白名单**，无法服务整群；除非上线拿全量或企业认证。频道私信同样受频道白名单 20 + 需进频道约束。本次仅核实，未改代码。PR #119 仍未合并。
- CF 比赛推送核查（2026-11-20，owner「这些天好像 bot 一直都没推送 codeforces 比赛，推送功能正常运作中吗」）：**功能正常，没推是因为窗口内确实没有比赛**。scheduler 每日 12:01 对已发现服务群跑 `contest_check`（日志实证：11-19 跑实验群A、11-20 跑实验群A+B 两群；11-18 无——当天 12:01 后才启动）。`check_contests_for_group`（shared.py:2037）拉 `contest.list`，筛 phase∈{BEFORE,CODING}、type∈{CF,ICPC}、排除 unrated/mirror/april fools/kotlin/practice、`start<=now+24h`；**无命中即静默 return（不打日志）**。核查当下 CF 日程：上一场 Round 1135 = **11-15 22:35**（早于 e2e 日志起点 11-18），下一场 Round 1136 = **11-21 22:35**。故 11-18/19/20 每天 12:01 检查时下一场都在 24h 窗口外（11-20 12:01 cutoff=11-21 12:01 < 1136 的 22:35）→正确静默。**11-21 12:01** cutoff=11-22 12:01，1136（22:35）进入窗口→将首次真正触发推送。发送路径=`send_group_markdown`（与所有卡片同一已验证通道）+ `<qqbot-at-everyone/>`；@all 需 bot 管理员（实验群已给）+ **每群每天 1 次**（contest_check 每日一次，符合）。**唯一未实测点**：@all markdown 真机投递在 e2e 从未发过（一直无内容可发）；机制有文档依据（C3：20261026 起文本+markdown 均支持 @everyone），风险低。如需确认可做一次受控 @all 探针（会 @ 全体，属可见动作，待 owner 同意）。本次仅核查，未改代码。PR #119 仍未合并。
- 老服务群热迁移启用（2026-11-20，owner「刚刚 bot 新进的这个群就是之前的服务群，可以开始配置渐进地转移数据」）：bot 17:48 新加入的群 `group_<服务群openid>` 即老服务群（不在三个实验群之列）。**配置**：/tmp/kouhai-e2e.yaml（非跟踪）启用 `legacy_migration.{enabled:true, group_openid:<服务群openid>, source_db:/tmp/kouhai-migration/legacy_source.db}`。**源 DB 核实**（task #33 产物，19MB，只读打开）：表结构与代码完全吻合——legacy_users 61 / legacy_problems 3262 / legacy_editorials 348 / legacy_solves 1044（order_floor=1044）/ legacy_submissions 2047 / nickname_index 70。**重启 e2e 启动实证**：`catalog loaded: 3262 problems, 348 editorials, order_floor=1044`；活库 problems 3269（+7 原生）/ editorials 360（+12 原生）/ group_order_floor[服务群]=1044（网关连接前落库，竞态闭合）；三群 discovered + gateway READY。**匹配率离线评估**：61 老用户中 51 唯一匹配 / 0 歧义 / 10 无匹配；恰 51 用户有 solves→所有活跃老用户都能唯一匹配，渐进迁移可干净落地（10 无匹配者无 solves，无数据可迁）。**机制**：dispatch 在 require-@ 门禁**之前**对每条服务群消息 fire-and-forget `schedule_migration`（昵称取 event.nickname/author.member_nickname/username），按 nickname_index 精确匹配老 QQ 号→原子幂等迁该成员 legacy_solves+submissions 进服务群（legacy_migration_map 守卫，ON CONFLICT DO NOTHING 不覆盖原生），私聊数据不迁（owner 决定）。**待真机**：重启后尚无成员发言（map=0）；老成员下次发言即触发，监视日志确认首批 migrated/无歧义。本次仅配置+启动核实，未改迁移代码。PR #119 仍未合并。
- 热迁移改名失配开放项（2026-11-20，真机）：首批迁移成功——owner（/range 触发）唯一匹配其老 QQ 号〈老昵称〉，迁入 20 solves + 48 submissions（活库实证，服务群榜单 #1（owner）20 题/46.58 分；max order_num=1011<floor 1044 不撞号）。**但改名成员失配**：某成员当前群昵称〈当前昵称〉多次发言均 `no unique match`（精确匹配机制）。源库最接近老昵称〈老昵称〉→对应老 QQ 号（2 solves/13 submissions，nicknames_json 仅此一昵称）。**疑似同一人改名，但身份不确定→不自动迁**（遵「身份不确定绝不乱迁」原则，类比 editorial match=false）。**待 owner 确认** 该成员是否=该老 QQ 号；确认后在源库 nickname_index 为该老号加当前昵称别名（is_latest 处理需斟酌：避免与既有 latest 冲突致歧义），其下次发言即自动迁入。此类改名 case 预计还有（离线评估 10 个 no_match 用户，多无 solves；有 solves 的改名者需逐个确认）。**机制局限记录**：精确昵称匹配对改名零容忍；官方无 roster/history API 无法批量映射，只能按访问+昵称。若改名普遍，后续可考虑 owner 手动映射表或模糊匹配（需防误迁，暂不做）。本次仅记录，未改源库/代码。PR #119 仍未合并。
- smart_model 上游 reasoning_effort 档位实测校准（2026-11-20，owner「不确定上游是否支持 max，可能是 xhigh/ultra，要确认」）：对 e2e 四个 smart provider（均 api.site）做候选扫描 minimal/low/medium/high/xhigh/max/ultra（"hi" 探针，记录 HTTP+usage.reasoning_tokens+reasoning_content）。**结论：当前全 max 配置正确，max 就是真正最高档，无需改。**
  - **astra(gpt-6.2-astra)/sol(gpt-6.2-sol)**：非法值 minimal 的 400 **回显完整允许枚举**「use low, medium, high, xhigh or max」→ 支持集 {low,medium,high,xhigh,max}，**max 是顶格**；ultra→502/500（不支持）。实证 max 真正 engages 推理：low/medium/high/xhigh 的 reasoning_tokens=None、ct=11，**max 时 rt=26(astra)/37(sol)**。xhigh 虽支持但低于 max。
  - **carbon(gemini-4-carbon)**：ultra/__probe_invalid__→干净 400「unsupported reasoning effort」（校验拒绝）；low/medium/high/xhigh/max→502 Cloudflare 网关 HTML（**校验通过后上游故障**）→ max 是合法值（通过校验），最高合法档=max。**附带发现：carbon 路由当前持续 502（max/high/xhigh 9/9 全挂），gemini via api.site 不可用**——与 clarify 兜底那次 gemini-3.8-flash 502 同源；smart 链 astra→sol→carbon→deepseek 仍靠 astra/sol 健康工作，carbon 兜底目前形同虚设（可用性问题，非 effort 问题，待观察/owner 知悉）。
  - **deepseek(deepseek-v4-flash)**：所有值（含 ultra/minimal）均 200 无校验，reasoning_content 长度无单调规律（minimal123/low93/medium85/high107/xhigh164/max81/ultra0）→**不吃 reasoning_effort**（DeepSeek 自有思考机制，恒推理），max 无害但惰性。保留 max 即可。
  - **方法论**：非法值回显枚举（astra/sol 明确）+ 校验拒绝(400) vs 上游故障(502) 区分（carbon）+ reasoning_tokens 实证（max 才推理）。未降任何推理强度（遵 AGENTS.md）。探针脚本 /tmp 不提交。
- 热迁移空昵称成员观察（2026-11-20）：继按钮成员A 后，按钮成员B 也下发**空昵称**致 no unique match。两例不同成员均空昵称——疑似部分成员官方事件未带 member_nickname/username（无群名片？）。空昵称无法精确匹配（设计如此）。暂观察是否成规律；若普遍需查官方事件昵称字段下发条件或补充身份捕获途径。改名成员已 owner 确认身份并迁入（见下）。
- 改名成员迁移完成（2026-11-20，owner 确认「是的，就是他」）：该成员（当前昵称〈当前昵称〉）= 其老 QQ 号。**处置**：源库 nickname_index 加当前昵称别名（is_latest=1）、老昵称〈老昵称〉置 is_latest=0（反映改名，any 兜底仍可匹配）；经**真实代码路径** `legacy_migration.maybe_migrate`（独立进程 get_database 幂等 schema+WAL 并发安全）立即触发迁入 2 solves+13 submissions（并入既有 1 原生 solve+4 原生 subs→共 3 solves/17 subs），map 行记当前昵称、users.nickname 更新为当前名。榜单 #3（改名成员）3 题/4.57 分。累计 3 映射/36 migrated solves（owner20+成员乙14+改名成员2）。后续发言 map 已存在→早退不重复迁、不再刷 no-match 日志。**机制扩展记录**：改名失配可经 owner 确认后加昵称别名解决（源库本地运行时数据，不提交）；身份不确定绝不自动猜迁。PR #119 仍未合并。
- 空昵称根因修正＝按钮回调（2026-11-20，owner「那两个成员好像没有@BOT，他也能收到吗」）：**修正上一条「空昵称=无群名片」猜测**。日志实证按钮成员A 18:06 `→ /newproblem`、按钮成员B 18:31 `→ /clear`——二者是**点了回调按钮**（再来一题/清空上下文），非手打 @bot 命令。回调 INTERACTION_CREATE 经 `_interaction_to_message` 合成等价命令（at_bot=True，绕过 @ 门禁，D17），故「没 @bot 也收到」。**空昵称真因**：官方 interaction 事件不带点击者昵称（`official/events.py:_interaction` 仅提取 group_member_openid/button_data/button_id，无 author/member_nickname）→ 合成消息写死 `nickname:""`、`author:{}`（`handlers/__init__.py:166-167`）→ 迁移精确昵称匹配落空。**结论**：① 按钮回调无需 @bot 即被接收并触发命令+迁移钩子；② 普通非@聊天在群开「获取群内全部消息」时也接收，迁移捕获在 @ 门禁之前对每条消息生效；③ **只点按钮从不打字的成员无法自动迁移**（昵称不下发），打过任何命令/聊天后即可迁——官方 API 固有限制（无 roster、interaction 无昵称）。**可选改进（待 owner 定夺，未实现）**：回调昵称为空时回退查 users 表/group_solves 快照里该 openid 的已存昵称，使已知成员的按钮点击也能触发迁移；未知纯按钮成员仍需先打字一次。涉及迁移正确性路径，暂记录不擅自改。PR #119 仍未合并。
- 全量消息加速迁移（2026-11-20，owner「把获取全部消息打开是不是有帮助，帮助尽快迁移」）：**实证当前 服务群 全量消息为关**——日志分析「无对应命令 dispatch 的迁移事件=0」，所有收到的消息都是 @bot 命令或按钮回调，普通聊天未下发。**结论：开启「获取群内全部消息」(GROUP_MESSAGE_CREATE) 显著加速迁移**——迁移钩子在 @ 门禁之前对每条消息生效，成员任意普通聊天即下发昵称触发匹配迁移，无需 @bot/打命令，覆盖面+速度大增（尤其只聊天不用命令者）。@ 门禁仍挡非@命令响应（不变吵）；迁移 fire-and-forget/幂等/低开销。bot 侧 intent 已订阅（1<<25 GROUP_AND_C2C 含 GROUP_MESSAGE_CREATE），属平台/群侧权限，owner 在管理端给 服务群 开启即可（必要时重连 renegotiate），无需改代码。config.example 早已注明此依赖。开启后验证信号＝出现「仅迁移无命令」事件。纯按钮空昵称限制仍在但全量后大概率经聊天补迁。本次仅核实+建议，未改代码/未重启。PR #119 仍未合并。
- 按钮空昵称第3例（2026-11-20 18:41，监控）：新成员（按钮成员C）点「清空上下文」(/clear) 按钮回调，nickname='' → no unique match，未迁移。再次确认全量消息仍关（此为 interaction 非聊天）。强化结论：纯按钮成员需全量消息先经聊天捕获昵称、或 @bot 命令方可迁；callback 昵称回退（查 users 表已存昵称）与全量消息协同可补此洞——仍属待 owner 的可选增强，未实现。
- 全量消息开启并验证生效（2026-11-20 18:51，owner「我刚才已经把全量消息打开了」+ 监控）：owner 在管理端给服务群开启「获取群内全部消息」后，监控立即捕获**普通聊天触发的迁移**（带昵称的 GROUP_MESSAGE_CREATE，非命令/按钮）——出现「仅迁移无命令」事件，正是先前预测的生效信号。一名活跃老成员经聊天即迁入 50 solves/116 submissions（此前仅 @bot 命令/按钮触发，覆盖面大增）；另有无匹配昵称成员（离线评估的 no_match 用户，无 solves，属预期）。**结论坐实**：全量消息显著加速渐进迁移，成员任意聊天即触发昵称匹配。按钮回调空昵称限制 owner 已定「不管了」（纯按钮成员靠聊天补迁）。具体 openid/QQ/昵称仅存本地运行时日志与源库，不入跟踪文件。本次仅运行时验证，未改代码。
- /np 冷却提示优化（2026-11-20，owner「刷新太频繁啦，等 0 秒再试哦～ 这个提示能优化下吗」）：根因＝`newproblem.py` 冷却拒绝用 `int(cooldown - elapsed)` 向零截断，剩余 <1s 时显示「等 0 秒」（e2e 冷却 10s 高频复现；生产 300s 亦可能出现）。**修复**：① `math.ceil` 向上取整，剩余 >0 时至少 1 秒，根治「0 秒」；② 新增 `_format_wait(seconds)` 友好时长——≥60s 显示「X 分钟」（整分）/「X 分 Y 秒」，否则「N 秒」，生产 300s 冷却→「等 5 分钟再试哦～」而非「等 300 秒」。消息其余措辞不变。回归测试 3 例（`_format_wait` 纯函数边界 1/59/60/90/299/300；亚秒剩余→「等 1 秒」且无「0 秒」；生产尺度→「等 5 分钟」）。全量 **700 passed**。新分支 fix/np-cooldown-wait + 独立 PR（PR #119 已 squash 合并 a0c73a8）。
- 题卡/题解配图缺失修复（2026-11-21，owner 截图反馈「样例这里没爬到图，用户体验很差」＋「题解也应该有这个问题」）：**根因**＝题面 `images` 明明已抓到（如 2155E 的 Figure 1–4，`kind=graphic`），但长图卡片只渲染摘要/样例/样例解释文本，从未消费 `statement["images"]`；题解侧更早就在 `html_to_markdownish` 里把 `<img>` 整段丢弃，抽取器/翻译/交付全程无图。**修复**：① 新增 `render/figures.py`——远程配图按 sha1 落盘 `render_cache/statement_images/` 后以 `data:` URI 嵌入长图（离线确定性渲染，与 snake 图同法），去重、上限 12、失败丢弃但保留源序号（`图 N` 不因缺图错位），`problem_preparation` 在后台预备阶段预热缓存；② 题卡在摘要气泡后插入 `题目配图` 画廊节点，`/pb` 复用持久化的 `figure_srcs`（`daily_msg`/card_json 同步保存），卡片版本号升至 2 使旧预渲染 PNG 失效；③ 题解 `scrape_cf_tutorial` 在 markdownish 转换中保留 `<img>` 为 `[[CFIMG_n]]` 标记（公式图/icon 除外，相对 src 绝对化），`cf_tutorial_agent` 抽取器选中 span 内的标记映射回 `Section.images` 并在翻译前剥除标记，`editorial_content`/bundle 透传 images，`deliver_official_tutorial` 在题解文本后追加 `题解配图` 画廊并在纯文本兜底中注明张数。**验证（遵守 owner「不要在任何群实际发送」约束，仅本地渲染）**：e2e 数据中 2155E 真实语句渲出 4 张图题卡；真实跑 `prepare_editorial(2155E)`（codeforces blog entry 实测命中 1 张配图）渲染题解卡并目视确认。新增 `tests/test_figures.py`＋cards/scrape/agent/editorial-followup 用例，全量 **721 passed**。分支 fix/np-figures，待 owner 确认渲染效果后开 PR。
- 配图改为**内联**（2026-11-22，owner 否决画廊版「莫名其妙给我发个题目配图…你倒是把东西发全了…看个样例解释还得上下拉着看」）：上一版把图做成摘要后的 `题目配图` / 题解末尾的 `题解配图` 画廊，图与正文脱节、也不知道每张图是什么。**重设计**＝图必须贴在提到它的句子旁边。① 题面：`fetcher.extract_figure_passage` 从原始 HTML 截出解释配图的那段正文（如 2155E 的「The first and seventh example tests… Figure 1… Figure 4」），图位置写成 `[[FIG_n]]`、同时采集 `figure_captions`（`Figure N`）；`problems.statement_json` 持久化 `figure_passage`，旧缓存用 `_figure_passage_collected` 触发重爬。② 翻译：`shared.translate_figure_passage` 要求占位符原样保留；输出做确定性校验（数量+顺序一致），LLM 丢标记时按标记切段重译（`_translate_figure_segments`），仍失败则回退原文。题解翻译同样校验并支持分段修复。③ 渲染：`cards._figure_passage_nodes` 按标记切分 passage，每个文字块一个气泡，紧跟该块引用到的图（两列网格、`图 N` 说明）；纯文本兜底用 `配图：图 1、图 2`。`/pb` 从 `card_json`（旧帖则从 statement）重建同款内联 payload。④ 题解：`localize_figure_markers` 把 blog 索引 `[[CFIMG_k]]` 重写为去重后的 1-based `[[FIG_n]]` 并返回 `Section.images`；`editorial_followup._inline_figure_nodes` 同法交错文本+图节点；仅当有图但无标记时才退回末尾画廊。**验证**：遵守「不要在任何群实际发送消息测试」，仅本地渲染 `/tmp/demo_problem_card.png`（2155E：讲解段落→4 图→收尾句）与 `/tmp/demo_editorial_card.png`（「下面是一个示例：」→图 1→复杂度），交 owner 目视确认。新增 `test_fetcher_figures` / `test_figure_translation` / cards / editorial-followup / scrape 用例，全量 **736 passed**。
- 配图内联扩展到样例解释 + 兜底署名（2026-11-22，owner「别换个题渲染效果又烂完了」＋「把这个兜底改成 KouhaiBot」）：审计缓存题面发现 6 道有图题里只有 2155E 能拿到描述段 passage，其余（1856E1/115C/768F/2108D/592D）的图都嵌入在 **Note（样例解释）** 里，旧流程按标签剥离把 `<img>` 全丢，只剩末尾画廊——正是 owner 否决的形态。**修复**：① `fetcher.extract_figure_notes` 在 Note 区域保留图形位置为 `[[FIG_n]]`（与 `statement_figures` 同一序号，去图注单元格），picker 注释提取优先使用它；缓存加 `_note_figures_collected` 标记，旧有图缓存自动重爬一次。② `translate_sample_notes` 加入占位符保留规则 + 确定性校验（数量/顺序），丢失时按标记切段重译，仍失败返回 `None` 让上层回退**原文（含标记）**，保证图仍在原位。③ 卡片把带标记的 `notes_message` 交给同一 `_figure_passage_nodes` 交错渲染；有标记时不再追加末尾画廊，纯文本兜底 `strip_figure_markers` 输出「（图 N）」。④ 卡版号 4 使旧预渲染失效；所有 `sender.bot_name or "算法助手"` 兜底改为 `"KouhaiBot"`（README 示例同步）。**验证（仍未向任何群发送）**：1856E1 真实页面 → 3 张树图分别嵌在「第一个/第三个/第四个测试」句子之后，中文译文标记完整；2155E 原图复渲不变；全量 **744 passed**。
- 题卡回归简洁：样例解释改为 LLM 纯文本生成（2026-11-22，owner 否决「简洁题面→突然长篇讲图→解释」）：2155E 的图其实在题面里，但描述的正是样例内容，内联/画廊两种形态都会把题卡撑得很长。**新形态固定为**：简洁题面（题意简述）→ 样例 → 样例解释（纯文本）。**规则**：原题有 Notes 才生成样例解释；没有 Notes 就不生成、不加节点。生成是**一个** `shared.generate_sample_explanation` 请求，携带完整题面、全部题面配图（multimodal）、全部样例、原 Notes、已生成的题意简述，要求模型把 Notes 依赖的图**用文字讲清楚**（树的节点/边、棋盘布局、箭头操作等），禁止「见图 N」、`[[FIG_n]]`、`[[IMAGE_n]]` 和任何图片语法；长度约等于原 Notes（上限 ≈1.5×，超长一次压缩重试），保留原文枚举/数值，纯文本，Unicode 上下标复用 summary 的白名单。**移除**：题面 passage 抽取/翻译、题面与 Notes 图的内联渲染、预渲染图缓存字段、`/pb` 的图回退；`statement["images"]` 只作为 LLM 输入。旧 READY 槽/旧卡片仍带 `[[FIG_n]]` 时，生成前剥离、渲染前打上「（图 N）」兜底，绝不泄漏占位符。生成失败重试 2 次后跳过节点（与原翻译失败策略一致）。**题解图不动**：editorial 仍按 `[[FIG_n]]` 交错渲染。验证：未向任何群发消息；单测覆盖「无 Notes 不调用」「素材/图/summary 透传」「旧标记剥离」「超长压缩」「卡片标记兜底」，全量 **731 passed**；用 e2e 数据真实跑 1856E1（树图+排列）/115C（管道方向）/768F（分栈概率 2/3）/2108D（六组样例+配图数组）/592D（网格最短路）本地渲卡目视确认：图内容均转成文字、无「图 N」泄漏、长度约为原 Notes 的 1–1.5 倍，2108D 最长但仍在限内；owner 确认后合并。
