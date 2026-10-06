# Nori Backend

[Nori iOS](https://github.com/yyyyymmmmm/nori-ios) 的自部署后端：一个服务，对外一个 "Hermes 服务" API 面。

Nori 对标 Muse —— 个人 AI 助手，但**你拥有全部基础设施**。后端跑在你自己的 NAS / 服务器 / Docker 上，对话、记忆、健康数据不出你的内网。

## 架构

```
┌──────────────────────────────────────────────────┐
│                   Hermes 服务                      │
│  ┌────────────────────┐    ┌───────────────────┐  │
│  │  Python 应用层      │───▶│  Hermes 引擎       │  │
│  │  (本仓库)           │    │  (Docker 镜像)     │  │
│  │                    │    │  nousresearch/    │  │
│  │  会话 / 记忆 / 任务  │    │  hermes-agent     │  │
│  │  文件 / TTS / 看板  │    │                   │  │
│  │  技能 / MCP / 目标  │    │  跑模型 + agent   │  │
│  │  知识库 / 资讯     │    │  loop             │  │
│  └────────────────────┘    └───────────────────┘  │
└──────────────────────────────────────────────────┘
```

- **Python 应用层**（本仓库，纯标准库为主）：所有业务 API，是 App 唯一打交道的一层。
- **Hermes 引擎**：OpenAI 兼容接口（`:8642`），跑模型和 agent loop。上游地址/key 是服务端内部配置（`STREAM_HERMES_URL` / `STREAM_HERMES_KEY`），App 端不感知、不配置。

**原则**：用户能配的东西（TTS key、模型、Home Assistant、技能、MCP）全走 API、App 内完成，**零 SSH**。Hermes 内部配置留在服务端。

## 核心 API

### 对话
| 端点 | 说明 |
|---|---|
| `POST /api/chat` | AI 对话（流式，SSE） |
| `GET /api/chat/sessions` | 会话列表 |
| `POST /api/chat/session` | 新建/重命名/删除会话 |

### 模型与服务商
| 端点 | 说明 |
|---|---|
| `GET /api/agent/hermes/models` | 模型列表（按服务商分组，带 selected 标记） |
| `POST /api/agent/hermes/model` | 切换模型（即时生效） |
| `POST /api/hermes/models/hide` | 隐藏不喜欢的模型（黑名单） |
| `GET /api/hermes/providers` | 服务商列表 |
| `POST /api/hermes/providers` | 增删服务商（默认服务商只读） |

### 记忆
| 端点 | 说明 |
|---|---|
| `GET /api/memory/list` | 记忆列表（含 Hermes 的 MEMORY.md/USER.md） |
| `POST /api/memory/add` | 新增记忆（同步写回 Hermes `MEMORY.md`） |
| `POST /api/memory/update` | 更新记忆 |
| `POST /api/memory/delete` | 删除记忆 |

### 技能
| 端点 | 说明 |
|---|---|
| `GET /api/agent/skills` | 技能清单（id/name/description/enabled） |
| `POST /api/agent/skills` | 启用/禁用技能（`{skill_id, enabled}`，改 `config.yaml` 并重启 gateway） |

### MCP
| 端点 | 说明 |
|---|---|
| `GET /api/mcp/servers` | MCP 服务器列表 |
| `POST /api/mcp/save` | 新增/更新 MCP 服务器 |
| `POST /api/mcp/delete` | 删除 MCP 服务器 |
| `GET /api/mcp/restart_status` | 重启状态 |

### 目标
| 端点 | 说明 |
|---|---|
| `POST /api/life/goal` | 建目标（AI 从聊天识别意图时调） |
| `PATCH /api/life/goal` | 更新目标/步骤 |
| `DELETE /api/life/goal?id=` | 删除目标 |
| `POST /api/life/goal/push_now` | 立即推进 |
| `POST /api/life/goal/report` | 进度回写（cron 调） |

### AI 内容
| 端点 | 说明 |
|---|---|
| `GET /api/agent/ideas` | AI 点子（prompt 存后端，失败返回 fallback） |
| `GET /api/agent/suggestions` | 今日建议 |
| `POST /api/agent/suggestions` | 带健康摘要的个性化建议（`{health: "..."}`） |

### TTS
| 端点 | 说明 |
|---|---|
| `GET /api/tts/key` | 是否已配置 key（只返回布尔，不回显明文） |
| `POST /api/tts/key` | 保存 TTS key（原子写 `config.yaml`，0600 权限） |
| `POST /api/tts/speak` | 神经语音合成（小米/智谱/阶跃） |

### 连接
| 端点 | 说明 |
|---|---|
| `GET /api/connections` | 连接状态（Home Assistant 等） |
| `POST /api/connections/ha` | 配置 Home Assistant |

### 任务
| 端点 | 说明 |
|---|---|
| `GET /api/agent/tasks` | 后台任务列表（Hermes async 适配层） |
| `POST /api/agent/tasks/cancel` | 取消任务 |

### 知识库
| 端点 | 说明 |
|---|---|
| `GET /api/kb/list` | 知识库列表 |
| `POST /api/kb/upload` | 上传文档 |
| `POST /api/kb/delete` | 删除文档 |

### 系统
| 端点 | 说明 |
|---|---|
| `GET /api/agent/settings` | Agent 设置（上下文压缩等） |
| `POST /api/agent/settings` | 更新设置 |
| `GET /api/agent/hermes/inspect/config` | Hermes 配置摘要（脱敏，不含明文 key） |
| `POST /api/selfupdate` | 一键更新（备份→更新→健康检查→失败回滚） |
| `GET /api/health` | 健康检查 |

## 部署

### 一键安装

```bash
git clone https://github.com/yyyyymmmmm/nori-backend.git
cd nori-backend
bash install.sh
```

安装脚本引导设置访问密码与 Hermes 上游，生成 `.env`，构建并启动容器，最后健康检查。

### 环境变量

| 变量 | 说明 |
|---|---|
| `STREAM_HERMES_URL` | Hermes 引擎地址（内部配置，App 不感知） |
| `STREAM_HERMES_KEY` | Hermes API key（**必须** = 容器实际的 `API_SERVER_KEY`） |
| `QL_AGENT_KEY` | 已废弃（曾误作网关 key，会 401） |

查容器实际 key：`docker exec <容器> env | grep -i API_SERVER`

### 更新

**推荐**：Nori App → 设置 → 后端更新 → 一键更新（自动备份 → 更新 → 健康检查 6 分钟 → 失败自动回滚，四步状态 App 内展示）。

手动：
```bash
./update.sh            # 更新 + 重建（自动备份 data/）
./update.sh --check    # 只检查不更新
```

`.env` 和 `data/` 始终保留，只替换代码。

## 开发

### 结构

- `backend/stream_api.py`：主服务，单文件为主
- `backend/hermes_api.py`：Hermes 相关路由（模型/技能/MCP/配置检查）
- `backend/memory_api.py`：记忆 API
- `backend/ai_content.py`：AI 内容生成（点子/建议，prompt 后端统一管理）
- `backend/goal_module.py`：目标管理
- `backend/hermes_inspect.py`：Hermes 容器检查（docker exec 读配置/记忆文件）
- `backend/hermes_upstream.py`：多上游管理
- 纯标准库 + `PyYAML`，无重型依赖

### 规范

- 新增接口参考 `/api/tts/key` 写法：鉴权 + 原子写配置 + 0o600 权限
- 不向 App 返回明文 key（只返回是否已配置）
- Hermes 相关走 `hermes_upstream.py`（多上游）/ `hermes_api.py`（路由）
- 提交前语法检查：`python3 -c "import ast; ast.parse(open('backend/stream_api.py').read())"`
- 默认分支 `main`，直接 push（自有仓库）

### 安全

- 所有写配置接口必须鉴权
- key 类写入用原子写 + 0600 权限
- 脱敏：配置检查接口不返回明文密钥
- 防路径穿越：Hermes 文件读写限制在允许目录
