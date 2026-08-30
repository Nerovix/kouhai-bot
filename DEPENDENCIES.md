# 依赖与安装教程

## 系统要求

- Python 3.11+
- Docker（用于运行 NapCat）
- NapCat QQ 客户端（外部依赖，需自行部署）

## Python 依赖

本项目使用 [uv](https://docs.astral.sh/uv/) 管理依赖。

### 安装 uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 安装项目依赖

```bash
cd kouhai-bot
uv sync
```

主要依赖：
- `websockets>=12.0` — NapCat WebSocket 通信
- `aiohttp>=3.9` — NapCat HTTP API 异步请求
- `cloudscraper>=1.2` — 绕过 Cloudflare 爬取 CF
- `Pillow>=10.0` — 公式图片预处理

## NapCat 配置

NapCat 是 QQ NT 的 Bot 框架，提供 OneBot11 兼容接口。这是一个外部依赖，需要你自行部署。推荐使用 Docker。

### Docker 安装

在任意位置创建 NapCat 配置目录（例如 `~/napcat`），并编写 `docker-compose.yml`：

```yaml
version: '3'
services:
  napcat:
    image: mlikiowa/napcat-docker:latest
    container_name: napcat
    environment:
      - ACCOUNT=你的QQ号
    ports:
      - "3000:3000"  # HTTP API
      - "6099:6099"  # WebUI
    volumes:
      - ./config:/usr/src/app/napcat/config
    restart: unless-stopped
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

### 配置反向 WebSocket

在 NapCat 配置目录下的 `config/onebot11.json` 中配置：

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
        "url": "ws://host.docker.internal:8097",
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

**重要说明：**
- `url` 中的 `host.docker.internal` 指向宿主机，**不能用 `127.0.0.1`**
- 端口 `8097` 需要与 `config.yaml` 中的 `napcat_ws_port` 一致
- `extra_hosts` 配置确保 Docker 容器能解析 `host.docker.internal`

### 启动 NapCat

```bash
cd ~/napcat  # 或你的 NapCat 配置目录
docker-compose up -d
```

### 首次登录

NapCat 首次启动时需要扫码登录 QQ。查看日志获取二维码：

```bash
docker logs -f napcat
```

登录成功后 token 会持久化，后续重启无需重新登录。

### 验证 NapCat

```bash
# 检查 HTTP API
curl -X POST http://127.0.0.1:3000/get_status \
  -H 'Content-Type: application/json' -d '{}'

# 应返回 {"status":"ok","data":{"online":true,"good":true}}
```

## 验证安装

```bash
# 测试导入
cd kouhai-bot
uv run python -c "from kouhai_bot.config import get_config; print(get_config().bot_qq)"

# 运行测试
uv run python -m pytest tests/ -v

# 启动 bot
uv run start
```

## 常见问题

### 1. NapCat 连不上 bot

**症状：** bot 日志显示 `NapCat connected from ...` 但消息不响应

**解决：** 检查 `napcat_ws_host` 是否为 `0.0.0.0`（不能用 `127.0.0.1`）

### 2. Docker 容器无法解析 host.docker.internal

**症状：** NapCat 日志显示 `getaddrinfo ENOTFOUND host.docker.internal`

**解决：** 确保 `docker-compose.yml` 中有 `extra_hosts` 配置

### 3. 端口冲突

**症状：** `uv run start` 失败，提示 `address already in use`

**解决：** 检查 `config.yaml` 中的 `napcat_ws_port` 是否与 NapCat 的 WebSocket server 端口冲突。NapCat 的 WebSocket **server** 端口（如 8095）和 bot 的 WebSocket **listen** 端口（如 8097）不能相同。

### 4. NapCat 每次重启都要扫码

**解决：** 在 `docker-compose.yml` 中设置 `ACCOUNT` 环境变量，格式为 QQ 号
