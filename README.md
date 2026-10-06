# Nori 后端（Nori Backend）

[Nori](https://github.com/yyyyymmmmm/nori-ios) 的自部署后端：一个服务，对外一个 "Hermes 服务" API 面。

## 定位

Nori 对标 Muse——个人 AI 助手，但**你拥有全部基础设施**。后端跑在你自己的 NAS/服务器/Docker 上，对话、记忆、健康数据不出你的内网。

## 架构

```
┌─────────────────────────────────────────┐
│              Hermes 服务                 │
│  ┌──────────────┐    ┌───────────────┐  │
│  │ Python 应用层 │───▶│ Hermes 引擎   │  │
│  │ (本仓库)      │    │ (Docker 镜像) │  │
│  │              │    │ nousresearch/ │  │
│  │ 会话/记忆/   │    │ hermes-agent  │  │
│  │ 任务/TTS/    │    │               │  │
│  │ 文件/看板    │    │ 跑模型+agent  │  │
│  │              │    │ loop          │  │
│  └──────────────┘    └───────────────┘  │
└─────────────────────────────────────────┘
```

- **Python 应用层**（本仓库，纯标准库为主）：所有业务 API。
- **Hermes 引擎**：OpenAI 兼容接口（`:8642`），上游地址/key 是服务端内部配置（`STREAM_HERMES_URL`/`STREAM_HERMES_KEY`），App 端不感知。

**原则**：用户能配的东西（TTS key、模型、Home Assistant）全走 API、App 内完成，零 SSH。

## ✨ 核心 API

| 前缀 | 说明 |
|---|---|
| `/api/chat` | AI 对话（流式） |
| `/api/hermes/models` | 模型列表（按服务商分组） |
| `/api/hermes/model` | 切换模型（即时生效） |
| `/api/hermes/providers` | 服务商增删改 |
| `/api/tts` | 神经 TTS（小米/智谱/阶跃） |
| `/api/tts/key` | TTS 厂商 key 配置 |
| `/api/connections` | 连接状态（HA 等） |
| `/api/memory` | AI 记忆 |
| `/api/health` | 健康数据聚合 |
| `/api/selfupdate` | 一键更新（备份→更新→健康检查→失败回滚） |

## 🚀 部署

```bash
git clone https://github.com/yyyyymmmmm/nori-backend.git
cd nori-backend
bash install.sh
```

安装脚本引导设置访问密码与 Hermes 上游，生成 `.env`，构建并启动容器，最后健康检查。

## 🔄 更新

**推荐**：Nori App → 设置 → 后端更新 → 一键更新（自动备份 → 更新 → 健康检查 → 失败回滚）。

手动：
```bash
./update.sh            # 更新 + 重建（自动备份 data/）
./update.sh --check    # 只检查不更新
```

`.env` 和 `data/` 始终保留，只替换代码。

## 🔧 开发

- 单文件为主（`backend/stream_api.py`），纯标准库 + `PyYAML`
- 新增接口参考 `/api/tts/key` 写法（鉴权 + 原子写配置 + 0o600）
- Hermes 相关走 `hermes_upstream.py`（多上游）/ `hermes_api.py`（路由）
- 提交前 `python3 -c "import ast; ast.parse(open('backend/stream_api.py').read())"`
