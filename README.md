# Nori 后端（Nori Backend）

家庭 NAS 上的 AI 助手后端服务，纯 Python 标准库为主（第三方只有 `paramiko` / `cryptography` / `PyYAML` / `PyMuPDF`）。

## 架构

**一个后端，对外一个"Hermes 服务"**：

- **Python 应用层**（本仓库）：会话/任务/记忆/文件/TTS/看板等 API
- **Hermes 引擎**（`nousresearch/hermes-agent` Docker 镜像）：跑模型和 agent loop，OpenAI 兼容接口

App 只对接 Hermes 服务的一个 API 面，不感知内部两层。Hermes 上游地址/key 是服务端内部配置（`STREAM_HERMES_URL`/`STREAM_HERMES_KEY`），App 端不配置。

用户可配置项（TTS 密钥、模型选择、Home Assistant 等）全部走后端 API，App 内完成，**零 SSH**。

## ✨ 功能

| 端口 | 服务 | 说明 |
|---|---|---|
| 9125 | cron | 定时任务管理 |
| 9127 | ha | Home Assistant 代理（智能家居） |
| 9128 | logs | 系统日志（含崩溃上报） |
| 9129 | files | 文件管理（上传/下载/重命名） |
| 9131 | sessions | 会话同步 |
| 9132 | stream | AI 流式代理（后端持流，客户端轮询） |
| 9133 | auth | 登录/Token/密码管理 |
| 9135 | secrets | 凭据加密存储（Fernet） |
| 9136 | router | 路由器状态 / Clash 快捷启停 |
| 9137 | docker | Docker Compose 部署管理 |
| 9138 | kb | 知识库（文档检索注入） |
| 9139 | hw | NAS 硬件温度 |
| 9149 | local | 本地模型管理（Ollama 状态/开关/模型/拉取/删除） |
| 9140 | memory | AI 记忆 |
| 9141 | weather | 天气（Open-Meteo，无 key） |
| 9142 | scenes | 智能家居场景（AI 生成动作组，一键执行） |
| 9143 | asr | 语音转文字（faster-whisper 转写，供 App 上传录音） |
| 9145 | agent | Agent 记忆规则管理（「以后XX都用agent」话术） |
| 9146 | automation | 定时自动化（「X分钟后执行Y」延迟动作，到点自动执行后消失） |
| 9147 | push | 微信推送队列（enqueue/pending/done + 推送开关，X-Push-Token 鉴权） |
| — | inbox | 收件箱（外部 agent 出站消息，App 收件箱页轮询） |
| — | life | 生活数据（备忘录/便签/生活记录） |
| — | mcp | MCP 工具服务（App「MCP工具服务」配置写入 Hermes） |
| — | channel | 渠道管理（微信通道模型路由） |
| — | usage | 用量统计 |
| — | media | MEDIA: 协议转换（AI 回复内图片/文件渲染） |

## 🚀 快速开始（一键安装）

```bash
git clone https://github.com/yyyyymmmmm/nori-backend.git
cd nori-backend
bash install.sh
```

安装脚本会引导设置访问密码与上游 LLM 端点，生成 `.env`，构建并启动容器，最后做健康检查。

<details>
<summary>手动部署（不用脚本）</summary>

```bash
git clone https://github.com/yyyyymmmmm/nori-backend.git
cd nori-backend
# 编辑 docker-compose.yml 设置 QL_PASSWORD 等环境变量
docker compose up -d
```

</details>

服务启动后，各 API 以 `/api/<模块>/` 前缀对外暴露（建议用 nginx 反代统一入口）。

> ⚠️ **nginx 路由部署须知（踩坑记录）**：新增 API 模块时，必须在**全部入口**配置特化 location：
> - `8080`（hermes-webui.conf）——**它有 `location /api/` 泛匹配兜底转发到 Hermes(9123)，新增路由不配特化会被 Hermes 404 吞掉**（scenes/asr/agent 都踩过）
> - `16668`（qingliao_http.conf）与 `443`（webui_443.conf）——按模块名加 `location /api/xxx { proxy_pass http://127.0.0.1:PORT; }`
>
> ⚠️ **场景动作 service 格式**：动作里存 `climate.turn_off`（点分隔），执行时后端自动拆为 HA 路径 `/api/services/climate/turn_off`（斜杠），勿直接拼接点号字符串（会 404）。

