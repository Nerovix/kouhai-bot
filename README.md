# Kouhai Bot

<p align="center">
  <img src="snake_trio.jpg" width="400" alt="snake trio mascot">
</p>

口嗨 Bot 是一个 QQ 群算法竞赛助手：每天推一道 CF 题，大家在群里口胡做法，Bot 负责判题、答疑、复盘、记榜，也会顺手发比赛预告。

它不是 OJ。它更像一个不太会睡觉的助教：听懂你的思路，指出缺口，题做出来后再陪你复盘。

---

## 开始配置！

### 1. 准备环境

先 clone 仓库。

```bash
git clone https://github.com/Nerovix/kouhai-bot.git
cd kouhai-bot
```

项目用 `uv` 管依赖：

```bash
uv sync
```

你还需要一个 QQ 账号，以及 NapCat。

> NapCat 可以简单理解成“把 QQ 变成 API”。Bot 自己监听一个反向 WebSocket 端口，NapCat 连上来；Bot 发消息时再调用 NapCat 的 HTTP API。

如果你第一次配 NapCat，先看 `DEPENDENCIES.md`。Docker 部署时特别注意：`napcat_ws_host` 要写 `0.0.0.0`，不要写 `127.0.0.1`，否则容器连不到宿主机上的 bot。

### 2. 写配置

配置文件是 `config.yaml`，不会被提交到 git。

```bash
cp config.example.yaml config.yaml
```

打开 `config.yaml` 后，先填这些：

- `bot_qq`：Bot 的 QQ 号
- `current_group`：Bot 服务的群号
- `napcat_ws_port`：Bot 监听给 NapCat 连的端口
- `napcat_http_host` / `napcat_http_port`：NapCat 的 HTTP API 地址
- `llm.providers`：判题、答疑、复盘用的模型
- `qwen`：识别 CF 题面里的公式图片

如果你在 Docker 里跑 NapCat，推荐：

```yaml
napcat_ws_host: "0.0.0.0"
napcat_ws_port: 8098
napcat_http_host: "127.0.0.1"
napcat_http_port: 3000
```

NapCat 里要同时开：

- `httpServers`：给 Bot 发消息用，通常是 `0.0.0.0:3000`
- `websocketClients`：反向连到 Bot，例如 `ws://host.docker.internal:8098`

不要让 Bot 的 `napcat_ws_port` 和 NapCat 自己发布出来的 WebSocket server 端口撞车。撞了的话 `uv run start` 会直接失败。

### 3. 配模型

Bot 需要两类模型：

- LLM：判 `/submit`、回答 `/clarify`、做 `/review`、生成题目简介
- Qwen-VL：把 CF 题面里渲染成图片的公式读出来

`llm.providers` 是 fallback 列表。Bot 会从上到下尝试，前面的挂了再换后面的。

```yaml
llm:
  providers:
    - name: openai
      api_key: "..."
      base_url: "https://api.openai.com/v1"
      model: "gpt-5.5"
      reasoning_effort: "high"
      model_tag: "『֎AI』"

    - name: deepseek
      api_key: "..."
      base_url: "https://api.deepseek.com"
      model: "deepseek-v4-pro"
      model_tag: "『🐳』"
```

`model_tag` 会贴在 Bot 的 LLM 回复尾巴上，让群友知道这次是谁在干活，~~方便精准开骂~~。

`qwen-vl-max` 目前读公式效果不错，阿里云免费额度也比较够用：

```yaml
qwen:
  api_key: "..."
  base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
  model: "qwen-vl-max"
```

### 4. 启动

```bash
uv run start
```

常用命令：

```bash
uv run status
uv run restart
uv run stop
```

日志在：

```text
~/.kouhai-bot/logs/<group_id>/YYYY-MM-DD.log
```

健康状态大概长这样：

- `uv run status` 里 `occupied=yes`
- 日志里有 `NapCat connected from ...`
- NapCat 日志里能看到反向 WebSocket 连到了你的 `napcat_ws_port`
- `curl http://127.0.0.1:3000/get_status` 能拿到 `"online": true`

### 5. 拉进群

把 Bot 拉进群，发：

```text
/help
```

不需要 @ Bot。

Bot 也会自动处理好友请求，但只会通过当前服务群成员的申请。不是群友、查不到群成员、请求格式不对，都不会放行。

## 群里怎么用

常用指令：

- `/problem` 或 `/pb`：重发当前题
- `/newproblem` 或 `/np`：当前题已解出时刷一道新题
- `/newproblem --force`：当前题没解出也强制换题
- `/submit 你的做法` 或 `/sbm 你的做法`：提交口胡做法
- `/clarify 你的问题` 或 `/clrf 你的问题`：澄清题面细节，不剧透做法
- `/review 你的问题` 或 `/rv 你的问题`：复盘已经解出的题
- `/clear`：清掉自己在当前题上的 submit / clarify / review 上下文
- `/tag`：看当前题标签
- `/scoreboard`：看累计榜
- `/status`：看 Bot 现在有没有正在处理的活
- `/help`：看完整帮助

`/submit` 不是“关键字匹配”。它会看你在这题上的历史对话，判断你的方案能不能补全成正确做法，以及关键点有没有说够。

