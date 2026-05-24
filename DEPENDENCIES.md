# 依赖与安装教程

## 系统要求

- Python 3.11+
- NapCat QQ 客户端（Docker 或 Linux 部署）

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

NapCat 是 QQ NT 的 Bot 框架，提供 OneBot11 兼容接口。

### Docker 安装（推荐）

已有的 NapCat Docker 配置在 `napcat/` 目录下，包含 `docker-compose.yml` 和配置文件。

### 配置反向 WebSocket

编辑 NapCat 的 `onebot11.json`（通常位于 NapCat 配置目录下）：

```json
{
  "network": {
    "http": { "enable": true, "port": 3000 },
    "wsReverse": [{
      "enable": true,
      "url": "ws://127.0.0.1:8095"
    }]
  }
}
```

重启 NapCat 后，bot 启动时会自动接收连接。

### 首次登录

NapCat 首次启动时需要扫码登录 QQ。登录成功后 token 会持久化，后续重启无需重新登录。

## 验证安装

```bash
# 测试导入
cd kouhai-bot
uv run python -c "from kouhai_bot.config import get_config; print(get_config().bot_qq)"

# 运行测试
uv run python -m pytest tests/ -v

# 启动 bot
uv run python -m kouhai_bot.main
```
