#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 工具服务管理 API（v3.5.0 新增）

让轻聊 App 管理 Hermes 的 MCP servers：App 配 key → 本模块写 Hermes config.yaml
的 mcp_servers 段 → 重启 hermes 容器 → 本地模式聊天自动获得 MCP 工具
（Hermes 是 MCP host，工具在 agent loop 中自动可调用）。

端点（unified_router 9127 挂载 /api/mcp 前缀）：
  GET  /api/mcp/servers          列表（key 脱敏）+ 预置模板
  POST /api/mcp/save             {name, template+key 或 url, enabled}
  POST /api/mcp/delete           {name}
  GET  /api/mcp/restart_status   最近一次 hermes 重启结果

安全：只允许 http(s) URL；config 写入为原子替换（临时文件 + rename）；
     key 只写 Hermes config（内网隔离目录），接口返回时一律脱敏。
"""
import json
import os
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler

RESTART_STATUS_FILE = "/data/streams_data/mcp_restart_status.json"

# 预置 MCP 模板（App 端展示用；key 由用户填入 URL 的 {key} 占位）
MCP_TEMPLATES = [
    {
        "id": "amap",
        "name": "高德地图",
        "desc": "天气/POI搜索/路径规划/导航/打车等地图能力",
        "url_template": "https://mcp.amap.com/mcp?key={key}",
        "key_hint": "高德开放平台 Web服务 Key（lbs.amap.com 免费申请）",
    },
]

_LOCK = threading.Lock()
_RESTARTING = False

def _load_servers():
    """Read MCP servers from the live Hermes config; never report a stale mirror."""
    import hermes_inspect
    ok, config = hermes_inspect.get_hermes_config()
    if not ok or not isinstance(config, dict):
        raise RuntimeError("无法读取 Hermes 正在使用的 config.yaml: %s" % config)
    configured = config.get("mcp_servers")
    if configured is None:
        return {}
    if not isinstance(configured, dict):
        raise RuntimeError("Hermes config 的 mcp_servers 不是字典")
    servers = {}
    for name, value in configured.items():
        if isinstance(value, dict):
            # Keep all Hermes-specific fields (headers, transport, env, etc.)
            # in memory so saving one server does not erase the other entries.
            servers[str(name)] = dict(value)
        else:
            servers[str(name)] = value
    return servers


def _save_servers(servers):
    import hermes_inspect
    ok, detail = hermes_inspect.update_hermes_config("mcp_servers", servers)
    if not ok:
        raise RuntimeError(detail)


def _restart_hermes_async():
    """异步重启 hermes 容器；结果写状态文件供 /api/mcp/restart_status 查询"""
    global _RESTARTING
    with _LOCK:
        if _RESTARTING:
            return {"ok": False, "error": "重启已在进行中"}
        _RESTARTING = True
    status = {"ok": False, "ts": "", "error": ""}

    def _worker():
        global _RESTARTING
        try:
            import datetime
            status["ts"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            import hermes_inspect
            cid = hermes_inspect.find_hermes_container()
            if not cid:
                raise RuntimeError("找不到 Hermes 容器")
            code, result = hermes_inspect._docker_api(
                "POST", "/containers/%s/restart?t=10" % urllib.parse.quote(str(cid), safe=""))
            if code in (204, 200):
                status["ok"] = True
            else:
                status["error"] = str(result or "Hermes restart failed")[:300]
        except Exception as exc:  # noqa: BLE001
            status["error"] = str(exc)[:300]
        finally:
            try:
                os.makedirs(os.path.dirname(RESTART_STATUS_FILE), exist_ok=True)
                with open(RESTART_STATUS_FILE, "w", encoding="utf-8") as f:
                    json.dump(status, f, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                pass
            with _LOCK:
                _RESTARTING = False

    threading.Thread(target=_worker, daemon=True, name="mcp-hermes-restart").start()
    return {"ok": True, "msg": "重启已触发，约 30 秒后生效"}


def _mask_key(url):
    return re.sub(r"(key=)[^&]+", r"\1***", url)


class Handler(BaseHTTPRequestHandler):
    """unified_router 委托 handler（照 memory_api 模式）"""

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token")

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self):
        import auth_api
        return auth_api.check_auth(self.headers, "X-Mcp-Password",
                                   os.environ.get("QL_PASSWORD", ""))

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if not self._check_auth():
            self._send(401, {"error": "未授权"})
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/mcp/servers"):
            try:
                servers = _load_servers()
            except Exception as exc:  # noqa: BLE001
                self._send(503, {"ok": False, "error": str(exc)[:200]})
                return
            safe = []
            legacy_map = {}
            for name, e in servers.items():
                e = e if isinstance(e, dict) else {}
                url = str(e.get("url") or "")
                entry = {
                    "name": name,
                    "url": _mask_key(url),
                    "has_key": "key=" in url,
                    "enabled": e.get("enabled", True),
                    "is_template": False,
                }
                safe.append(entry)
                legacy_map[name] = {k: v for k, v in entry.items() if k != "name"}
            # Both current iOS callers expect a name-keyed dictionary. Keep a
            # secondary array for clients that prefer list iteration.
            self._send(200, {"ok": True, "servers": legacy_map,
                             "servers_list": safe,
                             "templates": MCP_TEMPLATES})
            return
        if parsed.path.startswith("/api/mcp/restart_status"):
            st = {}
            try:
                with open(RESTART_STATUS_FILE, encoding="utf-8") as f:
                    st = json.load(f)
            except Exception:  # noqa: BLE001
                pass
            self._send(200, {"ok": True, "status": st})
            return
        self._send(404, {"error": "Not Found"})

    def do_POST(self):
        if not self._check_auth():
            self._send(401, {"error": "未授权"})
            return
        parsed = urllib.parse.urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:  # noqa: BLE001
            body = {}

        if parsed.path.startswith("/api/mcp/save"):
            self._do_save(body)
            return
        if parsed.path.startswith("/api/mcp/delete"):
            self._do_delete(body)
            return
        self._send(404, {"error": "Not Found"})

    def _do_save(self, body):
        name = re.sub(r"[^A-Za-z0-9_-]", "", str(body.get("name", "")))[:40]
        key = str(body.get("key", "")).strip()
        template = str(body.get("template", "")).strip()
        url_in = str(body.get("url", "")).strip()
        enabled = bool(body.get("enabled", True))

        if not name:
            self._send(400, {"ok": False, "error": "name 必填"})
            return
        if template:
            tpl = next((t for t in MCP_TEMPLATES if t["id"] == template), None)
            if not tpl:
                self._send(400, {"ok": False, "error": "未知模板"})
                return
            if not key:
                self._send(400, {"ok": False, "error": "key 必填"})
                return
            url = tpl["url_template"].replace("{key}", key)
        elif url_in:
            if not re.match(r"^https?://", url_in):
                self._send(400, {"ok": False, "error": "url 须以 http(s):// 开头"})
                return
            url = url_in
        else:
            self._send(400, {"ok": False, "error": "template+key 或 url 必填一个"})
            return

        try:
            servers = _load_servers()
            entry = dict(servers.get(name) or {})
            entry.update({"url": url, "enabled": enabled})
            servers[name] = entry
            _save_servers(servers)
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"ok": False, "error": "写配置失败: %s" % exc})
            return
        self._send(200, {"ok": True, "name": name, "restart": _restart_hermes_async(),
                         "hint": "约 30 秒后聊天即可使用新工具"})

    def _do_delete(self, body):
        name = str(body.get("name", ""))
        try:
            servers = _load_servers()
            if name not in servers:
                self._send(404, {"ok": False, "error": "不存在"})
                return
            servers.pop(name)
            _save_servers(servers)
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"ok": False, "error": "写配置失败: %s" % exc})
            return
        self._send(200, {"ok": True, "restart": _restart_hermes_async()})

    def log_message(self, fmt, *args):
        pass
