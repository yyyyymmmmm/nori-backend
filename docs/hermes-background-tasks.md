# Hermes 后台任务配置（B 方案）

## 目标

让 Hermes 收到耗时任务时，不阻塞对话，而是丢后台跑，用户可继续聊天。

## 原理

Hermes v1.0+ 原生支持 `async_delegation` 工具集：
- `delegate_task_async`：丢后台，立刻返回 task_id
- `check_task` / `steer_task` / `collect_task` / `cancel_task`：查进度、中途纠偏、收结果、取消

CLI 里 `/bg` 命令就是用的这套。

## 配置步骤

### 1. 确认 toolset 已启用

编辑 Hermes 配置 `~/.hermes/config.yaml`（NAS 上是 `/home/agent/.hermes/config.yaml`）：

```yaml
tools:
  # 确保 async_delegation 在启用列表里
  enabled:
    - async_delegation
    # ... 其他你用的 toolset
```

如果用的是默认配置，`async_delegation` 一般已启用。用 `hermes tools` 命令确认。

### 2. 加 system prompt 规则

在 Hermes 的 system prompt（或 SOUL.md）里加：

```markdown
## 后台任务规则

耗时超过 30 秒的任务（深度研究、大批量处理、长时间监控），必须用
`delegate_task_async` 丢后台，不要同步傻等。

流程：
1. 调 `delegate_task_async(goal="...")` 拿到 task_id
2. 立刻回用户："在后台跑了（任务 #xxx），你先忙别的，做完通知你"
3. 对话继续可用

用户问进度时调 `check_task(task_id)`。
用户想中途改方向时调 `steer_task(task_id, "新的指示")`。
```

### 3. 对接 Nori 后端（可选）

如果想让 Nori App 的任务中心也显示 Hermes 的后台任务，需要 Hermes 调 Nori 后端的任务 API。

给 Hermes 加个自定义 tool（`~/.hermes/tools/nori_task.py` 或 skill）：

```python
def nori_create_task(prompt: str) -> str:
    """在 Nori 后端创建后台任务，返回 task_id。"""
    import urllib.request, json, os
    backend = os.environ.get("NORI_BACKEND", "http://127.0.0.1:9123")
    token = os.environ.get("NORI_TOKEN", "")
    req = urllib.request.Request(
        backend + "/api/agent/tasks",
        data=json.dumps({"prompt": prompt}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + token},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["task_id"]
```

然后在 system prompt 里加：优先用 Nori 后台任务（App 能看到进度），而不是 Hermes 内部的 async_delegation。

### 4. 验证

1. 对 Hermes 说："研究一下 XX，丢后台跑"
2. Hermes 应立刻回"在后台跑了"，对话输入框可用
3. 继续聊天，确认不阻塞
4. 过几分钟问"任务怎么样了"，应能查到进度

## 常见问题

**Q: Hermes 还是同步等，不丢后台？**
A: 检查 `async_delegation` toolset 是否启用；检查 system prompt 是否写了规则。Hermes 默认可能倾向同步，需要明确指示。

**Q: Nori App 任务中心看不到 Hermes 的任务？**
A: 正常。Hermes 内部任务和 Nori 后端任务是两套。要统一走第 3 步的对接，让 Hermes 调 Nori 的 API。

**Q: 任务做完怎么通知？**
A: Hermes 内部任务做完会回对话。Nori 后端任务做完调 `_notify`（看 `agent_tasks.py:218`），可接推送。