Bot 开始处理 LLM 请求时会给你点一个“眼睛”表情。如果它觉得你在纯捣乱，可能只回一个摇手指表情，不保存这次上下文。

## private judge

私聊 Bot 可以单独做题，不影响群榜。

适合这些场景：

- 想自己练，不想在群里剧透
- 打星用户还在提交等待期，但想先把做法交给 Bot 看
- 想选一道 CF 题单独问答、提交、复盘

私聊里先选题：

```text
/setproblem
/setproblem 2234B
/setproblem https://codeforces.com/contest/2233/problem/F
/setproblem random
```

空参数表示使用当前群题。也可以引用一张题目卡片后发 `/setproblem`。

私聊可用：

- `/problem`
- `/tag`
- `/submit`
- `/clarify`
- `/review`
- `/clear`
- `/sync`
- `/testcd`
- `/status`
- `/help`

private judge 通过不会自动加群榜。只有“当前群题”可以在群里 `/sync` 同步回来；如果群里已经有人先过了，就只同步历史，不再加分。

注意 `/sync` 是“另一侧覆盖当前侧”。群里发 `/sync` 是 private → 群；私聊发 `/sync` 是群 → private。用之前确认一下方向。

## 每天自动发生什么

默认每天中午 12 点：

- 如果当前题已解出，Bot 发新题
- 如果当前题还没解出，Bot 提醒大家继续肝
- 发前一天 04:00 到今天 04:00 的小统计
- 检查未来 24 小时 CF 比赛并预告

比赛预告会尝试 @全体成员，所以建议给 Bot 管理员。

如果你提前爬好了官方题解，第一位 AC 后 Bot 会把中文题解发到群里。`/review` 也会用官方题解帮你对照思路，但不会在题还没解出时剧透。

## 可选功能

### 打星

`user_groups` 可以把一部分用户放到“打星”组里，单独记榜，或者设置题目发布后的提交等待。

如果开启动态等待：

- 这题是你先做出来的，下一题多等一会
- 不是你做出来的，等待时间慢慢降回最低值
- 等待期内的群 `/submit` 会转到 private judge

这是为了让新人有更多上手机会，不是为了惩罚会做题的人。

### 宵禁

`curfew` 可以让 `/submit` 在每天某个时间段暂停。其他命令不受影响。

比如：

```yaml
curfew:
  start_hour: 22
  duration_hours: 6
```

表示 22:00 到第二天 04:00 不接 `/submit`。该睡觉了，别哐哐口题。

### 题目范围

```yaml
problem:
  min_rating: 2600
  max_rating: 2800
  daily_post_cron: "0 12 * * *"
```

题面里依赖非公式图片、图形、复杂 diagram 的题会被跳过。Bot 现在主要擅长读文字和公式。

## 常见问题

### NapCat 连不上 Bot

先查：

```bash
uv run status
docker logs --tail 160 napcat
docker exec napcat getent hosts host.docker.internal
```

常见原因：

- `napcat_ws_host` 写成了 `127.0.0.1`
- Docker 没有配置 `host.docker.internal`
- NapCat 的 `websocketClients[].url` 端口写错
- Bot 的 WS 端口和 NapCat 自己的端口撞了

### Bot 收得到消息，但发不出去

通常是 NapCat HTTP API 没开，或者端口不对。

```bash
curl -sS -X POST http://127.0.0.1:3000/get_status \
  -H 'Content-Type: application/json' -d '{}'
```

如果这里不通，先修 NapCat 的 `httpServers`。

### NapCat 每次重启都要扫码

Docker Compose 里设置：

```yaml
environment:
  ACCOUNT: "<bot_qq>"
```

### 模型经常超时

把 `llm.providers` 配成“主力模型 + 稳定 fallback”。Bot 会自动按顺序重试和切换。

DashScope / 阿里云百炼会走流式接口，长推理更不容易被 10 分钟 HTTP 限制卡死。

## 数据放在哪里

默认在：

```text
~/.kouhai-bot/
```

里面会有群状态、榜单、题面缓存、题解缓存、私聊判题历史、日志等。

`config.yaml` 不提交，真实 QQ 号、API key、群号不要写进仓库。

## 开销

这是纯 chatbot，token 开销不大。按我们的使用量，每天大概 1-2M tokens。

如果全走 DeepSeek V4 Pro，大约每日 2-3r。更贵的模型当然会更贵，建议配 fallback。

## 开发

如果你是 AI，请先读 `AGENTS.md`。『Human』

本项目主要入口在 `src/kouhai_bot/`。新增命令通常只需要在 `src/kouhai_bot/handlers/cmd/` 里加一个文件并注册，`/help` 会自动生成。

## Bot 的诞生

LLM 在算法竞赛上的能力已超出大部分选手。这可以被视为威胁，但也应被视为机会：学习高阶算法竞赛技巧变得前所未有地容易。

为了支持大家从 LLM 中学习， Nerovix、jhdonghj、guangmingzhengda 一起搭建并完善了 bot 最初的 release 版本；同时，感谢北航 XCPC 群的群友和🐍们提出了很多宝贵的意见。

## License

MIT