## 🔄 一键更新

已装好的实例，在**仓库目录**里跑：

```bash
./update.sh            # 更新到最新 + 重建容器（推荐）

也可以在Nori App 里一键更新：设置 →「后端更新」→ 一键更新（走 /api/selfupdate，
效果等同在 NAS 上跑 ./update.sh；老版本后端没有此接口时 App 会显示手动命令）。
./update.sh --check    # 只看有没有新版，不改任何东西
./update.sh --version v4.0.13   # 更新到指定 tag（配套某个 App 版本）
```

`update.sh` 会依次做：检查版本 → 列出待更新提交 → **自动备份 `data/` 到 `backups/`** →
`git pull` → `docker compose up -d --build` → 轮询等待 9127 就绪 → 提示有无报错。

**不会动你的数据**：`.env`（访问密码/token 都在里面）和 `data/`（会话、配置、凭据）始终保留，
只替换 `backend/` 代码。万一新版本有问题：

```bash
git checkout -        # 代码回退
docker compose up -d --build
# 数据如需恢复：tar xzf backups/data-<时间戳>.tar.gz
```

App 与后端版本无需严格对应：后端向前兼容，App 旧版连新后端也能跑。

## 📋 查询后端版本

```bash
curl http://127.0.0.1:9127/api/version
# {"ok": true, "version": "v4.0.13", "commit": "8f2e181", "built": "2026-10-01", "modules": 32}
```

`GET /api/version` **免鉴权**（只返回版本号，不含任何配置/路径/凭据），App「关于」页用它显示
后端版本。`./update.sh` 会自动把版本信息写进 `.env`，用户不用手动配。

| 字段 | 含义 |
|---|---|
| `version` | 版本号（tag）。无 tag 时为空字符串 |
| `commit` | 短 commit hash，精确定位代码 |
| `built` | 构建/提交日期 |
| `modules` | 后端 `_api.py` 模块数，粗略反映版本新旧 |

版本号来源按此顺序回退：环境变量 `QL_BACKEND_VERSION` → `QL_VERSION` 文件 → `.git`。
三者都没有时各字段返回空字符串（**接口仍返回 200**，不报错）。

## 📖 踩坑记录在哪

完整踩坑实录（sudo/nginx/systemd/后端 patch/鉴权 token/PWA 缓存/ASR 自愈/docker 解析/看门狗）沉淀在 Hermes 技能 `qingliao-webui`（开发/调试/部署Nori 必读）与 NAS `轻聊app/避坑指南.md`（iOS 端）。

