#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主动通知偏好 API：GET/POST /api/notify/prefs。

端点（unified_router 9127 挂载 /api/notify 前缀；另有 /api/agent/notify 别名，
借 /api/agent 前缀 → lucky 白名单/relay/nginx 三处零改动）：
  GET  /api/notify/prefs   → {"prefs": {"daily_digest","task_fail_notify","max_per_day"}}
  POST /api/notify/prefs   {daily_digest?, task_fail_notify?, max_per_day?}
                           → {"ok": true, "prefs": {...}}

鉴权：与其他设置类 API 一致（auth_api.check_auth + X-Notify-Password 头）。
只依赖标准库。
"""
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler

import notify_prefs


class Handler(BaseHTTPRequestHandler):
    def _check_auth(self):
        import auth_api
        return auth_api.check_auth(self.headers, "X-Notify-Password",
                                   os.environ.get("QL_PASSWORD", ""))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Auth-Token, X-Notify-Password")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _is_prefs(self, path):
        return path.endswith("/notify/prefs")

    def do_GET(self):
        if not self._check_auth():
            self._send(401, {"error": "未授权"})
            return
        path = urllib.parse.urlparse(self.path).path
        if self._is_prefs(path):
            self._send(200, {"prefs": notify_prefs.get_prefs()})
            return
        self._send(404, {"error": "Not Found"})

    def do_POST(self):
        if not self._check_auth():
            self._send(401, {"error": "未授权"})
            return
        path = urllib.parse.urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:  # noqa: BLE001
            body = {}
        if self._is_prefs(path):
            ok, res = notify_prefs.set_prefs(body)
            if not ok:
                self._send(200, {"ok": False, "error": res})
                return
            self._send(200, {"ok": True, "prefs": res})
            return
        self._send(404, {"error": "Not Found"})

    def log_message(self, *a):
        pass
