#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""后台任务（"一边干活一边对话"）。

用户发一个后台任务 → 独立 session（与主对话隔离，不带历史）→ 后台线程
调 Hermes 跑 → 主对话不阻塞。App 可查进度、可取消；完成/失败走
notify_prefs 频次闸推送通知（复用 push_api.enqueue），通知带 task_id 供 App 跳转。

接口（unified_router 注册 /api/agent/tasks → 本模块 Handler）：
  POST /api/agent/tasks {"prompt":"..."}        → {"ok":true,"task_id"}
  GET  /api/agent/tasks                          → {"tasks":[{id,title,status,progress,created_at,updated_at}]}
  GET  /api/agent/tasks/{id}                     → {"ok":true,"task":{status,progress,summary,result,...}}
  POST /api/agent/tasks/{id}/cancel              → {"ok":true,"status"} / {"ok":false,"error"}

任务记录落盘 {STREAM_DATA_DIR}/agent_tasks.json（原子写，0600），只保留
最近 100 条；服务重启后 status=running 的任务标记为 interrupted。

执行口径：复用 hermes_upstream（地址/Key/模型动态读取）调 Hermes
/v1/chat/completions 流式接口；progress 为启发式约数（progress_note 标注），
100 仅在真正完成时写。只依赖标准库。
"""
import json
import os
import re
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import hermes_upstream
import notify_prefs
import push_api

_MAX_TASKS = 100          # 落盘保留上限
_MAX_RUNNING = 5         # 并发上限（防资源耗尽）
_MAX_PROMPT = 4000       # prompt 长度上限
_MAX_RESULT = 20000      # result 落盘截断
_PERSIST_THROTTLE = 2.0  # 流式执行中落盘节流（秒）

_STATUS = ("running", "done", "failed", "cancelled", "interrupted")


def _data_dir():
    return os.environ.get("STREAM_DATA_DIR", "/data/streams_data")


def _path():
    return os.path.join(_data_dir(), "agent_tasks.json")


_lock = threading.RLock()
_tasks = {}        # task_id -> record dict（内存权威）
_cancel = {}       # task_id -> threading.Event
_runner = None     # 测试注入：fn(prompt, on_chunk, cancel_event) -> str
_last_persist = 0.0


def _atomic_write_0600(path, obj):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".agent_tasks.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _persist(force=False):
    global _last_persist
    now = time.time()
    with _lock:
        if not force and now - _last_persist < _PERSIST_THROTTLE:
            return
        _last_persist = now
        items = sorted(_tasks.values(), key=lambda t: t["created_at"],
                       reverse=True)[:_MAX_TASKS]
    try:
        _atomic_write_0600(_path(), {"tasks": items})
    except Exception:
        pass


def _load():
    """启动恢复：读盘；running → interrupted（执行线程已随进程消失）。"""
    try:
        with open(_path(), encoding="utf-8") as f:
            d = json.load(f)
        items = d.get("tasks", [])
    except (OSError, ValueError):
        items = []
    with _lock:
        _tasks.clear()
        for t in items[:_MAX_TASKS]:
            if not isinstance(t, dict) or not t.get("id"):
                continue
            if t.get("status") == "running":
                t["status"] = "interrupted"
                t["progress_note"] = ""
                t["updated_at"] = time.time()
            _tasks[t["id"]] = t


def set_runner(fn):
    """测试注入执行器（None 恢复默认走 Hermes）。"""
    global _runner
    with _lock:
        _runner = fn


def _title_of(prompt):
    line = (prompt or "").strip().split("\n", 1)[0].strip()
    return line[:24] if line else "后台任务"


def _set(task_id, **kw):
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            return None
        t.update(kw)
        t["updated_at"] = time.time()
        return t


def _snapshot(task_id):
    with _lock:
        t = _tasks.get(task_id)
        return dict(t) if t else None


def create_task(prompt, runner=None):
    """创建任务并后台执行。返回 (ok, payload)。"""
    prompt = (prompt or "").strip()
    if not prompt:
        return False, {"error": "prompt 不能为空"}
    if len(prompt) > _MAX_PROMPT:
        return False, {"error": "prompt 过长（上限 %d 字）" % _MAX_PROMPT}
    with _lock:
        running = sum(1 for t in _tasks.values() if t.get("status") == "running")
        if running >= _MAX_RUNNING:
            return False, {"error": "后台任务已满（%d 个执行中），稍后再试" % _MAX_RUNNING}
        task_id = uuid.uuid4().hex[:12]
        now = time.time()
        _tasks[task_id] = {
            "id": task_id,
            "title": _title_of(prompt),
            "prompt": prompt,
            "status": "running",
            "progress": 5,
            "progress_note": "约数",
            "summary": "",
            "result": "",
            "error": "",
            "created_at": now,
            "updated_at": now,
        }
        _cancel[task_id] = threading.Event()
    _persist(force=True)
    th = threading.Thread(target=_worker,
                          args=(task_id, prompt, runner or _runner),
                          daemon=True)
    th.start()
    return True, {"task_id": task_id}


def get_task(task_id):
    t = _snapshot(task_id)
    if not t:
        return None
    return t


def list_tasks():
    with _lock:
        items = sorted(_tasks.values(), key=lambda x: x["created_at"],
                       reverse=True)[:_MAX_TASKS]
        return [{"id": t["id"], "title": t["title"], "status": t["status"],
                 "progress": t["progress"],
                 "created_at": t["created_at"],
                 "updated_at": t["updated_at"]} for t in items]


def cancel_task(task_id):
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            return False, {"error": "任务不存在"}
        if t["status"] != "running":
            return True, {"status": t["status"]}
        ev = _cancel.get(task_id)
    if ev:
        ev.set()
    # worker 检测到后会收尾；若 3 秒内未收尾（极端阻塞），直接标记
    for _ in range(30):
        if _snapshot(task_id)["status"] != "running":
            break
        time.sleep(0.1)
    else:
        _set(task_id, status="cancelled", progress_note="")
        _persist(force=True)
    return True, {"status": _snapshot(task_id)["status"]}


class _Cancelled(Exception):
    pass


def _notify(kind, task):
    """完成/失败通知：先过频次闸，再入推送队列。取消不通知。"""
    if kind == "failed":
        ok, _reason = notify_prefs.check_and_count("task_fail")
    else:
        ok, _reason = notify_prefs.check_and_count("task_done")
    if not ok:
        return False
    if kind == "failed":
        text = "后台任务失败：%s（%s） task_id=%s" % (
            task["title"], (task.get("error") or "未知错误")[:80], task["id"])
    else:
        text = "后台任务完成：%s task_id=%s" % (task["title"], task["id"])
    ok2, _ = push_api.enqueue(text)
    return ok2


def _default_runner(prompt, on_chunk, cancel_event):
    """默认执行器：Hermes /v1/chat/completions 流式；独立 session（不带历史）。"""
    url = hermes_upstream.chat_completions_url()
    key = hermes_upstream.get_key()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    messages = [
        {"role": "system",
         "content": "你是后台任务执行者。用户不在场，请独立完成下面的任务，"
                    "用简体中文简洁输出最终结果，不要寒暄。"},
        {"role": "user", "content": prompt},
    ]
    body = {"messages": messages, "stream": True}
    # Let Hermes resolve the model/provider from its own config.yaml. Sending a
    # custom provider's internal model ID here targets the gateway's wrong alias.
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    out = []
    try:
        # 读超时 10s：每次超时检查一次取消，保证取消及时响应
        resp = urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        raise RuntimeError("连接 Hermes 失败：%s" % e)
    with resp:
        while True:
            if cancel_event.is_set():
                raise _Cancelled()
            try:
                line = resp.readline()
            except socket.timeout:
                continue
            except Exception as e:
                raise RuntimeError("读取 Hermes 流失败：%s" % e)
            if not line:
                break
            try:
                text = line.decode("utf-8", "replace").strip()
            except Exception:
                continue
            if not text.startswith("data:"):
                continue
            payload = text[5:].strip()
            if payload == "[DONE]":
                break
            try:
                d = json.loads(payload)
                delta = (((d.get("choices") or [{}])[0].get("delta") or {})
                         .get("content")) or ""
            except (ValueError, AttributeError, IndexError):
                continue
            if delta:
                out.append(delta)
                on_chunk(delta)
    return "".join(out)


def _worker(task_id, prompt, runner):
    ev = _cancel.get(task_id)
    acc = [0]  # 输出字符数（启发式进度用）

    def on_chunk(delta):
        acc[0] += len(delta)
        # 启发式：10 起，按输出长度爬，封顶 95；如实标注约数
        p = min(95, 10 + acc[0] // 150)
        _set(task_id, progress=p)
        _persist()

    try:
        run = runner or _default_runner
        result = run(prompt, on_chunk, ev)
        if ev.is_set():
            raise _Cancelled()
        result = (result or "")[:_MAX_RESULT]
        _set(task_id, status="done", progress=100, progress_note="",
             result=result, summary=result[:120].strip())
        _persist(force=True)
        _notify("done", _snapshot(task_id))
    except _Cancelled:
        _set(task_id, status="cancelled", progress_note="")
        _persist(force=True)
    except Exception as e:  # noqa: BLE001
        _set(task_id, status="failed", progress_note="",
             error=str(e)[:200])
        _persist(force=True)
        _notify("failed", _snapshot(task_id))
    finally:
        with _lock:
            _cancel.pop(task_id, None)


# ---------------- HTTP Handler ----------------
from http.server import BaseHTTPRequestHandler  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    def _check_auth(self):
        import auth_api
        return auth_api.check_auth(self.headers, "X-Hermes-Password",
                                   os.environ.get("QL_PASSWORD", ""))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Auth-Token, X-Hermes-Password")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _split(self):
        """-> ("collection", None) | ("detail", id) | ("cancel", id) | (None, None)"""
        path = urllib.parse.urlparse(self.path).path
        if path.endswith("/agent/tasks"):
            return "collection", None
        m = re.search(r"/agent/tasks/([A-Za-z0-9_-]+)/cancel$", path)
        if m:
            return "cancel", m.group(1)
        m = re.search(r"/agent/tasks/([A-Za-z0-9_-]+)$", path)
        if m:
            return "detail", m.group(1)
        return None, None

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if not self._check_auth():
            self._send(401, {"error": "未授权"})
            return
        kind, tid = self._split()
        if kind == "collection":
            self._send(200, {"tasks": list_tasks()})
            return
        if kind == "detail":
            t = get_task(tid)
            if not t:
                self._send(404, {"ok": False, "error": "任务不存在"})
                return
            t = dict(t)
            t.pop("prompt", None)  # 列表/详情不回传完整 prompt（省流量）
            self._send(200, {"ok": True, "task": t})
            return
        self._send(404, {"error": "Not Found"})

    def do_POST(self):
        if not self._check_auth():
            self._send(401, {"error": "未授权"})
            return
        kind, tid = self._split()
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:  # noqa: BLE001
            body = {}
        if kind == "collection":
            ok, payload = create_task(body.get("prompt"))
            self._send(200, {"ok": ok, **payload})
            return
        if kind == "cancel":
            ok, payload = cancel_task(tid)
            code = 200 if ok else 404
            self._send(code, {"ok": ok, **payload})
            return
        self._send(404, {"error": "Not Found"})


_load()