## ⚙️ 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `QL_PASSWORD` | ✅ | 空→自动生成随机密码写入 `$QL_DATA_DIR/initial_password.txt` | 统一访问密码（`install.sh` 会写进 `.env`） |
| `QL_INBOX_TOKEN` | ✅ | 空=收件箱/后台作业接口拒绝放行 | 服务间 token，须与 Hermes 插件侧一致（`install.sh` 自动生成） |
| `QL_PUSH_TOKEN` | ✅ | 空=推送队列接口拒绝放行 | 服务间 token，须与投递 cron 侧一致（`install.sh` 自动生成） |
| `QL_HERMES_CONFIG` | | — | Hermes `config.yaml` 路径；不设置时按 `QL_CONFIG_YAML` → `/data/hermes_config.yaml` 依次探测 |
| `QL_DATA_DIR` | | `/data` | 数据目录（会话/上传/日志/密钥） |
| `QL_UPLOAD_DIR` | | `$QL_DATA_DIR/uploads` | 文件上传目录 |
| `QL_HERMES_URL` | ✅ | — | 上游 LLM（OpenAI 兼容端点） |
| `QL_HERMES_KEY` | | 空 | 上游 LLM API Key |
| `QL_HA_URL` | | `http://localhost:8123` | Home Assistant 地址（可选） |
| `QL_HA_TOKEN` | | 空 | HA 长期访问令牌 |
| `QL_ROUTER_HOST` | | 空 | 路由器 SSH 地址（可选，Clash 管理） |
| `QL_ROUTER_USER` | | `root` | 路由器 SSH 用户 |
| `QL_ROUTER_PASSWORD` | | 空 | 路由器 SSH 密码 |
| `QL_DOCKER_ROOT` | | `/data/docker` | Docker Compose 项目目录 |
| `QL_LIFE_DIR` | | `$QL_DATA_DIR` | 生活/目标数据目录（goals、express_watch） |
| `QL_DIAG_DIR` | | `$QL_DATA_DIR/diag` | 诊断落盘目录（不可写则退 `/tmp/qingliao_diag`） |
| `QL_HERMES_CONTAINER` | | `hermes-container` | 目标 Hermes 容器名（重启网关/查日志用） |
| `QL_HERMES_DATA_DIR` | | — | Hermes 容器 `/opt/data` 对应的宿主目录（媒体/文件路径映射） |
| `QL_HOST_SKILLS_DIR` | | `$QL_HERMES_DATA_DIR/skills` | 宿主 skills 目录（网盘技能包安装目标） |
| `QL_HERMES_STATE_DB` | | — | Hermes `state.db` 路径（token 用量统计读取） |
| `QL_HERMES_PYTHON` | | `sys.executable` | 目标容器内 Python 解释器（路由器 SSH 用） |
| `QL_PARAMIKO_PATH` | | 空 | 目标容器内 paramiko 的 site-packages（留空则不注入 `PYTHONPATH`） |
| `STREAM_DATA_DIR` | | `$QL_DATA_DIR/streams_data` | 流式任务数据 |
| `STREAM_DOC_INLINE_MAX` | | `30000` | 聊天附件正文注入上限（字）：最新一条用户消息里引用的文件按此截断注入（`doc_ref.py`） |
| `STREAM_DOC_OLDER_MAX` | | `800` | 更早历史轮里同一附件只注入这么长的节选（跨轮记得文件但不重复付全文 token） |
| `SESSIONS_DATA_DIR` | | `$QL_DATA_DIR/sessions` | 会话数据 |
| `QL_LOG_CONTAINER` | | `hermes` | 日志模块查询的容器名 |
| `QL_AGENT_URL` | | DeepSeek 官方 | Agent 模式模型端点（需支持 function calling） |
| `QL_AGENT_KEY` | | 空 | Agent 模式 API Key |
| `QL_AGENT_MODEL` | | `deepseek-chat` | Agent 模式模型名 |
| `QL_OAUTH_REDIRECT_BASE` | | 空→用请求 Host 头推导 | OAuth 回调基地址（如 `https://xxx.lucky.com`），厂商授权后浏览器跳回 `…/api/hermes/oauth/callback` |
| `QL_OAUTH_<VENDOR>_CLIENT_ID` / `QL_OAUTH_<VENDOR>_CLIENT_SECRET` | | 空 | 云服务连接器厂商开发者凭证（VENDOR=FEISHU/DINGTALK/WECOM/TENCENT_DOCS/BAIDU_NETDISK）；商业版内置，自托管用户按下方「连接器 OAuth 配置」注册一次后填入 |

## 🔌 连接器 OAuth 配置

连接器页「云服务」点「连接」走标准 OAuth：用户在厂商授权页点允许，
浏览器回调本后端换 token，全程不填 URL/Token。token 存
`{STREAM_DATA_DIR}/oauth_tokens.json`（0600），过期前自动续期。

- **商业版**：开发者凭证由产品方统一注册、内置，开箱即用。
- **自托管（NAS/Docker）**：每个厂商去其开放平台注册一次应用，
  回调地址填 `QL_OAUTH_REDIRECT_BASE` + `/api/hermes/oauth/callback`，
  把拿到的 Client ID / Secret 填进后端环境变量后重启后端：

| 厂商 | 开放平台 | 环境变量 |
|---|---|---|
| 飞书 | open.feishu.cn → 开发者后台创建企业自建应用 | `QL_OAUTH_FEISHU_CLIENT_ID` / `QL_OAUTH_FEISHU_CLIENT_SECRET` |
| 钉钉 | open.dingtalk.com → 创建 H5 微应用/小程序 | `QL_OAUTH_DINGTALK_CLIENT_ID` / `QL_OAUTH_DINGTALK_CLIENT_SECRET` |
| 企业微信 | work.weixin.qq.com → 应用管理创建自建应用 | `QL_OAUTH_WECOM_CLIENT_ID`（=corpid） / `QL_OAUTH_WECOM_CLIENT_SECRET`（=corpsecret） |
| 腾讯文档 | docs.qq.com 开放平台 | `QL_OAUTH_TENCENT_DOCS_CLIENT_ID` / `QL_OAUTH_TENCENT_DOCS_CLIENT_SECRET` |
| 百度网盘 | openapi.baidu.com → 开发者中心创建应用 | `QL_OAUTH_BAIDU_NETDISK_CLIENT_ID` / `QL_OAUTH_BAIDU_NETDISK_CLIENT_SECRET` |

未配置时 App 点「连接」会收到 `oauth_not_configured` 及中文指引，
不会静默失败。自测：`cd backend && python3 hermes_oauth_test.py`
（本地 mock 厂商走完全链路，23 项断言）。

## 🔔 主动通知偏好与频次闸

App 设置页可配，接口 `GET/POST /api/notify/prefs`
（另有 `/api/agent/notify/prefs` 别名）：

```json
{"daily_digest": true, "task_fail_notify": true, "max_per_day": 5}
```

- `daily_digest`：每日摘要开关（每天最多 1 条）
- `task_fail_notify`：定时任务失败通知开关
- `max_per_day`：每日主动推送上限（0–50，防打扰）

所有主动推送先过频次闸：超上限或已关闭直接拒绝并返回中文原因；
偏好落盘 `{STREAM_DATA_DIR}/notify_prefs.json`（0600）。

## ⏰ 定时任务运行历史

Hermes 实际执行定时任务，本后端是代理层：运行历史为本地 jsonl 镜像
（`{STREAM_DATA_DIR}/cron_runs.jsonl`，0600，保留最近 500 条），
每次查询 `GET /api/cron/runs?task_id=&limit=` 时先从 Hermes
`/api/jobs` 的 `latest_execution` 同步（去重键 task_id + finished_at）。
另有 `POST /api/cron/runs` 供外部执行器显式上报。

失败通知链路：状态命中失败集合、且任务配置未显式关闭
（`notify_on_fail != False`）、且通知偏好与频次闸放行、且本条未通知过 →
经推送队列入队，并把 `notified=true` 写回，避免重复打扰；
未知状态不通知（宁可漏，不可扰）。

## 🧠 从 ChatGPT / Claude / Gemini 导入记忆

`POST /api/memory/import`，宽容解析 `items`（每条取
content/text/message 字段，支持 ChatGPT 的 `{"parts": [...]}` 形状），
逐条经 `/api/memory/add` 口径写入（去重、上限 50），返回
`{"imported": n, "skipped": m}`。

三家产品都没有「一键导出记忆」按钮，取数据的办法：

| 来源 | 做法 |
|---|---|
| ChatGPT | 设置 → 个性化 → 记忆，让它「把记住的关于我的事整理成 JSON 数组发我」，复制粘贴为 `items` |
| Claude | 对话里让它「总结你了解到的关于我的偏好，输出 JSON 数组」，复制粘贴为 `items` |
| Gemini | 同上；或从 Google Takeout 导出后提取文本条目 |

示例：`{"source": "chatgpt", "items": ["我喜欢喝美式", {"text": "养了一只猫"}]}`

## 📦 数据导出

`GET /api/data/export?format=json`（默认）/ `?format=zip`
（另有 `/api/agent/data/export` 别名）：打包记忆条目 + Soul 人设 +
定时任务配置 + 通知偏好，返回文件下载。**不含任何 token/密钥**
（只取白名单字段，导出前再做键名扫描兜底）。

自测：`cd backend && python3 wave3_test.py`（42 项断言，覆盖四功能）。

## 📰 资讯点赞 / 动作权限 / 产物沉淀 / 媒体生成 / 简报口径

Wave 3 收尾：iOS 已按以下契约写好 UI，后端补齐真接口
（`backend/agent_prefs.py`），全部落盘 `{STREAM_DATA_DIR}` 下 0600 文件，
App 是纯控制平面：

| 接口 | 说明 |
|---|---|
| `POST /api/agent/brief/like` `{article_id, liked}` / `GET /api/agent/brief/likes` | 资讯点赞，`brief_likes.json` |
| `GET/POST /api/agent/action-policy` | 7 类动作默认策略（read_calendar/read_contacts/send_message/web_search/file_rw/device_control/media_generate，值 allow/ask/deny，默认 ask），`action_policy.json`。**删除类操作后端永远单独确认，不受 policy 影响** |
| `GET/POST/DELETE /api/agent/artifacts` | 产物沉淀（id/title/kind/content/created_at），上限 200 条（新→旧，淘汰最旧），`artifacts.json` |
| `POST /api/agent/media/generate` `{prompt}` | v1 如实返回 **501** `media_not_configured`（无 `QL_MEDIA_API_KEY` 时）/ `media_not_implemented`（有凭证但管线未接时）；**绝不伪造图片** |
| `GET/POST /api/agent/brief` | 简报口径：v1 只存用户口径，返回 `{"status":"not_configured","articles":[]}` |

> **简报引擎说明**：真正的 agent 主笔简报需要内容源管线
> （资讯源接入 → agent 撰写 → 定时投递），属后续专项；
> v1 不编造文章，`POST /api/agent/brief` 只保存用户口径偏好。

自测：`cd backend && python3 agent_prefs_test.py`（32 项断言）。

## 🤖 Agent 模式（工具调用）

消息含控制/查询意图（如"帮我查磁盘""把空调关了""生成离家模式"）时，自动切换 **Agent 通道**：直连支持 function calling 的模型（默认 DeepSeek 官方 API），模型可调用工具执行后回填结果：

- `get_time` / `get_disk_usage` / `get_service_status` / `get_temperature`
- `docker_ps` / `docker_action`（容器启停）
- `ha_list_entities` / `ha_call`（智能家居控制）
- `get_weather` / `scene_save` / `scene_run` / `scene_list`（场景）

**场景**：聊天里说「帮我生成离家模式：关灯、关空调、布防」→ Agent 查询 HA 实体 → 生成动作组存 `scenes.json` → 看板点场景卡一键执行（`POST /api/scenes/run`）。

## 🔗 上游依赖

- **LLM 上游**：任意 OpenAI 兼容端点（`/v1/chat/completions`）。支持流式输出与多模型 provider 路由。可与 [Hermes](https://hermes-agent.nousresearch.com) 网关、DeepSeek 官方 API、OpenCode Go 订阅等对接。
- **Home Assistant**（可选）：智能家居模块，未配置时该模块返回空数据。
- **路由器**（可选）：Clash 管理模块，需要容器能 SSH 访问路由器（装好 `paramiko` 依赖）。
- **Docker**（可选）：容器管理模块，需要挂载 `/var/run/docker.sock`。

## 📦 非 Docker 部署

```bash
pip install -r requirements.txt   # paramiko / cryptography / PyYAML / PyMuPDF（PDF 正文解析）
cd backend
export QL_PASSWORD=your-password
export QL_HERMES_URL=http://127.0.0.1:9123/v1/chat/completions
python3 qingliao_all.py
```

> 注意：`files_api` 使用了 `cgi` 模块（Python 3.13 已移除），请使用 **Python 3.11/3.12** 运行。

## 🧱 架构

- 单进程多线程：`qingliao_all.py` 启动全部服务（`http.server.ThreadingHTTPServer`）
- 纯标准库（HTTP 服务/JSON/线程），无 Web 框架
- 流式输出：后端持流（上游 SSE → 落盘 JSON），客户端按 `taskId` 轮询增量
- 数据落盘：`$QL_DATA_DIR` 下的 JSON 文件（会话/流式任务/配置）

## 🆕 2026-08-16 变更（本地模型 + 修复，接手必读）

- **stream_api**：新增 `provider=local` 分支——直连 Ollama（`http://127.0.0.1:11434/v1/chat/completions`，不经 Hermes 9123，断网可用）；`/api/stream/sync-models` 的 provider key 从 `/data/hermes_config.yaml` 读取（含 deepseek/stepfun/xiaomi/opencode key；文件缺失时同步返回 `ok:false`——已生成）
- **local_api.py**（新，9149）：`/api/local/status|toggle|models|update|delete`（docker exec ollama 封装）
- **docker_api**：镜像 `in_use` 匹配修复（兼容 repo:tag / repo 无 tag / repo@digest / 镜像 ID；原 `endswith(":tag")` 把所有同 tag 镜像误标绿点）
- **Ollama 容器**：`docker run -d --name ollama --restart=always -p 11434:11434 -v /path/to/ollama:/root/.ollama ollama/ollama`；模型 qwen3:4b / qwen2.5:1.5b
- **Hermes 配置**（config.yaml）：providers.ollama 已配（手动可选）
- 部署方式：改文件 → 写入挂载目录 → `systemctl restart qingliao`

## 📄 License

MIT
